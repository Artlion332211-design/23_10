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

**12. "No signals for hours" with all position slots full (not a bug).** With
`MAX_OPEN_POSITIONS` reached, entry evaluation returns before scoring, so the
`signals` table gets no rows per candle; DCA scoring writes rows only while a
position sits at a DCA level. Lesson: the "25 per candle" health check only
applies while a slot is free - otherwise check the kline stream, errors.log
and the position monitor.

## 2026-10-01

**13. Code review of the first live days - fixed before any of it bit
(deployed 03:32, commit b2fb2f8, positions verified unchanged).** Main finds:
a cancel that Binance couldn't confirm (network blip, key/IP rejection) left
the order unpolled, which would have frozen that position's exits and DCA
until a restart; a force-close could sell on top of a resting SELL whose
cancel wasn't confirmed; `/emergency_stop` could interleave with the
monitor or the order poll and sell the same coins twice, or wait minutes
behind a slow poll; DCA re-scoring every minute wrote ~40 NO_TRADE rows per
candle; the backtest counted sell orders instead of positions. Lessons: an
unconfirmed cancel means "unknown", never "cancelled"; exactly one code path
may apply each fill, and every other path must check whether it already
happened; keep network calls outside locks.

**14. A test failed every night between 00:00 and 01:00 UTC.** The
daily-report test built "an hour ago" from the wall clock, which is
yesterday in that hour. It would have blocked a night-time deploy (tests run
on the server first). Lesson: tests must never depend on the time of day.

## 2026-10-03

**15. Review of the deployed /sell and 50 USDT entry (9b64255) - fixed before
any of it bit.** Found by a post-deploy review, none happened in production:
an edited Telegram message re-ran its command (editing an old "/sell AAVE"
into "/sell AAVE так" sold at once; an edited old /emergency_stop would have
fired again); "/sell AAVE так" sold without the prompt that shows the
position; a sale whose response was lost was reported as "біржа відхилила"
although it may have filled, and an EXPIRED sale with no fill as "not
confirmed yet"; the 24 h no-re-buy cooldown was written by the Telegram
path only, so a fill the order poll resolved later never set it, and DCA
could still average into what a partial sale left; an exception after
"Продаю..." left the owner without any answer; a long sale blocked every
other command (including /emergency_stop) behind it; a STRONG_SIGNAL setting
that didn't fit the position cap stopped the bot at startup. Lessons: an
"outcome unknown" order must be checked against what the exchange holds
before telling the owner anything; a rule tied to a fill belongs in the
transaction that applies the fill; Telegram edits are new commands unless
filtered; an optional extra must never be able to stop a live bot.

**16. Claude's own slips while building the 2026-10-03 changes (caught by
tests, nothing deployed).** Python edits sent through a bash heredoc turned
`\n` inside f-strings into real line breaks (SyntaxError in the Telegram
handlers); and a scratch script name collided with an older one, so the old
mutation script ran first (harmless - it restores every file byte for
byte). Lessons: write multi-line code edits with the editor tool or a script
file, never a heredoc with backslashes; give scratch scripts unique names.

## 2026-10-08

**17. "Realized PnL +0.00 although trades closed in profit" (not a bug).** The
daily report for 2026-10-07 showed +0.00 realized; the owner expected the
profit of the trades closed on 10-02 and 10-06. The figure was right - it
covers that one day, and nothing closed that day - but nothing said so.
Fixed in 190660d: the line reads "за день" and a new line shows the month's
closed trades (PnL, count, wins), also in /today. Lesson: a number that is
right but easy to misread is a defect of the message.

**18. Main-screen buttons: STOP confirmation could be swallowed (caught by
review before deploy).** The confirm handler marked the prompt as used and
removed its buttons before running the action. If Telegram had failed that
edit for a moment, the emergency stop would not have run and a second tap
would have been ignored silently. The action now runs first, the button
removal is best-effort, and a repeat tap answers "Вже виконано". Lesson:
the safety action comes first, cosmetics after, and a click must never get
silence.

**19. Claude repeated the heredoc slip from #16 twice in one day (caught
before anything ran).** Two edit scripts sent through a bash heredoc lost
their `\n` again; both failed on their own checks, nothing was written.
Lesson, now a hard habit: every code or test edit goes through a script
file written with the editor tool, never a heredoc.

**20. Live-data files were one `git add -A` away from the public repo.** A
copy of the live DB (backups/), a scratch pickle and, on the server, the live
DB's WAL/SHM files were not ignored. Never committed (files are always
staged by name), now ignored (9b86663) with the owner's OK.
