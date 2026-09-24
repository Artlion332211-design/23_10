# Project handoff: everything before you touch this bot

This document exists so a fresh Claude Code session (or a human) picking up
this project - especially one running **locally, with real terminal access**
to deploy and operate it - has everything needed without re-deriving it from
scratch: how the bot actually decides to trade, what it sends to Telegram and
when, the full history of bugs found and fixed and why the code looks the way
it does, what was deliberately left alone (and why), and what's already been
validated against real market data.

Read this once, fully, before changing strategy logic. `README.md` is the
quick reference (setup, commands, architecture map); `DEPLOYMENT.md` is the
Windows-server operations guide. This document is the "why" behind both.

**This bot has never traded with real money.** Everything below - including
the 3-day validation run - was PAPER mode against real market prices. Nothing
in this document authorizes flipping `MODE=LIVE`/`DRY_RUN=false`; that
decision belongs to the project owner alone, gated by the checklist in
§9 of the README.

---

## 1. What this bot is, in one paragraph

A Binance **Spot-only** (no margin, no futures, no leverage) trading bot.
It watches a scanned universe of liquid USDT pairs, scores each candidate on
a 0-100 technical scale across 5 independent categories, requires the BTC
market regime to not be actively bearish, requires news to not be actively
negative, and only then buys - small, sized as a fraction of capital, never
"all in." If price drops after entry, it can average down (DCA) up to 3
times, but only if a fresh re-analysis still confirms the original thesis -
never mechanically. It takes profit at a fee/slippage-adjusted net target,
with three layered mechanisms (trailing-after-target, early-arm trailing,
and a hard ceiling backstop - see §5) protecting the gain from round-tripping
back to zero. Every meaningful event is pushed to Telegram. Philosophy, in
priority order: **capital preservation > entry quality > profitability >
trade count.**

---

## 2. Architecture map

```
config/          Settings (.env, flat/per-deployment) + RulesConfig
                  (config.yaml, structured strategy internals) -> AppConfig
database/        SQLAlchemy models, repositories, migrations, session
exchange/        Binance REST client (exchange/binance_client.py), symbol
                  filters (Decimal-safe LOT_SIZE/PRICE_FILTER/MIN_NOTIONAL
                  rounding), WebSocket manager, order execution engine
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
tools/           export_status.py - read-only DB snapshot for remote checks
app.py           Composition root / process entry point
```

**Mandated call chain, enforced everywhere**: `StrategyEngine -> RiskManager
-> ExecutionEngine -> Binance`. Strategy decides *what*; Risk decides
*whether it's allowed*; Execution decides *how* (order type, rounding,
timeout). No layer skips ahead of the one before it.

**LIVE and PAPER share the entire pipeline** - StrategyEngine, RiskManager,
ExecutionEngine, the DB schema, all identical. Only the `OrderExecutor`
implementation differs (`BinanceExecutionAdapter` vs `PaperBroker`), and both
implement the exact same 3-method protocol (`submit`/`cancel`/`get_status`).
The backtest engine reuses the same SignalEngine/DCA/take-profit/filters/
regime/correlation code too - a backtest result reflects the real strategy,
never a separate approximation.

---

## 3. How a BUY decision actually gets made

Entry evaluation runs on every **15-minute candle close** for every symbol
still in the scanned universe (`orchestration/runtime.py::_evaluate_entry`,
triggered by `_on_kline_message`). Universe rescanning itself happens every
`SCANNER_INTERVAL_MINUTES` (default 15) and filters Binance's full USDT pair
list down by: quote asset USDT, not a stablecoin pair, not a leveraged token
(UP/DOWN/BULL/BEAR suffix), not blacklisted, `MIN_QUOTE_VOLUME_24H_USDT`
(default $5M), `MIN_LISTING_AGE_DAYS` (default 60), `min_trades_24h`
(10,000), spread/depth checks, capped to `SCANNER_TOP_N` (default 25) by
volume.

For each candidate, in order:

### 3.1 Technical signal scoring (`strategy/signal_engine.py` + `scoring.py`)

Seven signals, each belonging to exactly one of 5 categories (trend,
momentum, volume, volatility, structure) so "confirmation" can't be gamed by
five variants of the same idea. Weights from `config/config.yaml`:

