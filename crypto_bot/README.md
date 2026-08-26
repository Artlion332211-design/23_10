# crypto_bot

A local, autonomous Binance **Spot** trading bot (no futures, no margin, no
leverage) with Telegram control. It analyzes the market, looks for
high-quality entries, buys, averages down in a controlled way (DCA, not
martingale), takes profit at a net target, and repeats - 24/7, with every
important event pushed to Telegram.

## Philosophy

In priority order: **capital preservation > entry quality > profitability >
trade count.** The bot is designed to sit on its hands most of the time. It
should never trade just because it needs to find a trade - a BUY requires at
least 5 confirmed signals spread across at least 4 genuinely different
technical categories (trend, momentum, volume, volatility, structure), a BTC
market-regime filter must not be bearish/crashing, and a hard risk-manager
check (open positions, exposure, daily capital, consecutive losses) must
pass. News is a *risk filter*, never a buy trigger: it can only lower a score
or hard-block a trade, never originate one.

## Architecture

```
config/          Settings (.env) + RulesConfig (config.yaml) -> AppConfig
database/        SQLAlchemy models, repositories, migrations, session
exchange/        Binance REST client, symbol filters (Decimal-safe rounding),
                 WebSocket manager, order execution engine
market/          Indicators, multi-timeframe candle store, BTC market-regime
                 classifier, order book analysis, universe scanner
strategy/        SignalEngine (BUY_SCORE 0-100), DCA, take-profit, filters,
                 StrategyEngine (orchestrates a trade decision end to end)
risk/            RiskManager (the single authority on "is this trade
                 allowed right now"), exposure accounting, crash policy,
                 correlation/concentration control
news/            News aggregation (RSS, Binance announcements, CryptoPanic),
                 sentiment scoring, dedup
paper/           PaperBroker - simulates fills against real market data
                 without ever sending a real order
backtest/        Event-driven backtest engine reusing the exact live
                 strategy code, walk-forward optimizer, reports
telegram_bot/    Command handlers, message formatting, bot lifecycle
orchestration/   BotRuntime (scheduler loops), Watchdog (crash-restart +
                 heartbeats), startup reconciliation, daily report builder
app.py           Composition root / process entry point
```

Live and paper trading share the *entire* pipeline (StrategyEngine, RiskManager,
ExecutionEngine, DB schema) - only the `OrderExecutor` implementation differs
(`BinanceExecutionAdapter` vs `PaperBroker`). The backtest engine reuses the
same `SignalEngine` / DCA / take-profit / filters / market-regime /
correlation code too, so a backtest result reflects the real strategy, not a
separate approximation of it.

## Setup

Requires Python 3.11+.

```bash
cd crypto_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # or requirements.txt for a runtime-only install

cp .env.example .env
# edit .env: Binance API key/secret, Telegram bot token + your numeric user id
```

Review `config/config.yaml` for the structured strategy internals (indicator
periods, signal point-weights and categories, regime policy, crash detector,
anti-FOMO thresholds, correlation limits, universe filters, watchdog/scheduler
intervals, backtest defaults). `.env` holds the flat, per-deployment values
(position sizing, DCA levels, score thresholds, risk limits) - see
`.env.example` for the full list with comments.

Your Binance API key must have **Spot & Margin Trading -> "Enable Spot &
Margin Trading" only**. Withdrawal permission must be disabled on the key.

## Running

`MODE` defaults to `PAPER` and `DRY_RUN` defaults to `true` - the bot never
places a real order unless you deliberately opt in.

```bash
# Paper trading (simulated fills against real market data) - the default
python app.py

# Explicit mode override for this run only (.env is left untouched)
python app.py --mode paper

# Live trading - requires secrets to be set (validated at startup) AND an
# explicit --confirm-live flag on top of --mode live, so a bare CLI override
# can never enable real trading by accident
python app.py --mode live --confirm-live

# Backtest - one bounded historical replay against real Binance klines, then exit
python app.py --mode backtest --symbols BTCUSDT,ETHUSDT,SOLUSDT \
    --start 2024-01-01 --end 2024-06-01 --out backtest_report/
```

