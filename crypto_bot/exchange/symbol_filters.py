"""Binance per-symbol trading-rule filters, with Decimal-safe rounding.

Binance rejects any order whose price/quantity doesn't land exactly on the
symbol's tick/step grid, and rejects notional value below the exchange
minimum. This module is the single place that turns "spend about $100 on
SOLUSDT" into an exact, exchange-valid (price, quantity) pair - or raises
`OrderWouldBeInvalid` so the caller never even attempts a doomed order.

Everything here uses `Decimal`, never `float`: float64 cannot represent
0.1 exactly, and repeated arithmetic on prices/quantities would eventually
drift off the exchange's step grid.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any


class OrderWouldBeInvalid(Exception):
    """No valid (price, quantity) could be constructed for this request."""


def to_decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step == 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def format_decimal(value: Decimal) -> str:
    """Render a Decimal the way Binance expects: plain fixed-point notation.

    `str(Decimal(...))` can produce scientific notation for very small/large
    values (e.g. `Decimal('1E-7')`), which Binance's API does not accept.
    """
    return format(value, "f")


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    lot_step_size: Decimal
    lot_min_qty: Decimal
    lot_max_qty: Decimal
    market_lot_step_size: Decimal
    market_lot_min_qty: Decimal
    market_lot_max_qty: Decimal
    min_notional: Decimal
    apply_min_notional_to_market: bool
    base_asset_precision: int
    quote_asset_precision: int

    @classmethod
    def from_exchange_info_symbol(cls, raw: dict[str, Any]) -> SymbolFilters:
        filters = {f["filterType"]: f for f in raw["filters"]}
        price_filter = filters.get("PRICE_FILTER", {})
        lot_filter = filters.get("LOT_SIZE", {})
        market_lot_filter = filters.get("MARKET_LOT_SIZE", lot_filter)
        notional_filter = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}

        min_notional_raw = notional_filter.get("minNotional", notional_filter.get("notional", "0"))
        apply_to_market = notional_filter.get("applyToMarket", notional_filter.get("applyMinToMarket", True))

        return cls(
            symbol=raw["symbol"],
            base_asset=raw["baseAsset"],
            quote_asset=raw["quoteAsset"],
            status=raw["status"],
            tick_size=to_decimal(price_filter.get("tickSize", "0.00000001")),
            min_price=to_decimal(price_filter.get("minPrice", "0")),
            max_price=to_decimal(price_filter.get("maxPrice", "0")),
            lot_step_size=to_decimal(lot_filter.get("stepSize", "0.00000001")),
            lot_min_qty=to_decimal(lot_filter.get("minQty", "0")),
            lot_max_qty=to_decimal(lot_filter.get("maxQty", "9000000000")),
            market_lot_step_size=to_decimal(market_lot_filter.get("stepSize", "0.00000001")),
            market_lot_min_qty=to_decimal(market_lot_filter.get("minQty", "0")),
            market_lot_max_qty=to_decimal(market_lot_filter.get("maxQty", "9000000000")),
            min_notional=to_decimal(min_notional_raw),
            apply_min_notional_to_market=bool(apply_to_market),
            base_asset_precision=int(raw.get("baseAssetPrecision", 8)),
            quote_asset_precision=int(raw.get("quoteAssetPrecision", 8)),
        )

    def is_tradable(self) -> bool:
        return self.status == "TRADING"

    def round_price(self, price: Decimal | float | str) -> Decimal:
        price = to_decimal(price)
        if self.tick_size == 0:
            return price
        steps = (price / self.tick_size).to_integral_value(rounding=ROUND_DOWN)
        return steps * self.tick_size

    def round_quantity(self, quantity: Decimal | float | str, *, market: bool = False) -> Decimal:
        """Floor to the LOT_SIZE step (and, for MARKET orders, also the
        MARKET_LOT_SIZE step). Always rounds DOWN: Binance rejects off-grid
        quantities, and rounding up could spend more than the caller intended.

        MARKET orders must satisfy *both* filters, and on spot Binance
        publishes MARKET_LOT_SIZE with stepSize 0 ("no extra constraint") -
        so using only the market step would send MARKET orders unrounded and
        get them rejected with "Filter failure: LOT_SIZE"."""
        quantity = _floor_to_step(to_decimal(quantity), self.lot_step_size)
        if market:
            quantity = _floor_to_step(quantity, self.market_lot_step_size)
        return quantity

    def _min_qty(self, market: bool) -> Decimal:
        return max(self.lot_min_qty, self.market_lot_min_qty) if market else self.lot_min_qty

    def _max_qty(self, market: bool) -> Decimal:
        if market and self.market_lot_max_qty > 0:
            return min(self.lot_max_qty, self.market_lot_max_qty)
        return self.lot_max_qty

    def validate_notional(
        self, price: Decimal | float | str, quantity: Decimal | float | str, *, market: bool = False
    ) -> None:
        if market and not self.apply_min_notional_to_market:
            return
        notional = to_decimal(price) * to_decimal(quantity)
        if notional < self.min_notional:
            raise OrderWouldBeInvalid(
                f"{self.symbol}: notional {notional} below exchange minNotional {self.min_notional}"
            )

    def quantity_for_notional(
        self, price: Decimal | float | str, notional_usdt: Decimal | float | str, *, market: bool = False
    ) -> Decimal:
        """Given a target USDT spend and a reference price, return a valid,
        rounded-down quantity - or raise OrderWouldBeInvalid if no valid
        quantity satisfies min qty / min notional at this price."""
        price = to_decimal(price)
        notional_usdt = to_decimal(notional_usdt)
        if price <= 0:
            raise OrderWouldBeInvalid(f"{self.symbol}: non-positive reference price {price}")

        raw_qty = notional_usdt / price
        qty = self.round_quantity(raw_qty, market=market)

        min_qty = self._min_qty(market)
        max_qty = self._max_qty(market)
        if qty < min_qty:
            raise OrderWouldBeInvalid(
                f"{self.symbol}: computed quantity {qty} below exchange minQty {min_qty} "
                f"for notional {notional_usdt} @ {price}"
            )
        if qty > max_qty:
            qty = self.round_quantity(max_qty, market=market)

        self.validate_notional(price, qty, market=market)
        return qty