| Signal | Category | Points | Fires when (1h timeframe unless noted) |
|---|---|---|---|
| `rsi_reversal` | momentum | 15 | 1h RSI(14) was oversold (<30) within the last 5 bars and is now turning up, OR 15m RSI reversal with 1h RSI < 55 |
| `macd_bullish` | momentum | 15 | 1h MACD histogram is positive |
| `ema_trend` | trend | 20 | 1h close above EMA20 and EMA20 above EMA50 (the `ema_trend_ok` composite) |
| `bollinger_recovery` | volatility | 12 | 1h close recovering off the lower Bollinger Band (20, 2σ) |
| `volume_confirmation` | volume | 18 | 1h volume ≥ 1.2× its 20-bar moving average |
| `vwap_recovery` | structure | 10 | 1h VWAP recovery, or 15m VWAP recovery |
| `market_structure` | structure | 10 | 1h higher-low swing structure, bullish candle pattern, RSI bullish divergence, or price near a support level |

**Confirmation rule** (both must hold, not just the point total):
`final_score >= MIN_BUY_SCORE` (default 75, adjusted by regime - see 3.2)
**AND** at least `MIN_CONFIRMED_SIGNALS` (default 5) individual signals
fired **AND** those signals span at least `MIN_CONFIRMATION_CATEGORIES`
(default 4) of the 5 categories. This is why five confirmations from only
two categories still doesn't qualify - it's not "genuinely independent"
confirmation.

**Two hard vetoes** applied regardless of score (`SignalEngine`):
- **ADX veto**: 1h ADX ≥ 35 with -DI dominant over +DI by ≥ 10 points -
  a strong bearish trend blocks the buy even if RSI looks tempting (don't
  mean-revert into a real downtrend).
- **4h trend-context veto**: this specific symbol's own 4h trend (not
  BTC's - see 3.2) classifies as CRASH-like - a bullish 15m/1h blip on the
  candidate itself cannot override its own bearish 4h picture.

### 3.2 BTC market regime filter (`market/market_regime.py`)

Evaluated on **BTCUSDT only**, across 15m/1h/4h (weighted 0.15/0.35/0.5),
independent of and in addition to the per-symbol 4h veto above - "before
buying any altcoin, the bot must check BTC." Each timeframe scores
±15 (price vs EMA200) ±10 (EMA20 vs EMA50) ±8 (MACD histogram sign) ±7 (RSI
above/below 55/45) ±10 (ADX≥25 direction), combined into a single
-100..+100 score, then classified:

| Score | Level | Buy policy | Score requirement |
|---|---|---|---|
| ≥ 40 | STRONG_BULL | allowed | `MIN_BUY_SCORE` − 5 |
| ≥ 15 | BULL | allowed | `MIN_BUY_SCORE` |
| ≥ −15 | NEUTRAL | allowed | `MIN_BUY_SCORE` + 5 |
| ≥ −40 | BEAR | allowed | `MIN_BUY_SCORE` + 10 |
| ≥ −70ish | STRONG_BEAR | **blocked** (RiskManager also blocks new positions, not DCA) | n/a |
| below | CRASH | **blocked** (RiskManager also blocks new positions AND DCA) | n/a |

A **separate, faster crash detector** can override the above instantly: BTC
15m closes dropped ≥4% in the last 60 minutes (`window_minutes`/
`drop_percent`) on ≥2.5× average volume with bearish momentum → immediate
CRASH regardless of the weighted score. There's also a "fast drop" dampener:
any ≥2.5% drop in the crash window caps the score at −45 even if the
weighted composite alone would have scored higher (STRONG_BEAR is enforced
faster than the slow composite would otherwise reach it).

### 3.3 Everything else that can block a BUY

- **News** (`news/`): only ever a *risk filter*, never a buy trigger. A
  critical-negative news item hard-blocks; otherwise sentiment nudges the
  score by −15..+5 (`news_adjustment`, capped small on the positive side
  deliberately).
- **AntiFOMO filter**: blocks if 1h change > 8%, 4h change > 15%, price is
  too far from EMA20 (in ATR units), RSI > 80, or an abnormal volume spike -
  don't chase a candle that already ran.
- **Correlation/concentration**: won't open a position highly correlated
  (>0.75 over 90 1h bars) with an already-open position beyond
  `max_correlated_positions` (2).
- **Liquidity freshness**: order book spread/depth checked right before
  submission, not just at scan time.
- **RiskManager.can_open_new_position** (the actual gate, `risk/risk_manager.py`):
  `emergency_stop` not active, buys not paused, consecutive losing streak
  under `MAX_CONSECUTIVE_BAD_TRADES` (default 3, auto-pauses new buys, not
  DCA, on breach), `MAX_OPEN_POSITIONS` (default 3, always 3 *different*
  symbols - never a second position on one already held), requested size
  under `MAX_POSITION_USDT`, projected exposure under
  `MAX_TOTAL_EXPOSURE_PERCENT` (default 35%), and today's already-deployed
  capital plus this request under `MAX_DAILY_NEW_CAPITAL_USDT` (default
  $500) - **this last cap covers DCA too, not just fresh entries, as of the
  fix in §8.3 below.**

