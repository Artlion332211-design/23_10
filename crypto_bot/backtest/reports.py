"""Render a `BacktestResult` into human-readable output: a plain-text
summary, CSV trade log, CSV equity curve, and an optional equity-curve PNG.
"""

from __future__ import annotations

import csv
from pathlib import Path

from backtest.engine import BacktestResult
from backtest.metrics import BacktestMetrics, EquityPoint, TradeRecord


def format_summary(metrics: BacktestMetrics, *, symbols: list[str], params: dict[str, object] | None = None) -> str:
    profit_factor = "inf" if metrics.profit_factor == float("inf") else f"{metrics.profit_factor:.2f}"
    lines = [
        "=" * 62,
        "BACKTEST REPORT",
        "=" * 62,
        f"Symbols: {', '.join(symbols)}",
        f"Period: {metrics.start.date()} -> {metrics.end.date()}",
        f"Starting balance: {metrics.starting_balance:.2f} USDT",
        f"Ending balance:   {metrics.ending_balance:.2f} USDT",
        f"Total return:     {metrics.total_return_percent:+.2f}%",
        f"Net profit:       {metrics.net_profit_usdt:+.2f} USDT",
        f"Total fees paid:  {metrics.total_fees_usdt:.2f} USDT",
        "-" * 62,
        f"Trades: {metrics.num_trades}   Win rate: {metrics.win_rate:.1f}%",
        f"Avg profit (winners): {metrics.avg_profit_percent:+.2f}%   Avg loss (losers): {metrics.avg_loss_percent:+.2f}%",
        f"Profit factor: {profit_factor}",
        f"Sharpe ratio:  {metrics.sharpe_ratio:.2f}",
        f"Sortino ratio: {metrics.sortino_ratio:.2f}",
        f"Max drawdown:  {metrics.max_drawdown_percent:.2f}%",
        f"Worst single-position drawdown: {metrics.worst_position_drawdown_percent:.2f}%",
        f"Avg holding time: {metrics.avg_holding_time_hours:.1f}h",
        f"Capital exposure (time-weighted): {metrics.exposure_percent:.1f}%",
        f"DCA used in {metrics.dca_frequency_percent:.1f}% of trades (avg {metrics.avg_dca_count:.2f} DCA/trade)",
        "=" * 62,
    ]
    if params:
        lines.append("Parameters: " + ", ".join(f"{k}={v}" for k, v in params.items()))
    return "\n".join(lines)


def write_trades_csv(trades: list[TradeRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "symbol", "opened_at", "closed_at", "avg_entry_price", "exit_price", "quantity",
            "cost_usdt", "proceeds_usdt", "net_pnl_usdt", "net_pnl_percent", "dca_count",
            "close_reason", "worst_drawdown_percent",
        ])
        for t in trades:
            writer.writerow([
                t.symbol, t.opened_at.isoformat(), t.closed_at.isoformat(), t.avg_entry_price, t.exit_price,
                t.quantity, t.cost_usdt, t.proceeds_usdt, t.net_pnl_usdt, t.net_pnl_percent, t.dca_count,
                t.close_reason, t.worst_drawdown_percent,
            ])


def write_equity_csv(equity_curve: list[EquityPoint], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", "equity_usdt"])
        for point in equity_curve:
            writer.writerow([point.timestamp.isoformat(), point.equity_usdt])


def plot_equity_curve(equity_curve: list[EquityPoint], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    path.parent.mkdir(parents=True, exist_ok=True)
    timestamps = [p.timestamp for p in equity_curve]
    values = [float(p.equity_usdt) for p in equity_curve]

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(timestamps, values)  # type: ignore[arg-type]  # matplotlib handles datetime x-axes fine; stubs are overly strict
    ax.set_title("Equity Curve")
    ax.set_xlabel("Time")
    ax.set_ylabel("Equity (USDT)")
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def write_full_report(result: BacktestResult, out_dir: Path, *, symbols: list[str], params: dict[str, object] | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_text = format_summary(result.metrics, symbols=symbols, params=params)
    (out_dir / "summary.txt").write_text(summary_text)
    write_trades_csv(result.trades, out_dir / "trades.csv")
    write_equity_csv(result.equity_curve, out_dir / "equity_curve.csv")
    try:
        plot_equity_curve(result.equity_curve, out_dir / "equity_curve.png")
    except Exception:  # noqa: BLE001 - the plot is a nice-to-have, never fatal to the report
        pass
    return out_dir / "summary.txt"