PAPER and LIVE start the full 24/7 orchestration: WebSocket market data feed,
periodic universe rescanning, candle-close-driven entry evaluation, a polled
position-monitor loop for DCA/take-profit, periodic news refresh, a daily
report, and the Telegram bot - all under a watchdog that restarts a crashed
loop with backoff. Stop with `Ctrl+C` (SIGINT) or SIGTERM; the bot shuts
down cleanly and sends a Telegram notice.

On every startup, before any trading resumes, the bot reconciles local state
against the source of truth: in LIVE mode that's Binance itself (pending
orders are re-checked, and any open position Binance no longer backs is
flagged - never auto-corrected or auto-sold); in PAPER mode the in-memory
paper balance is rebuilt from the durable local fill ledger, so restarting
with open paper positions doesn't lose track of them.

## Telegram commands

`TELEGRAM_ALLOWED_USER_ID` is the only user who can issue commands - every
other caller is silently ignored (the bot never reveals that a command
exists to anyone else). API keys and secrets never appear in any Telegram
output, including `/config`.

| Command | Purpose |
|---|---|
| `/status` | Mode, uptime, BTC regime, pause/emergency flags, health |
| `/balance` | Current balance (real or paper) |
| `/positions` | Open positions with entry, DCA count, target |
| `/signals` | Most recent scored candidates |
| `/pnl` | All-time realized PnL and win rate |
| `/today` | Today's report (balance, PnL, trades, fees, exposure) |
| `/history` | Recent closed trades |
| `/pause` / `/resume` | Stop/resume new BUYs (existing positions keep being managed) |
| `/stop_dca` / `/start_dca` | Disable/enable DCA on open positions |
| `/market` | Current BTC market regime and reasons |
| `/news` | Recent news items and sentiment |
| `/config` | Current configuration (secrets redacted) |
| `/emergency_stop` | Kill switch: stops new BUYs and DCA immediately. Never auto-sells existing positions - that would need a separate, explicit configuration decision |

## Before enabling LIVE

- [ ] Run PAPER for long enough to be comfortable with the bot's behavior and Telegram reporting
- [ ] Run a backtest over as much real history as you can get, on the symbols you intend to trade
- [ ] Binance API key: Spot trading permission only, withdrawals disabled
- [ ] `TELEGRAM_ALLOWED_USER_ID` set to *your* numeric Telegram user id
- [ ] Position sizing (`INITIAL_ORDER_USDT`, `MAX_POSITION_USDT`, `MAX_OPEN_POSITIONS`) sized to capital you can afford to lose
- [ ] `MAX_TOTAL_EXPOSURE_PERCENT` and `MAX_DAILY_NEW_CAPITAL_USDT` reviewed
- [ ] Start with `MODE=LIVE` and `DRY_RUN=true` first ("shadow live": real market data, real decisions, no real orders) and watch it for a while before setting `DRY_RUN=false`
- [ ] `python app.py --mode live --confirm-live` only once all of the above holds

## Testing

```bash
pytest tests/ -q
mypy .
ruff check .
```

The backtest engine was validated against real market data during
development (see below) and two real bugs were found and fixed as a direct
result - unit tests alone did not catch them.

## Known limitations of this build

Binance's REST/WebSocket API is geo-blocked (HTTP 451) from the sandbox this
project was built in, so the following could not be personally smoke-tested
against the real exchange here:

- Live/testnet order placement and the WebSocket kline/user-data streams
  (`exchange/`, `orchestration/runtime.py`) - built and unit/integration
  tested against fakes and mocks that implement the exact same interfaces,
  but never exercised against a real Binance connection.
- Paper trading against *live* Binance market data end-to-end (the
  `PaperBroker` itself, and its integration with `ExecutionEngine`, is
  integration-tested with a fake price source).

What *was* validated against real market data: the backtest engine was run
against real historical candles (Binance being unreachable, a same-shape
public candle API was used as a substitute purely for this validation, never
shipped in the bot) and this exercise found and fixed two real bugs - a
backtest open-position-undercounting issue and a misleading optimizer error
message - plus, independently, two real product bugs in the live/paper
trading code path (a cost-basis averaging bug and a news-symbol-extraction
bug) and, while wiring up startup reconciliation, a real precision bug
where every monetary column was silently round-tripping through 64-bit
float on top of SQLite (fixed in `database/models.py`).

Before running LIVE with real funds, test thoroughly on Binance Testnet
(`BINANCE_TESTNET=true`) first.