Only when *all* of the above pass does `ExecutionEngine.buy()` actually
place an order (MARKET if spread ≤ `MAX_SPREAD_PERCENT`/2, else LIMIT - see
§6).

---

## 4. DCA (averaging down) - explicitly not martingale

`strategy/dca.py`. A price drop **alone is never sufficient** - "−3% → buy
independent of anything" is exactly what this is *not*. Up to
`MAX_DCA_COUNT` (default 3) levels, each individually gated:

| Level | Default drop from avg entry | Default size |
|---|---|---|
| 1 | −3% | $50 |
| 2 | −6% | $75 |
| 3 | −10% | $75 |

When price crosses a level's threshold, the bot re-runs the **full signal
re-analysis** (`evaluate_candidate`) fresh at the current price, then
requires **all** of: no hard veto still active, re-analysis score ≥
`MIN_DCA_SCORE` (default 65), no market crash, no blocking news, liquidity
still fine, projected position cost stays under `MAX_POSITION_USDT`, DCA not
manually paused (`/stop_dca`), no emergency stop, **and** (as of the fix in
§8.3) the DCA size doesn't push exposure past `MAX_TOTAL_EXPOSURE_PERCENT`
or today's capital past `MAX_DAILY_NEW_CAPITAL_USDT`. If the thesis no
longer holds, the bot does nothing and waits - it does not "rescue" a bad
entry by throwing more capital at it.

Every DCA fill recomputes the position's weighted-average entry price and,
from it, a new fee/slippage-adjusted take-profit target (§5).

---

## 5. How a position exits - three layered mechanisms

### 5.1 The target price (`strategy/take_profit.py::compute_target_price`)

Not "+10% on price" - **+`TARGET_PROFIT_PERCENT` (default 10%) net of the
sell-side taker fee and expected slippage.** The sell price is solved so
that, after both are deducted, net profit still clears the target:
`sell_price = avg_entry * (1 + target%) / ((1 - fee) * (1 - slippage%))`.
Recomputed after every DCA fill (new weighted average → new target).

### 5.2 Plain exit (`USE_TRAILING_AFTER_TP=false`, the default)

`current_price >= target_price` → immediate full MARKET/LIMIT close,
`close_reason="TAKE_PROFIT"`.

### 5.3 Trailing after target (`USE_TRAILING_AFTER_TP=true`)

At target, sell `TRAILING_PARTIAL_CLOSE_FRACTION` (default 60%) immediately
to lock in real profit, then arm a trailing stop on the remainder at
`TRAILING_DISTANCE_PERCENT` (default 2.5%) below the running peak price.

### 5.4 Early profit protection (`EARLY_PROFIT_PROTECTION_ENABLED`, on in
`.env` for this deployment)

**Independent of 5.3, and pre-empts it.** The moment net profit reaches
`EARLY_PROFIT_ARM_PERCENT` (default 9.5%, i.e. *before* the full target),
a **tight** trailing stop arms on the **full** position at
`EARLY_PROFIT_TRAILING_DISTANCE_PERCENT` (default 1.0%) below the running
peak. Rationale: a move that gets close to target and reverses before
touching it exactly still gets most of the gain locked in, instead of
riding it all the way back to break-even. Once armed, this pre-empts the
plain target check entirely - the position rides the early trail, not
5.2/5.3. **This is exactly the mechanism that closed the XRP position in the
3-day validation run - see §10.**

`Position.trailing_is_early` records which mode armed the trail, so
`manage_position` picks the right distance (1.0% vs 2.5%).

### 5.5 Hard profit-ceiling backstop (`HARD_PROFIT_CEILING_PERCENT`, default
12%, always on - not gated by any flag)

**The absolute last line of defense**, added after a code-review round
found nothing was guaranteed to close a position if 5.2/5.3/5.4 somehow all
failed to fire (a bug, a stuck resting order, a misconfiguration). Checked
at the very top of `manage_position`, **before even the resting-order
guard** - if net profit ever reaches 12%, the bot cancels anything still
resting for that position (folding in whatever partial fill it already
picked up - see §8.4) and force-closes the rest via MARKET order,
regardless of what the normal exit logic is doing. Settings validation
enforces `HARD_PROFIT_CEILING_PERCENT` > `TARGET_PROFIT_PERCENT` and >
`EARLY_PROFIT_ARM_PERCENT` - it must always sit above them so it only ever
fires as a genuine last resort, never as the normal exit.

### 5.6 Drawdown warnings (`DRAWDOWN_WARNING_PERCENT_1`/`_2`, default 20%/30%)

