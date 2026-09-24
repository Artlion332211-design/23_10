"""One-shot status export for a running (or stopped) bot instance.

Dumps open positions, recent closed trades, risk flags, and recent
warning/error events to a single JSON file - so a Claude Code session (or
anyone) that only has read access to this repository's git history, never
to the machine the bot actually runs on, can still answer "what is the bot
doing right now" from the latest pushed snapshot.

Intended to run on a schedule (see DEPLOYMENT.md's "Remote status checks"
section) via the same task-scheduling mechanism already used to keep the
bot itself running, each run followed by a commit+push of `status/latest.json`
to the dedicated `bot-status` branch (never the code branch - this is data,
not code, and would otherwise drown real commits in automated noise).

Read-only: opens the existing database and reads it; never touches
Binance's trading endpoints, never places or cancels an order. The one
network call it makes (a public ticker price, to compute unrealized PnL)
is best-effort - any failure there is caught and that position's PnL is
simply omitted, never a reason for the whole export to fail.
"""

from __future__ import annotations

import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_config  # noqa: E402
from database.migrations import run_migrations  # noqa: E402
from database.models import BotEventLevel  # noqa: E402
from database.repository import EventRepository, PositionRepository  # noqa: E402
from database.session import init_engine, session_scope  # noqa: E402
from exchange.binance_client import BinanceClient  # noqa: E402
from risk.risk_manager import RiskManager  # noqa: E402
from utils.time import utcnow  # noqa: E402

OUT_PATH = Path(__file__).resolve().parent.parent / "status" / "latest.json"


def _decimal(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


async def _fetch_prices(settings: Any, symbols: list[str]) -> dict[str, Decimal]:
    """Best-effort public ticker lookup - any failure (network, geo-block,
    a stale/blank API key) just means those symbols end up without a
    current_price/unrealized_pnl in the export, never a crashed run."""
    if not symbols:
        return {}
    prices: dict[str, Decimal] = {}
    client = BinanceClient(
        settings.binance_api_key.get_secret_value(), settings.binance_api_secret.get_secret_value(),
        testnet=settings.binance_testnet,
    )
    try:
        await client.connect()
        for symbol in symbols:
            try:
                ticker = await client.get_symbol_ticker(symbol)
                prices[symbol] = Decimal(str(ticker["price"]))
            except Exception:  # noqa: BLE001 - one symbol's failure must not block the others
                continue
    except Exception:  # noqa: BLE001 - no live prices this run is fine; the rest of the export still runs
        pass
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
    return prices


async def main() -> None:
    config = get_config()
    settings = config.env

    engine = init_engine(settings.database_url)
    run_migrations(engine)

    with session_scope() as session:
        open_positions = list(PositionRepository(session).get_open_positions())
        open_data_no_price: list[dict[str, Any]] = [
            {
                "symbol": p.symbol,
                "opened_at": p.opened_at.isoformat(),
                "avg_entry_price": _decimal(p.avg_entry_price),
                "total_quantity": _decimal(p.total_quantity),
                "target_price": _decimal(p.target_price),
                "dca_count": p.dca_count,
                "trailing_active": p.trailing_active,
                "trailing_is_early": p.trailing_is_early,
                "trailing_peak_price": _decimal(p.trailing_peak_price),
                "drawdown_alert_20_sent": p.drawdown_alert_20_sent,
                "drawdown_alert_30_sent": p.drawdown_alert_30_sent,
            }
            for p in open_positions
        ]
        symbol_by_avg_entry = {p.symbol: p.avg_entry_price for p in open_positions}

        closed = [
            {
                "symbol": p.symbol,
                "closed_at": p.closed_at.isoformat() if p.closed_at else None,
                "realized_pnl_usdt": _decimal(p.realized_pnl_usdt),
                "realized_pnl_pct": _decimal(p.realized_pnl_pct),
                "close_reason": p.close_reason,
            }
            for p in PositionRepository(session).recent_closed(limit=10)
        ]

        events = [
            {
                "timestamp": e.timestamp.isoformat(),
                "level": e.level.value,
                "category": e.category,
                "message": e.message,
            }
            for e in EventRepository(session).recent(limit=20, min_level=BotEventLevel.WARNING)
        ]

    prices = await _fetch_prices(settings, [p["symbol"] for p in open_data_no_price])
    open_data: list[dict[str, Any]] = []
    for p in open_data_no_price:
        symbol = p["symbol"]
        price = prices.get(symbol)
        entry = symbol_by_avg_entry[symbol]
        if price is not None and entry > 0:
            p = {
                **p,
                "current_price": str(price),
                "unrealized_pnl_pct": str((price / entry - 1) * 100),
            }
        open_data.append(p)

    flags = RiskManager(settings).status()

    report = {
        "exported_at": utcnow().isoformat(),
        "mode": settings.mode.value,
        "dry_run": settings.dry_run if settings.mode.value == "LIVE" else False,
        "risk_flags": {
            "buy_paused": flags.buy_paused,
            "dca_paused": flags.dca_paused,
            "emergency_stop": flags.emergency_stop,
            "consecutive_bad_trades": flags.consecutive_bad_trades,
        },
        "open_positions": open_data,
        "recent_closed_positions": closed,
        "recent_warnings_and_errors": events,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Wrote {OUT_PATH}")
    print(f"  mode={report['mode']} open_positions={len(open_data)} risk_flags={report['risk_flags']}")
    for p in open_data:
        pnl = f" pnl={p['unrealized_pnl_pct']}%" if "unrealized_pnl_pct" in p else " pnl=n/a (no live price)"
        print(f"  {p['symbol']}: entry={p['avg_entry_price']} qty={p['total_quantity']}{pnl}")


if __name__ == "__main__":
    asyncio.run(main())
