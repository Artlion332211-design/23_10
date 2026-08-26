"""Order-book based spread/depth analysis.

Feeds two decisions: the liquidity filter (reject a symbol before scoring it
at all if its book is too thin or its spread too wide) and
`ExecutionEngine`'s MARKET-vs-LIMIT choice (tight spread -> MARKET is safe;
wide spread -> LIMIT protects against paying deep into the book).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class OrderBookSnapshot:
    symbol: str
    best_bid: Decimal
    best_ask: Decimal
    bid_depth_usdt: Decimal  # cumulative bid notional within the depth band of best bid
    ask_depth_usdt: Decimal  # cumulative ask notional within the depth band of best ask

    @property
    def mid_price(self) -> Decimal:
        if self.best_bid == 0 and self.best_ask == 0:
            return Decimal("0")
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_percent(self) -> Decimal:
        mid = self.mid_price
        if mid == 0:
            return Decimal("100")
        return (self.best_ask - self.best_bid) / mid * 100

    @property
    def total_depth_usdt(self) -> Decimal:
        return self.bid_depth_usdt + self.ask_depth_usdt

    @property
    def is_empty(self) -> bool:
        return self.best_bid == 0 or self.best_ask == 0


def parse_order_book(
    symbol: str, raw: dict[str, Any], *, depth_band_percent: Decimal = Decimal("0.5")
) -> OrderBookSnapshot:
    bids = [(Decimal(p), Decimal(q)) for p, q in raw.get("bids", [])]
    asks = [(Decimal(p), Decimal(q)) for p, q in raw.get("asks", [])]
    if not bids or not asks:
        return OrderBookSnapshot(
            symbol=symbol, best_bid=Decimal("0"), best_ask=Decimal("0"),
            bid_depth_usdt=Decimal("0"), ask_depth_usdt=Decimal("0"),
        )

    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    band = mid * depth_band_percent / 100

    bid_depth = sum((price * qty for price, qty in bids if price >= best_bid - band), Decimal("0"))
    ask_depth = sum((price * qty for price, qty in asks if price <= best_ask + band), Decimal("0"))

    return OrderBookSnapshot(
        symbol=symbol, best_bid=best_bid, best_ask=best_ask,
        bid_depth_usdt=bid_depth, ask_depth_usdt=ask_depth,
    )