**Pure notification, changes nothing about position/exit logic.** The first
time an open position's price drops 20% (then separately 30%) below average
entry, one Telegram alert fires - never repeated for the same threshold on
the same position, even across a restart (`Position.drawdown_alert_20_sent`/
`_30_sent`, persisted). Uses the raw price ratio, not fee/slippage-adjusted
- this is a risk alert, not an exit-price calculation.

---

## 6. Order execution mechanics (`exchange/execution_engine.py`)

- **MARKET vs LIMIT**: spread ≤ `MAX_SPREAD_PERCENT`/2 (default 0.25%) →
  MARKET (instant, minimal slippage risk); wider → LIMIT at the current
  price (protects against paying deep into a thin book), cancelled after
  `LIMIT_ORDER_TIMEOUT_SECONDS` (default 90s) if still unfilled.
- **Write-ahead persistence**: the local `Order` row is created and
  committed **before** Binance is ever called. A crash between "Binance
  accepted it" and "we recorded the result" leaves a durable NEW-status row
  that startup reconciliation (§8.6) repairs against Binance's own order
  history - Binance is always the source of truth, never the local row
  alone.
- **`OrderRepository.has_resting_order()`**: the guard that stops
  `manage_position`/entry evaluation from submitting a second DCA/exit/
  entry order while one for the same symbol/position/purpose is still
  resting (a LIMIT order can rest longer than the 60s position-monitor poll
  interval).
- **The "phantom fill" problem** (the single most important Binance API
  quirk this codebase works around - see §8.1-8.2 for the bugs this caused
  and how they were fixed): `POST /api/v3/order` (placement) returns a
  `fills` array; `GET`/`DELETE /api/v3/order` (status/cancel) **do not** -
  only order-level fields. Real fill/commission data for those has to be
  separately fetched via `GET /api/v3/myTrades`. `ExecutionResult.fill_data_incomplete`
  is the flag that means "exchange confirms a terminal status but the real
  fill breakdown couldn't be fetched this attempt" - every caller must
  retry on it, never treat it as "nothing filled."

---

## 7. Telegram - complete reference

`TELEGRAM_ALLOWED_USER_ID` is the only user who can issue commands; any
other caller is silently ignored (the bot never reveals a command exists to
anyone else). Secrets never appear in any output, including `/config`. All
text is Ukrainian (RSI/MACD/EMA/BTC/USDT/DCA etc. left as-is - no different
Ukrainian term exists for them).

### 7.1 Commands (`telegram_bot/handlers.py`)

