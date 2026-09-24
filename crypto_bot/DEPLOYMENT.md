# Deploying on a local Windows machine as a 24/7 server

This is the operational companion to the main [README](README.md) (setup,
architecture, Telegram commands, the "Before enabling LIVE" checklist all
live there). This document covers only what's specific to running the bot
**unattended, long-term, on a Windows machine you're repurposing as a home
server**.

The bot itself is platform-independent (pure Python + SQLite); the only
Windows-specific part is *how the OS keeps it running* - Windows has no
`systemd`, so a plain `python app.py` in a terminal dies the moment you log
out or the machine reboots. This guide wraps it in a proper background
service instead.

## 1. Prerequisites

- **Windows 10/11** (or Windows Server), with the machine set to stay
  powered on and connected to the internet continuously (see [§6](#6-keep-the-machine-actually-always-on)
  - a laptop that sleeps on lid-close will silently kill the bot).
- **Python 3.11+** from [python.org](https://www.python.org/downloads/windows/)
  - during install, check **"Add python.exe to PATH"**.
- **Git for Windows** ([git-scm.com](https://git-scm.com/download/win)), if you're
  cloning the repo rather than copying files over some other way.
- A **Binance API key** with *Spot & Margin Trading -> "Enable Spot & Margin
  Trading"* only - withdrawal permission must stay disabled.
- A **Telegram bot token** (from [@BotFather](https://t.me/BotFather)) and
  your own numeric Telegram user id (from [@userinfobot](https://t.me/userinfobot)
  or similar).

Run everything below in **PowerShell** (Start menu -> "PowerShell"), not the
old `cmd.exe` - the commands here assume PowerShell syntax.

## 2. Get the code and install dependencies

```powershell
cd C:\
git clone <this repo's URL> crypto_bot_deploy
cd crypto_bot_deploy\crypto_bot

py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If `Activate.ps1` is blocked by PowerShell's execution policy, run once (as
the same user that will run the bot):

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## 3. Configure `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

Fill in `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ALLOWED_USER_ID`. Leave `MODE=PAPER` and `DRY_RUN=true` (the
defaults) until you've read the README's **"Before enabling LIVE"**
checklist and are deliberately ready to change them.

`.env` holds real secrets - it's already covered by the repo's root
`.gitignore`, so `git status` should never show it as a change to commit.
Keep it that way; never paste its contents anywhere.

## 4. First run: manual, in the foreground

Before wrapping anything in a service, run it directly once so you can see
what it does and catch any config mistake with a real error message in
front of you, not buried in a service log:

```powershell
cd C:\crypto_bot_deploy\crypto_bot
.venv\Scripts\Activate.ps1
python app.py
```

You should see startup log lines and a Telegram "ЗАПУСК" (startup) message
land in your chat with the bot within a few seconds. Try `/status` from
Telegram. Stop it with `Ctrl+C` once you're satisfied - it shuts down
cleanly and sends a Telegram "ЗУПИНКА" notice.

## 5. Running as a persistent Windows service

Two options. **NSSM is the recommended one** - it's the closest Windows
equivalent to `systemd`: proper service semantics, auto-restart on crash,
starts before any user logs in, stdout/stderr captured to files.

### Option A - NSSM (recommended)

[NSSM](https://nssm.cc/) ("the Non-Sucking Service Manager") wraps any
executable - including `python.exe` - into a real Windows service. It's a
small, well-established, long-standing tool; download it from
**nssm.cc** and verify the download yourself before running it, as with any
third-party binary.

```powershell
# after downloading and unzipping nssm, from an elevated (Run as Administrator) PowerShell:
cd C:\path\to\nssm\win64
.\nssm.exe install CryptoBot
```

This opens a GUI. Fill in:

| Field | Value |
|---|---|
| Path | `C:\crypto_bot_deploy\crypto_bot\.venv\Scripts\python.exe` |
| Startup directory | `C:\crypto_bot_deploy\crypto_bot` |
| Arguments | `app.py` |

On the **Details** tab, set Startup type to **Automatic**. On the **I/O**
tab, redirect stdout and stderr to e.g.
`C:\crypto_bot_deploy\crypto_bot\logs\service_stdout.log` and
`...\service_stderr.log` (in addition to the bot's own `logs/` files - this
also captures anything printed before logging is configured, or a raw
Python traceback if the process dies outright). On the **Exit actions**
tab, set the default action to **Restart application** - this is what
covers a crash of the *whole process*, on top of the bot's own internal
Watchdog which already restarts individual crashed loops (websocket feed,
position monitor, etc.) without needing the OS's help; the two are
complementary, not redundant.

Then:

```powershell
nssm start CryptoBot
```

Manage it afterwards with ordinary Windows service tools:

```powershell
nssm status CryptoBot
nssm stop CryptoBot
nssm restart CryptoBot
# or: Get-Service CryptoBot | Start-Service / Stop-Service / Restart-Service
```

To remove the service later:

```powershell
nssm stop CryptoBot
nssm remove CryptoBot confirm
```

### Option B - Task Scheduler (no extra download)

If you'd rather not install a third-party tool, Task Scheduler can run the
bot at startup, though its crash-restart handling is less immediate than a
real service's.

1. Open **Task Scheduler** -> **Create Task** (not "Basic Task", so you get
   the full options).
2. **General** tab: name it `CryptoBot`. Select **"Run whether user is
   logged on or not"**. Check **"Run with highest privileges"** only if you
   have a specific reason to.
3. **Triggers** tab -> **New** -> **Begin the task: At startup**.
4. **Actions** tab -> **New** -> **Start a program**:
   - Program/script: `C:\crypto_bot_deploy\crypto_bot\.venv\Scripts\python.exe`
   - Add arguments: `app.py`
   - Start in: `C:\crypto_bot_deploy\crypto_bot`
5. **Settings** tab: check **"If the task fails, restart every"** and pick
   e.g. 1 minute, with a high or unlimited restart count. Uncheck "Stop the
   task if it runs longer than" (the bot is meant to run indefinitely).
6. Save, then right-click the task -> **Run** to start it immediately
   without waiting for the next reboot.

Task Scheduler gives no live stdout capture by default - rely on the bot's
own `logs/` files (see below) to see what it's doing.

## 6. Keep the machine actually always-on

A "server" that goes to sleep isn't one. On the machine that will run this:

- **Power & sleep settings** (Settings -> System -> Power & battery): set
  "Screen and sleep" to **Never** while plugged in.
- If it's a laptop: **Settings -> System -> Power & battery -> lid-close
  action -> "Do nothing"** (closing the lid otherwise suspends everything,
  service included).
- Disable automatic Windows Update restarts during your bot's active hours,
  or at minimum confirm your service is set to start automatically after
  one (Options A and B above both already are) - an unattended reboot is
  routine on Windows and the bot must come back up on its own.

## 7. Logs & monitoring

The bot writes its own rotating logs to `logs/` (set by `LOG_DIR`/`LOG_LEVEL`
in `.env`) regardless of how it's launched. To watch them live in
PowerShell:

```powershell
Get-Content C:\crypto_bot_deploy\crypto_bot\logs\*.log -Wait -Tail 50
```

The bot also proactively pushes a status summary to Telegram three times a
day (`STATUS_PING_HOUR_*_UTC` in `.env`) specifically so you don't have to
watch logs to know it's alive - `/status` works on demand too.

## 8. Updating the bot

```powershell
cd C:\crypto_bot_deploy\crypto_bot
nssm stop CryptoBot          # or stop the Task Scheduler task
git pull
.venv\Scripts\Activate.ps1
pip install -r requirements.txt   # in case dependencies changed
nssm start CryptoBot
```

Back up `data\crypto_bot.db` (see below) before pulling an update you
haven't reviewed, in case a schema migration needs to be rolled back
manually.

## 9. Backing up the database

All position/order/PnL history lives in one SQLite file:
`data\crypto_bot.db`. It's excluded from git on purpose (it's runtime
state, not code) - back it up yourself:

```powershell
Copy-Item C:\crypto_bot_deploy\crypto_bot\data\crypto_bot.db `
  "C:\crypto_bot_deploy\backups\crypto_bot_$(Get-Date -Format yyyy-MM-dd_HHmm).db"
```

Consider a scheduled daily copy via the same Task Scheduler, independent of
whether you chose NSSM or Task Scheduler to run the bot itself.

## 10. Before you flip this to real money

This document is only about *keeping the process running*. Read the
README's **"Before enabling LIVE"** checklist in full before ever setting
`MODE=LIVE` and `DRY_RUN=false` - it's a completed prerequisite, not a
Windows-specific one, and nothing here substitutes for it.
