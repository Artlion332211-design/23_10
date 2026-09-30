# Incident log

Every production problem, newest last: symptom, root cause, fix, lesson.
The owner asked explicitly that mistakes be recorded and learned from - read
this before diagnosing a new problem (it may be a known pattern), and append
an entry after fixing one. Include our own mistakes, not only exchange or
network failures. Never put secrets, IP addresses or account data here: the
repository is public.

## 2026-09-29 - first day (PAPER from 14:55, LIVE from 18:26, on a Windows laptop)

**1. WebSocket queue overflow at every candle close.**
Symptom: `BinanceWebsocketQueueOverflow (100)` at :00; only 5 of 25 symbols
evaluated. Cause: entry evaluation (REST + DB + indicators) was awaited inline
in the kline read loop. Fix: queue + one serialized worker, socket queue 2000.
Lesson: never do slow work inside a WebSocket callback; check "evaluated N of
N" per candle in the `signals` table.

**2. The bot never bought: 105/105 evaluations BLOCKED by "critical news".**
Cause: a headline about a hack of another exchange named none of the
candidates, got tagged `MARKET`, and a critical `MARKET` item vetoed every
symbol for 48h; keywords also matched substrings ("hackathon" -> "hack").
Fix: only symbol-specific (or Binance-related market-wide) critical news
hard-blocks; whole-word keyword matching. Lesson: after any run, look at the
distribution of decision reasons - one reason dominating is a bug, not the
market.

**3. Home Wi-Fi / DNS outages** (three in one evening, 1-10 minutes each:
`getaddrinfo failed`, Telegram polling errors). The bot recovered by itself
each time. Lesson: a laptop on Wi-Fi is a weak server - one reason for the
move to a droplet.

**4. Our regression: the new entry worker had no exception guard.** A DB
error in the evaluation pre-checks would kill it and burn watchdog restarts.
Fix + test. Lesson: every new long-running loop needs its own guard and test.

**5. Our regression: `/status` said "ПРОБЛЕМА - немає зв'язку" right after a
restart.** Telegram answers before `runtime.initialize()` starts the stream;
the watchdog's lifetime restart counter also produced permanent alarms. Fix:
"starting" state, 60s grace for planned reconnects, recovered restarts are
not problems. Lesson: test health reporting for startup, planned reconnects
and recovered-transient states before shipping it.

**6. "No trades" in a strong uptrend - not a bug.** Momentum signals are
reversal-only (see HANDOFF §3.1). 255 evaluations: RSI reversal 0, Bollinger
0, MACD 1; a 30-day backtest gave ~1 trade per 2 days. Lesson: before calling
"no trades" a bug, count per-signal fire rates and run a backtest.

**7. LIVE: every signed request failed with -1021 "Timestamp outside
recvWindow"** (25 error messages per candle). Cause: python-binance measures
the local-vs-server clock offset once at startup; the OS corrected its clock
later, making the stored offset wrong. Fix: resync the offset on -1021 and
retry (safe even for orders). Lesson: anything measured once at startup can
go stale on a 24/7 box.

**8. LIVE: price feed dead ~2 minutes after a Wi-Fi drop, then
`BinanceWebsocketQueueOverflow (2000)`.** Cause: python-binance keeps its own
read loop reconnecting after transient errors; our code tore the socket down
on those error payloads, and `__aexit__` then waited for a read loop that a
successful internal reconnect had revived, while nobody consumed the queue.
Fix: leave transient errors to the library, tear down only on fatal ones;
inactivity timeout raised to 300s. Lesson: understand the library's own
reconnect logic before layering a supervisor on top - two reconnect
mechanisms fighting is worse than one.

## 2026-09-30

**9. LIVE: -2015 "Invalid API-key, IP, or permissions" on every signed call
from 06:31.** Cause: the home ISP changed the laptop's public IP; the API key
is IP-whitelisted (Binance requires that for trading keys). No positions were
open. Fix: owner added the new IP; the alert now includes the machine's
current public IP; errors are de-duplicated and explained in plain language
(both #7 and #9 had sent 25 raw exception dumps per candle). Lesson: on
-2015 check the public IP first; a dynamic home IP cannot host an
IP-whitelisted trading key long-term -> moved to a droplet with a static IP.

**10. Low memory on the 8 GB laptop.** Claude Code reaped a background job;
the browser and a desktop app were the big consumers (the bot itself ~220 MB).
Lesson: a shared personal laptop is a poor 24/7 server.

**11. Migration to a DigitalOcean droplet (10:06).** Not an incident - the
fix for #3, #7, #9 and #10. Procedure that worked: harden the server, clone,
run the full test suite on it, verify Binance answers from the server IP
(not HTTP 451) and accepts the key, stop the old instance and neutralize its
`.env` before starting the new one (never two instances), copy the DB with
the SQLite backup API, then verify startup, streams and the next candle.
Note for DigitalOcean: outbound traffic leaves from the droplet's own IP,
not from a Reserved IP - whitelist the droplet IP on Binance.