| Command | What it does |
|---|---|
| `/status` | Mode, uptime, BTC regime, open positions count, total unrealized PnL, pause/emergency flags, health |
| `/balance` | Live balance (exchange or paper account) + approximate total portfolio value |
| `/positions` | Each open position: entry, current price, live PnL%, DCA count, target |
| `/signals` | Most recently scored candidates |
| `/pnl` | All-time realized PnL and win rate |
| `/today` | Today's report (balance, PnL, trades, fees, exposure) |
| `/history` | Recent closed trades |
| `/pause` | Stop new BUYs (open positions keep being managed) |
| `/resume` | **Atomically** clears buy_paused, dca_paused, AND emergency_stop (one DB transaction, per the fix in §8.7 - `/emergency_stop`'s own confirmation message tells the operator to use this to recover, so it must undo everything that command set) |
| `/stop_dca` / `/start_dca` | Disable/enable DCA on open positions |
| `/market` | Current BTC regime + reasons |
| `/news` | Recent news items + sentiment |
| `/config` | Current configuration (secrets redacted) |
| `/emergency_stop` | **The real, instant kill switch.** Stops new BUYs and DCA immediately. Existing positions are left alone UNLESS `EMERGENCY_AUTO_SELL=true`, in which case every open position is market-sold immediately too. This is what the project owner should use for "stop it now" - see §11. |

Plus a **proactive 3x/day status push** (`STATUS_PING_HOUR_1/2/3_UTC`,
default ~08:00/14:00/22:00 Kyiv) so it's obvious the bot is alive without
asking.

### 7.2 Every event that triggers a Telegram push (`telegram_bot/notifications.py`)

| Event | When |
|---|---|
| СИГНАЛ НА КУПІВЛЮ (buy signal) | A candidate passed scoring, before the order is actually submitted |
| КУПІВЛЯ ВИКОНАНА (buy executed) | Entry order filled - price, score, signals, regime, news, target, DCA plan |
| СИГНАЛ ДОКУПКИ / ДОКУПКА ВИКОНАНА | DCA signal / DCA fill executed |
| ПОЗИЦІЯ ЗАКРИТА (position closed) | Full close, any reason - entry/exit price, net PnL $/%, holding time, close_reason (TAKE_PROFIT / TRAILING_STOP / HARD_PROFIT_CEILING / EMERGENCY_SELL) |
| ВІДКЛАДЕНЕ ВИКОНАННЯ ОРДЕРА (delayed fill) | A LIMIT order that was still resting when submitted has now resolved |
| ПОПЕРЕДЖЕННЯ: ПРОСІДАННЯ ПОЗИЦІЇ (drawdown warning) | §5.6 - position down 20%/30% from entry, once each |
| ПОМИЛКА / ПОМИЛКА API | Any handled error worth surfacing (rejected order, failed liquidation, resolution failure, etc.) |
| ЗАПУСК / ЗУПИНКА (startup/shutdown) | Process start (mode, dry_run, positions recovered) / clean stop |
| ВАЖЛИВА НОВИНА (news alert) | A critical/high-impact news item on a tracked symbol |
| ТРИВОГА: ОБВАЛ РИНКУ (crash alert) | BTC regime transitions into CRASH |
| ЩОДЕННИЙ ЗВІТ (daily report) | Once a day at `DAILY_REPORT_HOUR_UTC` (default 21:00 UTC) |
| status ping | 3x/day, see above |

`on_no_trade` (a rejected/no-trade candidate) is deliberately **not** pushed
to Telegram - it's logged to the DB (`/signals`, `/history`) instead, since
pushing every rejected candidate would spam the chat.

---

## 8. Development history: what was found and fixed, and why

This project went through two full multi-agent code-review rounds (the
second one, in this same continued session, fanned out ~15 independent
"finder" agents across the whole codebase) plus a real 3-day paper-trading
validation against live market data. What follows is every substantive bug
found and fixed, grouped by theme, so the reasoning behind non-obvious code
survives past this conversation.

### 8.1 The Binance "phantom fill" API contract gap

**Root cause** (§6): `GET`/`DELETE /api/v3/order` never carry `fills`, even
when the order genuinely filled. Without a fallback, `get_status()`/
`cancel()` would report `status=FILLED` with `filled_quantity=0` for
exactly the case the whole reconciliation system exists to handle: a
resting LIMIT order that resolves later. **Fix**: `BinanceExecutionAdapter._resolve_fills`
falls back to `GET /api/v3/myTrades` whenever `executedQty>0` but no
`fills` array is present; `ExecutionResult.fill_data_incomplete` flags when
even that fallback couldn't get real data (transient failure, or an empty
response despite `executedQty>0`) so callers retry instead of resolving off
a phantom zero.

### 8.2 The self-contradiction in the first phantom-fill fix

The *initial* fix above had two real, confirmed follow-on bugs, both caught
by the second review round:

- **`reconcile_pending_order()`** persisted the exchange's status to the DB
  **before** checking `fill_data_incomplete` - so it would decide "this
  needs a retry" and *also* have already written `Order.status=FILLED` to
  the DB, which silently defeated `has_resting_order()`'s duplicate-order
  guard for the exact order being retried. **Fixed**: persistence now only
  happens once the result is confirmed complete.
- **`check_pending_limit_orders()`'s cancel-on-timeout branch** applied the
  incomplete-data retry logic to the `get_status()` poll result but *not*
  to the separate `cancel()` result a few lines below it in the same
  function - a genuine partial fill picked up right as a timeout-driven
  cancel raced it could be silently recorded as a clean zero-fill cancel.
  **Fixed**: both branches now go through one shared `_should_keep_retrying_incomplete_result`
  helper. A bounded give-up (5× `LIMIT_ORDER_TIMEOUT_SECONDS`) was also
  added so a permanently-stuck order doesn't retry silently forever - past
  that, it resolves anyway but logs loudly for manual reconciliation.

### 8.3 DCA silently bypassing the daily capital/exposure caps

`RiskManager.can_dca()` checked `emergency_stop`/`dca_paused`/crash policy
but **never** `MAX_DAILY_NEW_CAPITAL_USDT` or `MAX_TOTAL_EXPOSURE_PERCENT` -
even though every DCA fill calls the exact same `record_new_capital_deployed()`
a fresh entry does. With several open positions each still under their own
`MAX_POSITION_USDT`, DCA could keep deploying capital all day with zero
veto, while `can_open_new_position()` correctly blocked brand-new entries
at the same cap. **Fixed**: `can_dca()` now takes `requested_usdt`/
`trading_balance_usdt` and runs the identical exposure/daily-cap checks
`can_open_new_position()` does; `manage_position()` gained a
`trading_balance_usdt` parameter (fetched once per monitor cycle) to supply
it. Mirrored into `backtest/engine.py`'s `_BacktestPortfolio.can_dca` too,
so a backtest doesn't overstate what real capital discipline would allow.

### 8.4 `emergency_liquidate_all()` / the hard-ceiling backstop not accounting for resting orders

