"""Thin async wrapper around python-binance's Spot REST API.

This is the only module that talks to `binance.AsyncClient` directly. It
owns connection lifecycle, exchangeInfo caching, request throttling, and
turning transient network/HTTP errors into bounded retries.

It intentionally exposes order-placement methods (`create_order`,
`cancel_order`, ...) because it is a general-purpose exchange SDK wrapper -
but by convention **only `exchange/execution_engine.py` calls them**. Every
other layer (market data, scanner, strategy) must only use the read-only
methods. This keeps the "Strategy -> Risk -> Execution -> Binance" call
chain honest without needing artificial Python-level access control.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd
from binance import AsyncClient
from binance.exceptions import BinanceAPIException, BinanceRequestException

from exchange.symbol_filters import SymbolFilters
from utils.time import utcnow

logger = logging.getLogger(__name__)

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
]
OUTPUT_KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote",
]
RATE_LIMIT_STATUS_CODES = {418, 429}


class _Throttle:
    """Minimal-interval + bounded-concurrency request gate.

    Binance's exact per-endpoint REQUEST_WEIGHT costs change over time; hard
    coding a weight table would silently go stale. Instead this keeps
    request *rate* conservative and `BinanceClient._call` reacts hard to
    actual 429/418 responses (see `RATE_LIMIT_STATUS_CODES` below).
    """

    def __init__(self, min_interval: float = 0.15, max_concurrent: int = 5) -> None:
        self._min_interval = min_interval
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def __aenter__(self) -> _Throttle:
        await self._semaphore.acquire()
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._semaphore.release()


def _extract_retry_after(exc: BinanceAPIException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    value = headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _interval_to_ms(interval: str) -> int:
    multipliers = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
    return int(interval[:-1]) * multipliers[interval[-1]]


def _empty_klines_dataframe() -> pd.DataFrame:
    return pd.DataFrame(columns=OUTPUT_KLINE_COLUMNS)


def _klines_to_dataframe(raw: list[list[Any]]) -> pd.DataFrame:
    if not raw:
        return _empty_klines_dataframe()
    df = pd.DataFrame(raw, columns=KLINE_COLUMNS)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base", "taker_buy_quote"):
        df[col] = df[col].astype(float)
    df["trades"] = df["trades"].astype(int)
    return df[OUTPUT_KLINE_COLUMNS]


class BinanceClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        testnet: bool = True,
        exchange_info_ttl_seconds: int = 3600,
        https_proxy: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._testnet = testnet
        self._https_proxy = https_proxy
        self._client: AsyncClient | None = None
        self._throttle = _Throttle()
        self._exchange_info_ttl = exchange_info_ttl_seconds
        self._exchange_info_cache: dict[str, SymbolFilters] | None = None
        self._exchange_info_fetched_at: float = 0.0

    async def connect(self) -> None:
        self._client = await AsyncClient.create(
            api_key=self._api_key,
            api_secret=self._api_secret,
            testnet=self._testnet,
            https_proxy=self._https_proxy,
        )
        logger.info("Connected to Binance %s", "TESTNET" if self._testnet else "LIVE")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close_connection()
            self._client = None

    async def __aenter__(self) -> BinanceClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def raw(self) -> AsyncClient:
        if self._client is None:
            raise RuntimeError("BinanceClient not connected - call connect() first")
        return self._client

    async def _call(self, fn_name: str, /, **kwargs: Any) -> Any:
        """Call a python-binance AsyncClient method with throttling + bounded
        retry. Retries network errors and 5xx/429/418 responses; does not
        retry other 4xx client errors since repeating them changes nothing.
        """
        method = getattr(self.raw, fn_name)

        def _is_retryable(exc: BaseException) -> bool:
            if isinstance(exc, BinanceRequestException):
                return True
            if isinstance(exc, BinanceAPIException):
                return exc.status_code in RATE_LIMIT_STATUS_CODES or exc.status_code >= 500
            return isinstance(exc, (TimeoutError, ConnectionError, asyncio.TimeoutError))

        max_attempts = 5
        last_exc: BaseException | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                async with self._throttle:
                    return await method(**kwargs)
            except asyncio.CancelledError:
                raise
            except (BinanceAPIException, BinanceRequestException, TimeoutError, ConnectionError, OSError) as exc:
                last_exc = exc
                if not _is_retryable(exc) or attempt >= max_attempts:
                    raise
                delay = min(2**attempt, 30)
                if isinstance(exc, BinanceAPIException) and exc.status_code in RATE_LIMIT_STATUS_CODES:
                    delay = max(delay, _extract_retry_after(exc) or 0)
                    logger.warning(
                        "Binance rate limit (%s) on %s, backing off %.1fs", exc.status_code, fn_name, delay
                    )
                else:
                    logger.warning(
                        "Binance call %s failed (%r), retry %s/%s in %.1fs", fn_name, exc, attempt, max_attempts, delay
                    )
                await asyncio.sleep(delay)
        assert last_exc is not None  # pragma: no cover - loop always returns or raises
        raise last_exc

    # ------------------------------------------------------------------
    # Read-only market data (safe for any module to call)
    # ------------------------------------------------------------------

    async def ping(self) -> None:
        await self._call("ping")

    async def get_server_time(self) -> datetime:
        result = await self._call("get_server_time")
        return datetime.fromtimestamp(result["serverTime"] / 1000, tz=UTC)

    async def get_exchange_info(self, *, force_refresh: bool = False) -> dict[str, SymbolFilters]:
        now = time.monotonic()
        cache_fresh = (
            self._exchange_info_cache is not None
            and (now - self._exchange_info_fetched_at) < self._exchange_info_ttl
        )
        if not force_refresh and cache_fresh:
            assert self._exchange_info_cache is not None
            return self._exchange_info_cache

        raw = await self._call("get_exchange_info")
        parsed = {s["symbol"]: SymbolFilters.from_exchange_info_symbol(s) for s in raw["symbols"]}
        self._exchange_info_cache = parsed
        self._exchange_info_fetched_at = now
        logger.info("Refreshed exchangeInfo for %s symbols", len(parsed))
        return parsed

    async def get_symbol_filters(self, symbol: str, *, force_refresh: bool = False) -> SymbolFilters:
        info = await self.get_exchange_info(force_refresh=force_refresh)
        filters = info.get(symbol)
        if filters is None and not force_refresh:
            info = await self.get_exchange_info(force_refresh=True)
            filters = info.get(symbol)
        if filters is None:
            raise KeyError(f"Unknown Binance symbol: {symbol}")
        return filters

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int = 500,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> pd.DataFrame:
        kwargs: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            kwargs["startTime"] = int(start_time.timestamp() * 1000)
        if end_time is not None:
            kwargs["endTime"] = int(end_time.timestamp() * 1000)
        raw = await self._call("get_klines", **kwargs)
        return _klines_to_dataframe(raw)

    async def get_historical_klines(
        self, symbol: str, interval: str, start_time: datetime, end_time: datetime | None = None
    ) -> pd.DataFrame:
        """Paginate `get_klines` to cover an arbitrarily long history (a
        single call is capped at 1000 candles by Binance)."""
        end_time = end_time or utcnow()
        interval_ms = _interval_to_ms(interval)
        frames: list[pd.DataFrame] = []
        cursor = start_time
        while cursor < end_time:
            batch = await self.get_klines(symbol, interval, limit=1000, start_time=cursor, end_time=end_time)
            if batch.empty:
                break
            frames.append(batch)
            last_open = batch["open_time"].iloc[-1].to_pydatetime()
            next_cursor = last_open + timedelta(milliseconds=interval_ms)
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < 1000:
                break
        if not frames:
            return _empty_klines_dataframe()
        result = pd.concat(frames, ignore_index=True)
        return result.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)

    async def get_ticker_24h(self, symbol: str | None = None) -> Any:
        kwargs = {"symbol": symbol} if symbol else {}
        return await self._call("get_ticker", **kwargs)

    async def get_order_book(self, symbol: str, *, limit: int = 50) -> dict[str, Any]:
        return await self._call("get_order_book", symbol=symbol, limit=limit)

    async def get_symbol_ticker(self, symbol: str) -> dict[str, Any]:
        return await self._call("get_symbol_ticker", symbol=symbol)

    async def get_account_balances(self) -> dict[str, tuple[Decimal, Decimal]]:
        account = await self._call("get_account")
        balances: dict[str, tuple[Decimal, Decimal]] = {}
        for entry in account.get("balances", []):
            free, locked = Decimal(entry["free"]), Decimal(entry["locked"])
            if free > 0 or locked > 0:
                balances[entry["asset"]] = (free, locked)
        return balances

    async def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        kwargs = {"symbol": symbol} if symbol else {}
        return await self._call("get_open_orders", **kwargs)

    async def get_order_status(self, symbol: str, *, order_id: str | None = None,
                                 orig_client_order_id: str | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            kwargs["orderId"] = order_id
        if orig_client_order_id is not None:
            kwargs["origClientOrderId"] = orig_client_order_id
        return await self._call("get_order", **kwargs)

    # ------------------------------------------------------------------
    # Trading endpoints - by convention only exchange/execution_engine.py
    # should call these.
    # ------------------------------------------------------------------

    async def create_order(self, **kwargs: Any) -> dict[str, Any]:
        return await self._call("create_order", **kwargs)

    async def cancel_order(self, **kwargs: Any) -> dict[str, Any]:
        return await self._call("cancel_order", **kwargs)