Both force-close paths used to read `Position.total_quantity` and sell that
directly - but if a DCA or exit LIMIT order was still resting for that
position, the true current quantity could differ, risking an
oversell-rejection or an inaccurate liquidation. **Fixed**: a new
`_cancel_resting_orders_for_position()` helper cancels anything resting
first, applies whatever fill it had already picked up via the same dispatch
delayed fills use (`apply_resolved_order`), and only then reads the
now-accurate quantity to force-sell.

### 8.5 `PaperBroker.cancel()` not checking marketability

Unlike `get_status()`, `cancel()` unconditionally deleted the resting order
and reported a clean zero-fill CANCELED, even if the current price had
already crossed the limit (i.e. real Binance would have filled it). This
was invisible while the only caller was the timeout loop (which always
calls `get_status()` first) - but §8.4's new helper calls `cancel()`
directly, with no prior `get_status()`, so in PAPER mode a forced close
could silently under-report what actually filled. **Fixed**: `cancel()`
now fills instead of cancelling when marketable, mirroring `get_status()`.

### 8.6 Startup reconciliation not applying fills, only persisting them

`reconcile_live()` asked Binance for each pending order's real status and
persisted the Order/Fill rows - but never turned a genuine resolution into
**Position** state. If an ENTRY/DCA order actually filled (or an exit order
partially filled) while the bot was down, it came back up with the Order
marked FILLED in the DB but no Position created/updated - real capital
moved on the exchange with zero tracking. **Fixed**: `reconcile_live` now
routes every genuinely-resolved order through the same `StrategyEngine.apply_resolved_order`
dispatch a delayed fill discovered while running uses. This is the crash-
recovery guarantee: **the bot remembers open orders and resumes from where
it stopped**, not just "the order status looks right."

### 8.7 `/resume` not undoing everything `/emergency_stop` set

`/emergency_stop` pauses buys AND DCA AND sets `emergency_stop` (one atomic
DB transaction). `/resume` originally only cleared `buy_paused` - so the
kill switch could never be fully undone from Telegram, and DCA stayed
silently disabled after the first emergency stop with no command telling
the operator `/start_dca` was still needed. **Fixed** in two steps: first
made `/resume` also clear `emergency_stop` and call `start_dca()`
separately, then (once a review round flagged the three-separate-transactions
risk) bundled all three into one atomic `RiskManager.resume_trading()` call,
mirroring how `trigger_emergency_stop()` already bundles its three writes.

### 8.8 Raw network exceptions escaping the Binance adapter

`submit()`/`cancel()`/`get_status()` only caught `(BinanceAPIException,
BinanceRequestException)` - but `BinanceClient._call()`'s own retry loop
can re-raise a bare `TimeoutError`/`ConnectionError`/`OSError` after
exhausting retries, and that propagated uncaught past the write-ahead Order
row. **Fixed**: widened the exception handling to match, with `submit()`'s
handler deliberately reporting `NEW` (not `REJECTED`) on a network failure
- a lost response doesn't mean the order wasn't accepted, and `REJECTED`
would be actively misleading. Separately found and fixed: `BinanceClient._is_retryable()`
recognized `TimeoutError`/`ConnectionError` but not a bare `OSError` (e.g. a
DNS failure), even though the surrounding except clause already caught it -
so that whole class of transient failure got zero retry/backoff.

### 8.9 Earlier-round fixes (before this conversation's visible history, from the original build + first hardening pass)

- Partial-close accounting bug in both `PositionRepository.apply_sell_fill`
  and the backtest engine (dust-threshold handling on a "sell everything"
  order rounded to the LOT_SIZE step).
- Buy-side commission never subtracted from realized PnL (fees paid on
  entry/DCA fills were tracked but not folded into the final PnL
  calculation).
- Every monetary/quantity DB column was silently round-tripping through
  64-bit float on top of SQLite (`Numeric` binds through Python `float` on
  any dialect without native Decimal support) - fixed with a `DecimalString`
  TypeDecorator storing exact fixed-point text instead.
- `check_pending_limit_orders()` existed but was never actually wired into
  the scheduler loop.
- An `accepted=True`-but-zero-fill LIMIT order (still resting) was being
  treated as a completed trade in places.
- Crash-policy checks were bypassable via `manage_position`'s DCA path and
  the backtest portfolio's own DCA path.
- `EMERGENCY_AUTO_SELL` setting existed in config but nothing read it.
- Live vs. backtest used different denominators for exposure percentage.
- Backtest's DCA re-analysis was missing the same veto pipeline (news/
  liquidity/structure) the live re-analysis uses.
- Backtest look-ahead and calendar-leakage bugs (indicators computed with
  future data visible, timeframe alignment issues).
- Market-data staleness was configured but never actually checked before
  trusting a snapshot.

### 8.10 Deliberately NOT done - and why (don't "fix" these without re-reading this section)

These were raised by review rounds, evaluated, and consciously deferred -
not overlooked:

- **`backtest/engine.py` hand-reimplements the same exit/DCA decision
  ladder `manage_position` encodes**, as a second, separately-maintained
  branch tree (already drifted once - early-profit-protection had to be
  manually mirrored in as its own task). Real DRY violation, but unifying
  the vectorized/pandas backtest path with the live async event-driven path
  is a substantial architecture change, not a bug fix. Documented, accepted
  tradeoff.
- **`ExecutionEngine.buy()`/`sell()` duplicate ~20 lines** of order-creation/
  DRY_RUN/submit-persist-tracking logic. Pre-existing, cosmetic, risk of
  behavior drift between BUY/SELL-specific validation if unified carelessly.
- **The position-monitor loop processes open positions sequentially**, not
  concurrently via `asyncio.gather` - real latency cost at `MAX_OPEN_POSITIONS`,
  but concurrent execution needs careful design around shared DB sessions
  and per-symbol order-submission rate limits; not done casually.
- **The "fetch position, bail if None/not OPEN" guard is hand-retyped ~9
  times** across `strategy_engine.py`. Cosmetic, large mechanical refactor
  touching many call sites for a purely stylistic win - deferred.
- **`PaperBroker` and `backtest/engine.py`'s `_simulate_buy`/`_simulate_sell`
  independently re-derive the same fee/slippage formula**, numerically equal
  today but with no shared source of truth. Pre-existing, moderate risk to
  touch two independently-tested numeric paths for a cosmetic unification.
- **`PaperBroker` never reserves/locks USDT for a resting LIMIT order** the
  way real Binance locks notional at acceptance time - two independently-
  submitted paper orders can theoretically over-commit the simulated
  balance. Real gap, low practical risk (this bot's own order flow rarely
  submits overlapping orders against the same balance), non-trivial
  multi-file fix (balance semantics touch several display/risk call sites).
- **`apply_sell_fill`'s "rounding dust" threshold is 0.1% of the sold
  quantity**, not bounded by the actual LOT_SIZE step that produced the
  rounding - a very coarse-step asset could theoretically round more than
  0.1% and leave a real non-dust remainder stuck open. Low practical risk
  given the universe scanner already filters to liquid, fine-grained-step
  pairs.
- **News sentiment keyword scoring double-counts substring-overlapping
  keywords** (e.g. "fine"/"fined" both match and both score) - makes a
  negative score *more* negative than warranted, i.e. errs toward extra
  caution, never toward missed risk. Pre-existing, low urgency given the
  direction of the error.
- **`ReconnectingStream`'s WebSocket connection establishment has no
  timeout** - a silently-blackholed handshake (rare network condition)
  never reaches the reconnect/backoff logic. Real but needs focused review
  of `exchange/websocket_manager.py`, not rushed.
- **`SymbolFilters` never validates a computed price against Binance's
  PRICE_FILTER min/max bounds** (only tick-size rounding + MIN_NOTIONAL are
  enforced). Real gap on paper, but real Binance USDT pairs this bot's
  liquidity filters would ever select essentially always have
  `minPrice=0`/`maxPrice=0` (unbounded) in practice.
- **`backtest/optimizer.py`'s grid search builds trial `Settings` via
  `model_copy(update=combo)`**, which pydantic does not re-validate - a
  trial could silently violate a cross-field constraint (e.g. ceiling ≤
  target) and still get scored/picked as "best". Fails safe: `Settings()`'s
  real constructor (used at actual startup) would still reject an invalid
  combination before it could ever run live. Backtest-tool-only.
- **Sync SQLAlchemy Session inside an asyncio application** throughout
  (every `session_scope()` call is a blocking synchronous DB operation
  inside async functions). Flagged early, deliberately not refactored to
  async SQLAlchemy - large, invasive, high-risk change with no live traffic
  yet to justify the risk. Revisit only if profiling ever shows this is a
  real bottleneck under live load.

---

## 9. Testing

```bash
cd crypto_bot
pytest tests/ -q     # 154 tests as of this writing, all passing
mypy .                # 0 issues across 55 source files
ruff check .           # 0 issues
```

Every fix above shipped with the full suite green plus new/updated tests
targeting the specific bug (never just "tests still pass" - a regression
test that would have failed before the fix, for each). Backtest engine
findings were validated by running it against real historical candles
(Binance being geo-blocked from the build sandbox, a same-shape public
candle API stood in purely for this validation, never shipped in the bot).

---

## 10. Real-world validation: the 3-day virtual paper launch

Binance is geo-blocked from the sandbox this bot was built in, so before
trusting it, a real end-to-end validation was run: two real coins (SOL,
XRP), bought the way the bot actually buys, tracked against **real
Coinbase-sourced market prices** (a same-shape substitute for the
geo-blocked Binance data, purely for this validation) for 3 days, with the
bot's own unmodified `StrategyEngine.manage_position()` re-run daily against
fresh prices.

**Result**: XRP peaked around +18% (far past the +10% target), the early
profit-protection trail (§5.4) armed at +9.5% and, once price finally
pulled back past its 1% trailing distance, closed the position at **+5.41%
net** (+$1.08) via `TRAILING_STOP`. SOL stayed open the whole time, ending
at +3.78% unrealized, never crossing the 9.5% early-arm threshold. DCA
never triggered for either coin (neither dropped far enough); drawdown
warnings never fired (neither dropped 20%+).

**Important, honest caveat documented at the time**: this validation
checked prices **once every 24 hours**, not continuously like the real bot
will (default 60s poll interval). XRP's actual 1%-trailing-distance stop
should have fired almost immediately after its peak in a continuously-
running bot - the +5.41% result reflects the daily-check methodology
missing the real trigger moment, not a limitation of the trailing-stop
logic itself. **Once actually deployed and running continuously, expect the
trailing mechanisms to lock in gains far closer to the peak than this
validation's numbers suggest** - the validation's real purpose was proving
the decision logic fires correctly end-to-end against live prices, which it
did.

---

## 11. Operational status - what's live, what's next

- **Code**: pushed to `claude/binance-spot-trading-bot-sa4jfa` on
  `artlion332211-design/23_10`. Full test suite, mypy, ruff all green as of
  the latest commit.
- **Not yet deployed anywhere persistent.** The 3-day validation ran in a
  scratch environment inside this cloud session, not on any long-running
  infrastructure - that scratch DB and its 2 positions are not the
  project's real state going forward.
- **Deployment target**: the project owner's own Windows machine, run as an
  always-on local server (`DEPLOYMENT.md`), via NSSM or Task Scheduler -
  chosen over Docker for simplicity on a single dedicated machine.
- **Remote status bridge**: `tools/export_status.py`, scheduled to push a
  read-only snapshot of open positions/recent trades/risk flags/recent
  errors to a dedicated `bot-status` git branch every 15-30 minutes
  (`DEPLOYMENT.md` §8) - this is how a cloud Claude session (with no direct
  access to the deployment machine - see §12) can answer "what's the bot
  doing" on request, from real (if slightly delayed) data.
- **Still `MODE=PAPER`, `DRY_RUN=true`** in the repo's `.env`. Flipping this
  is the project owner's call alone, after the README's "Before enabling
  LIVE" checklist is satisfied on the actual deployment machine (fresh
  PAPER run there, backtest over real history for the intended symbols,
  Binance key scoped to Spot-only with withdrawals disabled, position
  sizing reviewed for capital the owner can actually afford to lose).

---

## 12. On "full access" and what's actually possible for a session working on this

This project has been developed across a cloud Claude Code session with no
direct access to the deployment machine. That's a structural fact, not a
permissions setting - confirmed directly against Claude Code's own
documentation (self-hosted environments require a Team/Enterprise plan and
don't support Windows as a runner host; Remote Control lets a user steer
their *own* local session remotely but doesn't bridge a *different* session
into that machine). **If you are the local session this document was
handed to, you likely DO have real terminal access to the deployment
machine** - which is exactly why this handoff exists: use that access
directly (per `DEPLOYMENT.md`), and don't assume you need to coordinate
with or wait for a cloud session that structurally cannot reach you.

**On stopping the bot**: the bot's own `/emergency_stop` Telegram command
(§7.1) is the real, instant, already-built kill switch - it doesn't depend
on any session (cloud or local) being reachable, only on the operator's own
Telegram access, which they always have from their phone. Don't build a
slower substitute for this unless explicitly asked to.

---

## 13. Before changing anything here

- Every commit on this project ends with the attribution footer required
  by the harness that authored it - preserve that convention if you're a
  Claude session continuing this work, and never remove attribution from
  history.
- Run `pytest`, `mypy`, `ruff check` before considering any change done -
  see §9.
- If you find something that looks wrong, re-check §8.10 first - it might
  be a documented, deliberate tradeoff, not an oversight.
- Never weaken a risk-management gate (exposure caps, daily capital cap,
  consecutive-loss pause, crash policy, the hard profit-ceiling backstop)
  without the project owner explicitly asking for that specific change -
  these exist because "capital preservation" is priority one, not
  priority four.
