# Manual: running the trading agent

Everything you need to run the agent on the Zenbook, in one place: setup, start and stop, updates, where the dashboard is, Telegram, your daily routine, settings, tests, troubleshooting and backups. What the agent does and when is in [HOW-IT-WORKS.md](HOW-IT-WORKS.md); the code is in [CODE.md](CODE.md).

> **Where you are (2026-11-05):** the Zenbook is prepared, the app is installed and the IBKR paper gateway has logged in (October 2026). Still to do: the update to the simplified version (5.4), the `FINNHUB_API_KEY` (3.3), backups (12) and the tests (10).

Every command runs on the Zenbook, as your normal user, in the app's folder:

```sh
cd ~/projects/trading-agent     # code, .env, secrets/, backups/
```

**Contents**

1. [Quick reference](#1-quick-reference)
2. [What you need](#2-what-you-need)
3. [Setting up (once)](#3-setting-up-once)
4. [First start and checks](#4-first-start-and-checks)
5. [Starting, stopping, updating](#5-starting-stopping-updating)
6. [The dashboard](#6-the-dashboard)
7. [Telegram and the heartbeat](#7-telegram-and-the-heartbeat)
8. [Your daily routine](#8-your-daily-routine)
9. [Settings you can change](#9-settings-you-can-change)
10. [Testing that everything works](#10-testing-that-everything-works)
11. [Logs, audit trail, troubleshooting](#11-logs-audit-trail-troubleshooting)
12. [Backups and moving to another machine](#12-backups-and-moving-to-another-machine)
13. [Reference: the parts and the logins](#13-reference-the-parts-and-the-logins)

---

## 1. Quick reference

| You want to | Do |
|---|---|
| **Start** (also after every reboot and every update) | `make up`. It builds the image from the code you have, updates the database and starts everything. Then `/status` in Telegram. |
| **Update** to the latest code | `git pull && make up`, outside trading sessions (5.3) |
| **Stop** | `make down`, outside trading sessions if possible (5.2) |
| **Open the dashboard** on the Zenbook | **http://localhost:8501** |
| **Open the dashboard** on the phone | `https://klaus-ux301laa.<your-tailnet>.ts.net`, with Tailscale on (6.1) |
| See what happened to a stock today | Dashboard → **Day** → pick the day, then the stock (6.2) |
| See what is held, and when and why something was sold | Dashboard → **Portfolio**; Telegram `/positions`, `/pnl` |
| Check that everything runs | Telegram `/status` |
| Pause new trades / stop everything | `/pause`, `/stop` in Telegram (7.2) |
| Sell one position | `/exit SYMBOL` in Telegram |
| Watch the agent's log | `make logs` (11.1) |
| Change a setting | `.env` or `config/*.yaml`, then `make up` (9) |

The agent and the IB Gateway **never start by themselves**, not after a reboot (including the automatic one at 04:00) and not after a crash. Only `make up` starts them. The database and the dashboard come back on their own.

---

## 2. What you need

### 2.1 The machine

| | |
|---|---|
| Computer | Asus Zenbook UX301LA (x86_64, 8 GB RAM), always plugged in, never sleeping, online |
| OS | Ubuntu 24.04 LTS or newer (desktop install is fine) |
| Disk | At least 15 GB free |
| Memory use | About 2 GB for the app, 3.5 GB with the IB Gateway; a desktop and a browser fit next to it |

### 2.2 Accounts and keys

| What | Needed for | Required? | Where to get it | Goes into |
|---|---|---|---|---|
| IBKR account with a **paper** login | Orders (the gateway logs in to the paper account) | Yes | IBKR Client Portal → Settings → Paper Trading Account | `.env` `TWS_USERID`, `make tws-password` |
| IBKR app on your phone | Confirming the gateway's 2FA prompt (about once a week) | Yes | App store | |
| Telegram bot | All alerts and commands | Yes | `@BotFather` → `/newbot` | `.env` `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_CHAT_ID` |
| Gemini API key | The AI agents | Yes | aistudio.google.com → Get API key | `.env` `GEMINI_API_KEY` |
| Finnhub API key | News for US stocks (buy signals and news exits) | Strongly recommended | finnhub.io (free) | `.env` `FINNHUB_API_KEY` |
| FRED API key | US macro data for the proposer | Recommended | fred.stlouisfed.org → My Account → API Keys (free) | `.env` `FRED_API_KEY` |
| healthchecks.io check | An e-mail when the Zenbook or the agent dies | Recommended | healthchecks.io → new check, period 5 min, grace 15 min | `.env` `HEARTBEAT_URL` |
| Tailscale | The dashboard from your phone | Recommended | tailscale.com (free), app on the phone | |
| Langfuse | AI traces, step by step | Optional | cloud.langfuse.com (EU, free) | `make trace-headers`, `.env` `TRACE_UI_URL` |
| Anthropic API key | The critic on Claude instead of Gemini | Optional | console.anthropic.com | `.env` `ANTHROPIC_API_KEY`, then `LLM_DEV_OVERRIDES=false` |
| age key pair | Encrypted offsite backups | Recommended | `age-keygen` (12.1) | `.env` `BACKUP_AGE_RECIPIENT`; private key only in your password manager |
| GitHub deploy key | Downloading the code | Yes | `ssh-keygen` on the Zenbook, public key under GitHub → repo → Settings → Deploy keys (read-only) | |

EU news comes from Yahoo and needs no key. Market prices come from Yahoo and the ECB (no keys).

---

## 3. Setting up (once)

### 3.1 Prepare the Zenbook

Done on your Zenbook in October 2026. For reference, or for a fresh install, in a terminal as your normal user:

```sh
# Basics and the time zone
sudo apt update && sudo apt install -y git make openssl curl age unattended-upgrades
sudo timedatectl set-timezone Europe/Berlin            # check: timedatectl shows NTP active

# Never sleep (also with the lid closed)
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
sudo nano /etc/systemd/logind.conf
#   HandleLidSwitch=ignore
#   HandleLidSwitchExternalPower=ignore
#   HandleLidSwitchDocked=ignore
# then reboot

# Docker Engine and Compose from Docker's apt repository (not Docker Desktop, not the snap)
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER                           # log out and in again
echo '{"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "5"}}' | sudo tee /etc/docker/daemon.json
sudo systemctl enable --now docker && sudo systemctl restart docker

# A 4 GB swap file (if `swapon --show` shows less)
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Tailscale and the firewall: nothing reachable from outside except through Tailscale
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
sudo apt install -y openssh-server
sudo ufw default deny incoming && sudo ufw allow in on tailscale0 && sudo ufw enable

# Security updates with an automatic reboot at 04:00 when needed
sudo dpkg-reconfigure -plow unattended-upgrades
#   in /etc/apt/apt.conf.d/50unattended-upgrades:
#   Unattended-Upgrade::Automatic-Reboot "true";
#   Unattended-Upgrade::Automatic-Reboot-Time "04:00";
```

### 3.2 Code and passwords

```sh
ssh-keygen -t ed25519                   # add ~/.ssh/id_ed25519.pub as a read-only deploy key on GitHub
git clone git@github.com:klausrossmann/trading-agent.git ~/projects/trading-agent
cd ~/projects/trading-agent && git config pull.ff only
make secrets                            # random database and VNC passwords in secrets/
make tws-password                       # type your IBKR paper password (not shown)
```

### 3.3 Settings: `.env`

```sh
cp .env.example .env && chmod 600 .env && nano .env
```

Write each value directly after `=`, without quotes, and never put a comment on the same line (Docker Compose would read it as part of the value).

| Key | Value |
|---|---|
| `APP_MODE` | `paper` |
| `TWS_USERID` | Your IBKR username (the gateway logs in in paper mode) |
| `TELEGRAM_BOT_TOKEN` | From `@BotFather` |
| `TELEGRAM_OWNER_CHAT_ID` | Empty for now; filled in at the first start (section 4) |
| `GEMINI_API_KEY` | Your Gemini key |
| `LLM_DEV_OVERRIDES` | `true` while there is no Anthropic key (the critic runs on Gemini) |
| `FINNHUB_API_KEY` | Your Finnhub key (without it: no news buys or exits for US stocks) |
| `FRED_API_KEY` | Your FRED key |
| `HEARTBEAT_URL` | The healthchecks.io ping URL, `https://hc-ping.com/<uuid>` |
| `TRACE_UI_URL` | Optional: `https://cloud.langfuse.com/project/<id>` |
| `DASHBOARD_UI_URL` | Optional: the dashboard's address, for links in `/status` |
| `BACKUP_AGE_RECIPIENT`, `BACKUP_HEARTBEAT_URL` | Section 12.1 |

Leave the rest as in `.env.example` (`LLM_FAKE=false`, `IB_HOST`, `IB_PORT=4004`, `IB_CLIENT_ID=11`, the `DB_*` values). There is no switch for the broker: every order goes to the IBKR account the gateway is logged in to. All `.env` keys are explained in 9.2.

### 3.4 Build, database, history

```sh
make up                                                 # builds the image (a few minutes the first time), creates the tables, starts everything
docker compose run --rm agent trading-agent backfill    # 6 years of prices, FX, macro, earnings (several minutes)
docker compose run --rm agent trading-agent backfill    # again: must report no changes
docker compose run --rm agent trading-agent quality     # data check, ideally "0 blocked symbols"
```

---

## 4. First start and checks

| # | Do | Expect |
|---|---|---|
| 1 | `make up` | Four containers start; at the end it prints the dashboard address |
| 2 | `docker compose logs -f ib-gateway \| grep -v "Connection refused"` | `Login has completed` within 30–90 s; confirm the 2FA prompt in the IBKR app if one comes |
| 3 | Send your bot any message, then `docker compose logs agent \| grep telegram.ignored` | Your `chat_id`; put it into `.env` as `TELEGRAM_OWNER_CHAT_ID`, then `make up` |
| 4 | Telegram `/status` | Mode `paper`, kill switch `active`, data dates, next jobs, `IB Gateway: connected`, heartbeat ok |
| 5 | Open **http://localhost:8501** on the Zenbook | The dashboard, **Day** page |
| 6 | `sudo tailscale serve --bg 8501`, then `tailscale serve status` | The dashboard's `https://...ts.net` address; open it on the phone with Tailscale on and bookmark it |
| 7 | `docker compose run --rm -e IB_CLIENT_ID=12 agent trading-agent ibkr-check` | Account values, the positions IBKR holds, no contracts without an id |
| 8 | Optional, Langfuse: `make trace-headers` (paste the public and secret key), `TRACE_UI_URL` in `.env`, `make up` | `/status`: `LLM tracing: on` |

Then work through the tests in section 10.

---

## 5. Starting, stopping, updating

### 5.1 Start

`make up` does everything, every time:

1. builds the Docker image from the code in the folder (under a minute when little changed),
2. starts the database and applies any database update (migration; does nothing if there is none),
3. starts or replaces the containers `db`, `ib-gateway`, `agent` and `dashboard`,
4. prints where the dashboard is.

Then check:

| Step | Command | Expect |
|---|---|---|
| 1 | `docker compose ps` | `db`, `agent`, `dashboard`, `ib-gateway` all `Up`. `Restarting` means it crashes again and again (11.3). |
| 2 | `docker compose logs -f ib-gateway \| grep -v "Connection refused"` | `Login has completed` within 30–90 seconds; confirm a 2FA prompt on the phone. "Connection refused" is normal until then. |
| 3 | `make logs` | `agent.started`, `scheduler.planned`, `ibkr.connected` (one `ibkr.connect_failed` before it is normal) |
| 4 | Telegram `/status` | Mode `paper`, kill switch `active`, current data dates, next jobs, `IB Gateway: connected` |

**Start before 07:45** on a trading day to get the full EU day, and before 14:15 for the full US day. Jobs whose time has passed by more than a few minutes are skipped; a missed placement is never made up later ([HOW-IT-WORKS.md](HOW-IT-WORKS.md) 9.4).

### 5.2 Stop

`make down` stops and removes the containers; the database, `.env`, `secrets/` and `backups/` stay. Do it outside trading sessions: orders already at IBKR (buy limits, stops, targets) stay active there, but while the agent is down nothing watches them, moves stops or sells by time stop or news.

Restart only the agent: `docker compose restart agent` (keeps the old `.env`; after changing `.env` use `make up`).

### 5.3 Update

When new code is on GitHub and its CI run is green (github.com/klausrossmann/trading-agent/actions), outside trading sessions or after `/pause`:

```sh
git pull && make up
```

Then `/status`, and `/resume` if you paused. A restart triggers a reconciliation with IBKR, which is why updates happen outside sessions. `config/` and `prompts/` are part of the image, so a change there also needs `make up`. Only documentation changed: `git pull` is enough.

### 5.4 The first update to the simplified version (November 2026)

1. Wait until no order is pending and ideally nothing is held (`/positions`), or do it in the evening after 23:05.
2. In `.env`, delete the lines `IB_ENABLED`, `IB_ORDERS_ENABLED`, `IB_READ_ONLY_API` and `COMPOSE_PROFILES` (they no longer exist), and add `FINNHUB_API_KEY` if missing.
3. `git pull && make up`. The database updates add the step log (`0013`) and move the old simulator's positions and trades to the archive book `sim_archive` (`0014`). From then on the dashboard shows only what was ordered at IBKR.
4. The gateway now always starts with `make up` and accepts orders: the first placement after the next scan goes to the IBKR paper account.
5. Check: `/status` (gateway connected, kill switch active, no ⚠️ about the reconciliation). The dashboard shows the new **Day** page; it fills from 07:45 on the next trading day.

If you started the old version with `make up` alone after pulling (before November 2026 `make up` didn't build), the dashboard still showed the old pages and the simulator's positions. Running `make up` again with the current code fixes both.

---

## 6. The dashboard

A website on the Zenbook that only shows data and can't change anything. It works whether the agent runs or not.

### 6.1 Where it is

| From | Address |
|---|---|
| The Zenbook itself | **http://localhost:8501** |
| Phone, tablet or another computer, with the Tailscale app on and logged in to the same account | `https://klaus-ux301laa.<your-tailnet>.ts.net` |

`<your-tailnet>` is a name Tailscale gave your account, like `tail1a2b3c`. To get the exact address:

- On the Zenbook: `tailscale serve status`. The first line is the address.
- In the Tailscale app on the phone: the device list → `klaus-ux301laa` → its name ending in `.ts.net`.
- In the browser: login.tailscale.com → Machines → `klaus-ux301laa`.

If `tailscale serve status` prints nothing, the dashboard isn't published yet: run `sudo tailscale serve --bg 8501` once (it survives reboots). There's no login: only devices in your Tailscale network can reach it; nothing else on the network or the internet can.

### 6.2 The pages

| Page | Shows | At the start (no trades yet) |
|---|---|---|
| **Day** (opens first) | Pick a **day** and a market. **Stocks past each phase**: how many stocks were left after the screen, technical analyst, earnings analyst, proposer, critic, ranking, risk engine, IBKR order and fill. **Stocks**: one row per stock, one column per phase, each with ✓ passed, ✗ stopped (and why), ⚠ error; the first column, **news**, shows the News agent's buy signals and its verdicts on held stocks. Filter e.g. "stopped at critic" or "bought". **What happened to**: pick a stock to see every step with its time, **input and output**: the screen's indicator values, each agent's input, answer and attempts (prompts and replies), the risk engine's 9 checks, the orders as sent to IBKR with IBKR's reply, order reference and any error, fills, stop moves and the sale. "Follow this trade over every day" adds the later days. **Data freshness**: the latest date of every dataset. | "Nothing recorded" until the first scan (08:15) |
| **Portfolio** | **Held at IBKR**: quantity, entry, current stop (and whether it moved), target, last close, R now, P&L in EUR, day bought, sessions held, time-stop day. **Buy orders waiting**. **Sold**: when, why in words (target, stop, stop after it was raised, time stop, news, `/exit`), result in EUR and R, fees. **Results** and the realized P&L curve. **A trade, step by step**: the whole timeline of any trade. | "No open positions.", "Nothing sold yet." |
| **Costs** | AI spend per month, role and model, and per day; broker fees | Empty until the first AI call |
| **Risk** | Kill switch; how much of each limit is used (drawdown, day and week loss, positions, sector); correlation of the holdings; the risk engine's rejections of the last 30 days | Kill switch `active`; limits after the first end of day |

**How it fills:** the scans (08:15 EU, 14:45 US) fill Day and Costs; the placements (09:15, 15:45) add the risk and order steps; IBKR's fills, stop moves and sales follow during the day and in the evening, and appear in Portfolio. Data is cached for 60 seconds; **Reload data** in the sidebar fetches it at once.

**Daily one-minute check:** Day → Data freshness (are the dates current?), the order column (any ⚠?) and Risk (kill switch `active`).

### 6.3 Where to find what

| You want to know | Look here |
|---|---|
| What did the agent propose today? | Telegram 🧠, `/proposals`; dashboard → **Day** |
| Why did it propose (or skip) a stock? | Telegram `/why SYMBOL`; dashboard → **Day** → pick the stock |
| Exactly what an AI agent was asked and answered | Dashboard → **Day** → the stock → open the agent's step: input, output and every attempt with prompts and answers (kept for good). **Open trace** there or in `/why` shows the run in Langfuse (cloud.langfuse.com → project `trading-agent` → Tracing, last 30 days; [OBSERVABILITY.md](docs/design/OBSERVABILITY.md)). |
| What was ordered, and what was rejected? | Telegram 📤; dashboard → **Day** (risk, order and fill columns, with IBKR's answer) and **Risk** → Rejections |
| What does it hold, how is it doing? | Telegram `/positions`, `/pnl`; dashboard → **Portfolio** |
| What was sold, when and why | Dashboard → **Portfolio** → Sold |
| AI and broker costs | Telegram `/budget`; dashboard → **Costs** |
| Limits used, kill switch | Telegram `/status`; dashboard → **Risk** |
| Are the prices up to date? | Telegram `/status` → Data; dashboard → **Day** → Data freshness |
| What happens next, and when? | Telegram `/status` → Next jobs; the whole day in [HOW-IT-WORKS.md](HOW-IT-WORKS.md) 4 |
| Something went wrong | Telegram ⚠️ and 🔌; the log (11.1) |
| The machine itself is down | E-mail from healthchecks.io (7.4) |
| The IB Gateway's login screen | Remmina on the Zenbook desktop (11.3) |

---

## 7. Telegram and the heartbeat

The bot answers only your chat (`TELEGRAM_OWNER_CHAT_ID`); messages from anyone else are ignored and logged.

### 7.1 Messages it sends by itself

| Icon | When (Berlin time) | What |
|---|---|---|
| ▶️ ⏹ | Start, stop | The agent started or is stopping |
| 📰 | 08:30; 07:45 EU, 14:15 US; every 30 min | Morning briefing; a buy signal from the news (the stock joins the next scan); important news on a held stock |
| 🧠 | 08:15 EU, 14:45 US | Scan result: ranked proposals, blocked ones, passes, AI cost |
| 📤 | 09:15 EU, 15:45 US | Placement: orders sent to IBKR and rejections with the reason |
| 🟢 🔴 ⌛ | When IBKR reports it (checked every 10 min in sessions) | Bought; sold (target, stop, time, news, `/exit`); entry expired or rejected |
| 🚪 | Every 30 min, if needed | News exit: the News agent is at least 0.7 sure the reason for a trade is gone; a market sell went to IBKR |
| 🧐 | Every 30 min, if needed | The same, but less sure: no sale, `/exit SYMBOL` is your call |
| 🌙 | 23:05 | Evening digest: proposals, orders, fills, labels to do, AI spend |
| ⏸ ⛔ ▶️ | Any time | Kill switch paused, halted, active again (also after a loss limit) |
| ⚠️ | Any time | A job failed or was missed, data problem, reconciliation mismatch |
| 🔌 | Any time | Gateway down for 10 minutes and back again; orders, stop moves or exits skipped because IBKR wasn't connected |
| 🔐 | Live mode only | Confirm with `/confirm_live CODE` |

### 7.2 Commands

| Command | What it does |
|---|---|
| `/status` | Mode, kill switch, uptime, heartbeat, data dates, blocked stocks, next jobs, IBKR connection, AI tracing |
| `/briefing` | The morning briefing, now |
| `/budget` | AI spend this month and the budget mode |
| `/proposals` | The latest proposals and their status |
| `/why SYMBOL` | Thesis, critique and plan for one proposal |
| `/review` | Label the latest proposals Agree / Disagree / Skip, optionally with a reason. Only for evaluation; never changes a trade. |
| `/positions` | Open positions (entry, stop, target, last close, R, P&L) and pending entries |
| `/pnl` | Results for the day, week, month and since the start |
| `/exit SYMBOL` | Sell that position with a market order (at the next open if the market is closed) |
| `/pause [reason]`, `/resume` | No new entries until `/resume` |
| `/stop` | Kill switch: halt (asks for confirmation). No new entries, unfilled entries cancelled; open positions keep their stops and targets. |
| `/reset CODE` | End a halt, with the code from `docker compose run --rm agent trading-agent reset` on the Zenbook |
| `/confirm_live CODE` | Live mode only: allow orders after a start |
| `/help` | The command list |

### 7.3 Kill switch in short

Three states: **active** (normal), **paused** (no new entries: `/pause`, or a day's or week's loss limit), **halted** (no new entries, unfilled entries cancelled: `/stop`, the drawdown limit or a reconciliation mismatch). A halt ends only with a code printed on the Zenbook and sent from your phone with `/reset CODE` within 15 minutes.

### 7.4 The heartbeat (healthchecks.io)

Telegram can't tell you that the Zenbook is down, because the agent sends the messages. The heartbeat can: an outside service that expects a sign of life every 5 minutes.

- The agent calls `HEARTBEAT_URL` (`https://hc-ping.com/<uuid>`) every 5 minutes. Opening it in a browser just prints `OK`.
- **Where you look:** healthchecks.io → log in → **Checks**. The `trading-agent` check is green (up) or red (down), with the time of the last ping.
- **When it alerts:** no ping for 20 minutes (5 min period + 15 grace) → an e-mail, and another when pings resume. Causes: the Zenbook is off, asleep or offline, Docker or the agent stopped, or the internet is down.
- In Telegram, `/status` → `Heartbeat: ok, 2 min ago` means the last ping got through.

---

## 8. Your daily routine

| When | Do |
|---|---|
| After every reboot (including the automatic one at 04:00) | `make up`, then `/status` |
| Before 07:45 on trading days | Make sure the agent runs |
| Morning, 10 minutes | Read 📰 and the 🧠 EU proposals. Strongly disagree with something? `/pause` stops the placement at 09:15. |
| Evening, 30–60 minutes | Read 🌙. Decide on 🧐 advice (`/exit`). `/review` the day's proposals (agree or disagree, with a short reason). |
| Saturday, 1 hour | The week's results on the dashboard. Which lessons should change a prompt or a rule? Changes are versioned and need a reason. |
| About once a week | Confirm the IBKR 2FA prompt on the phone when the gateway asks |

---

## 9. Settings you can change

### 9.1 How to change a setting

- **`config/*.yaml` and `prompts/`** are part of the code: change them in the repository, commit, push, and on the Zenbook `git pull && make up`.
- **`.env`** lives only on the Zenbook: edit it there, then `make up` (a plain `docker compose restart` doesn't re-read `.env`).
- Change things outside trading sessions, or `/pause` first.
- **`config/risk.yaml` needs extra care**: every change gets its own commit with a reason. Unknown or invalid keys stop the agent from starting, so a typo can't silently turn off a limit.

### 9.2 `.env`: switches and keys

| Setting | Current | What it does |
|---|---|---|
| `APP_MODE` | `paper` | `paper` or `live`. Live also needs a live IBKR login and `/confirm_live`. In live mode only `markets.live` is traded, with the real `agent_budget_eur`. |
| `TWS_USERID`, `IB_HOST`, `IB_PORT`, `IB_CLIENT_ID` | | IBKR login name and connection. Port 4004 is paper, 4003 live. Every order goes to IBKR; nothing is placed while the gateway is disconnected. |
| `LLM_DEV_OVERRIDES` | `true` | `true`: the critic runs on Gemini (`dev_overrides` in `models.yaml`). `false` once an Anthropic key exists. |
| `LLM_FAKE` | `false` | `true` only in the local test stack (`make dev-up`): canned answers, no API calls. Never on the Zenbook. |
| `GEMINI_API_KEY`, `ANTHROPIC_API_KEY` | | AI provider keys. Without a key, that provider's roles can't run. |
| `TRACE_ENDPOINT` | Langfuse EU | Where each run's trace goes ([OBSERVABILITY.md](docs/design/OBSERVABILITY.md)). Tracing starts once the Langfuse keys are in `secrets/trace_headers` (`make trace-headers`); `TRACE_ENDPOINT=` (empty) switches it off. Every AI call is stored in the database either way. |
| `TRACE_CONTENT` | `true` | `false`: traces keep timings and tokens but no prompts or answers |
| `TRACE_ENVIRONMENT` | empty | Label in Langfuse; empty means `APP_MODE` |
| `TRACE_UI_URL` | your project | Langfuse project address for the "Open trace" links (dashboard, `/why`) |
| `DASHBOARD_UI_URL` | | The dashboard's address, linked in `/status` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_CHAT_ID` | | Your bot and your chat. Without the chat id, every message is ignored. |
| `FRED_API_KEY` | | US macro data; skipped without it |
| `FINNHUB_API_KEY` | | News for US stocks; without it no news buys or news exits for US stocks |
| `SEC_EDGAR_USER_AGENT` | | Required by the SEC for filings data (not used yet) |
| `HEARTBEAT_URL`, `BACKUP_HEARTBEAT_URL` | | Outside health checks for the agent and the backup |
| `BACKUP_AGE_RECIPIENT` | | Public key for the encrypted weekly backup copy |
| `LOG_LEVEL`, `TZ`, `DB_*` | | Logging detail, time zone, database connection; the defaults work |

### 9.3 `config/risk.yaml`: the hard limits

The risk engine reads this file. "Budget" is one budget for the US and Germany together: in paper mode `paper_budget_eur` (€15,000), in live mode `agent_budget_eur`.

| Setting | Current | What it does |
|---|---|---|
| `capital.agent_budget_eur` | 1000 | The money the agent may use in live mode, whatever the account really holds. The percentages below are of the budget. |
| `capital.paper_budget_eur` | 15000 | Paper mode: one notional budget for both markets |
| `capital.min_cash_reserve_pct` | 10 | Cash that is never spent |
| `per_trade.min_risk_pct` / `max_risk_pct` | 0.75 / 1.5 | Most you can lose on one trade if the stop holds, at the lowest and the highest confidence |
| `per_trade.min_position_pct` / `max_position_pct` | 10 / 25 | Most money in one stock, at the lowest and the highest confidence |
| `per_trade.min_confidence` / `max_confidence` | 0.30 / 0.60 | Confidence at or below which positions are smallest, and at or above which they are largest |
| `per_trade.min_position_eur` | 100 | Smallest position worth the fees |
| `per_trade.max_fee_to_risk_pct` | 10 | Reject if buy and sell fees exceed this share of the risk |
| `per_trade.min_risk_reward` | 2.0 | Minimum (target − entry) ÷ (entry − stop) |
| `per_trade.stop_atr_min` / `stop_atr_max` | 1.0 / 4.0 | Allowed stop distance in ATRs |
| `per_trade.max_limit_deviation_pct` | 1.0 | The entry may be at most this much above the current price |
| `portfolio.max_open_positions` | 8 | Positions plus pending entries at the same time |
| `portfolio.max_sector_pct` | 60 | Most of the budget in one sector |
| `portfolio.max_correlated_cluster_pct` | 60 | Most of the budget in stocks that move together (correlation above 0.7 over 60 sessions) |
| `loss_limits.daily_loss_pct` | 3 | A day's loss this large pauses new entries through the next trading day |
| `loss_limits.weekly_loss_pct` | 6 | A week's loss this large pauses until Monday |
| `loss_limits.max_drawdown_pct` | 15 | A drawdown this large halts until you reset |
| `markets.paper` / `markets.live` | [US, EU] / [US] | Markets that may be traded in each mode |
| `instruments.min_price`, `min_avg_daily_dollar_volume` | 5, 20,000,000 | Lowest share price and 20-day average traded value |
| `instruments.blacklist` | [] | Yahoo symbols that are never scanned or traded, e.g. `[TSLA, SAP.DE]` |
| `execution.max_orders_per_day` | 8 | New brackets per day |
| `execution.no_trading_first_minutes` / `last_minutes` | 15 / 10 | No new entries this close to the open or close |
| `execution.no_new_entries_before_earnings_days` | 3 | No new entry if a report is this many sessions away or fewer |

How the limits are used (the 9 checks and the position-size formula): [HOW-IT-WORKS.md](HOW-IT-WORKS.md) 5, step 8.

### 9.4 `config/models.yaml`: AI models and budget

| Setting | Current | What it does |
|---|---|---|
| `roles.triage` | Gemini 3.1 Flash-Lite | News agent (cheap and fast; it reads many headlines) |
| `roles.analysis` | Gemini 3.8 Flash | Technical and earnings analysts |
| `roles.proposer` | Gemini 3.8 Flash | Proposer |
| `roles.critic` | Claude Sonnet 5.5 | Critic; needs `ANTHROPIC_API_KEY` and a price below |
| `dev_overrides.critic` | Gemini 3.8 Flash | Used instead while `LLM_DEV_OVERRIDES=true` |
| `prices` | per model | Price per million tokens. **A model without a price is never called.** |
| `budget.monthly_usd` | 16 | The monthly AI budget (about €15) |
| `budget.lean_mode_at` / `hard_stop_at` | 0.8 / 1.0 | From 80 % spent fewer candidates per scan; from 100 % no more AI calls this month |
| `rate_limits_rpm` | 10 / 15 | Calls per minute per model, to stay inside the free tier |
| `scan.candidates` / `candidates_lean` | 10 / 5 | Candidates analysed per market and scan. The biggest lever on AI cost. |

### 9.5 `config/strategies.yaml`: the screen, trade management, news

| Setting | Current | What it does |
|---|---|---|
| `min_price`, `min_avg_dollar_volume`, `avg_volume_sessions` | 5, 20 M, 20 | Price and liquidity floor of the screen |
| `high_lookback`, `pullback_sessions` | 20, [3, 10] | The pullback starts from the highest close of the last 20 days, 3–10 days ago |
| `ma_band_pct` | [0, 3] | Close 0–3 % above the EMA20 or SMA50 |
| `rsi_range` | [40, 55] | RSI window |
| `earnings_buffer_sessions` | 3 | No report this close |
| `rs_window` | 63 | Ranking: 3-month return against the market |
| `entry_valid_sessions` | 2 | How long a buy limit stays open |
| `breakeven_r` | 1.0 | Move the stop to the entry price at +1R |
| `time_stop_sessions` | 15 | Sell after this many sessions |
| `trail_atr` | off (`null`) | E.g. 2.0: after breakeven, trail the stop 2 ATR under the highest close |
| `regime_sma` | off (`null`) | E.g. 200: no new entries while the market's index ETF closes below this average |
| `regime_max_vix` | off (`null`) | E.g. 30: no new entries while the VIX closes above this (needs `FRED_API_KEY`) |
| `news.buy_confidence` | 0.6 | A buy signal this sure puts the stock into the market's next scan |
| `news.exit_confidence` | 0.7 | An exit this sure sells at the market; below it you get advice (🧐) |
| `news.max_candidates` | 3 | News candidates per scan and market |
| `news.max_trades_per_day` | 1 | News-triggered entries placed per day, both markets together |
| `news.signal_hours` | 24 | Older buy signals are ignored |
| `news.lookback_days`, `max_headlines`, `batch` | 2, 5, 8 | Days of news per request, new headlines read per stock, stocks per News agent call |

### 9.6 Other files

| File | What it controls |
|---|---|
| `config/schedule.yaml` | When each job runs: a clock time (`cron`, Berlin time) or relative to an exchange's open or close. `misfire_grace_minutes` is how late a job may still run. Keep the order: prices before end of day, scan before placement, placement inside the trading window. |
| `config/fees.yaml` | IBKR commissions per market, for the fee check and the book. Compare with the first real statements. |
| `config/data.yaml` | Price history length (6 years), price source per market, macro series, quality thresholds |
| `config/universe.yaml` | The stocks that are watched, with sector and currency, plus the two benchmark ETFs. Generated by `scripts/build_universe.py`; don't edit by hand. |
| `prompts/<agent>/v<N>.md` | The instructions for each AI agent. A change becomes a new version, so results before and after can be compared. |

---

## 10. Testing that everything works

Run these once, in this order, and again when you suspect a problem in that area.

| # | Test | Do | Expect | Done |
|---|---|---|---|---|
| 1 | Containers | `docker compose ps` | All four `Up` | ☐ |
| 2 | Telegram | `/status`, `/help` | Status and command list | ☐ |
| 3 | Heartbeat | `/status` | `Heartbeat: ok, … ago`; the check on healthchecks.io is green | ☑ |
| 4 | Market data | `docker compose run --rm agent trading-agent quality` | A short report, ideally `0 blocked symbols` | ☑ |
| 5 | Dashboard | http://localhost:8501 on the Zenbook; the `.ts.net` address on the phone with Tailscale on, then off | Day, Portfolio, Costs, Risk load; nothing without Tailscale | ☐ |
| 6 | IBKR | `docker compose run --rm -e IB_CLIENT_ID=12 agent trading-agent ibkr-check` | `Contracts without conid: none`; in market hours `AAPL: delayed, last …` | ☑ |
| 7 | AI analysts | `docker compose run --rm agent trading-agent analyse --top 3` | Rating, plan and earnings stance per stock; a few cents | ☐ |
| 8 | Agent pipeline | `docker compose run --rm agent trading-agent propose --market US`, then `/proposals`, `/why SYMBOL`, `/review`, dashboard → Day | Ranked proposals; every step on the Day page; nothing ordered (placement uses only the scheduled scan) | ☐ |
| 9 | Kill switch | See below | `Kill switch: active` at the end | ☐ |
| 10 | Backups | See below | `backup ok`; row counts twice | ☐ |
| 11 | Gateway overnight | Next morning: `docker compose logs --since 12h ib-gateway \| grep -v "Connection refused" \| tail -50` | The 23:45 restart and a new login without you | ☐ |
| 12 | A trading day | Watch Telegram for two trading days | 🧠 📰 📤 🌙 at their times; 🟢 🔴 ⌛ when something fills; every order with its `TA-SYMBOL-kind-…` reference in TWS or Client Portal | ☐ |
| 13 | Chaos tests | With `/positions` empty: [IMPLEMENTATION.md](docs/design/IMPLEMENTATION.md) 15.5 step 17 | No order is lost, duplicated or left without a stop | ☐ |
| 14 | AI tracing | After `make trace-headers`: `/status`; `docker compose run --rm agent trading-agent propose AAPL`, then Langfuse → Tracing; dashboard → Day → AAPL → technical step → Open trace | `LLM tracing: on`; one trace per run with one step per AI answer | ☐ |
| 15 | AI test cases (evals) | Once: `curl -LsSf https://astral.sh/uv/install.sh \| sh`, new terminal, `uv sync && uv run pytest -m llm tests/evals` | All cases pass; a few cents | ☐ |

**Test 9, kill switch** (not at 09:15 or 15:45):

1. `/pause test`, then `/status` → `paused (test)`. `/resume`.
2. `/stop` and confirm → `/status` shows `halted`.
3. On the Zenbook: `docker compose run --rm agent trading-agent reset` prints a code.
4. Within 15 minutes: `/reset CODE` → `Kill switch: active`. Don't leave it halted, or nothing trades.

**Test 10, backups** (needs 12.1):

```sh
OFFSITE=1 make backup                                                  # backup now, plus the encrypted copy
make restore-check FILE=$(ls -1t backups/*.dump | head -1)             # restores into a scratch database, then drops it
# paste the private key from your password manager into /tmp/age-key.txt, then:
AGE_KEY=/tmp/age-key.txt make restore-check FILE=$(ls -1t backups/offsite/*.age | head -1)
shred -u /tmp/age-key.txt
```

---

## 11. Logs, audit trail, troubleshooting

### 11.1 Logs

Watch the agent live (Ctrl-C ends only the view): `make logs`.

One line per step of the last 24 hours:

```sh
docker compose logs agent --since 24h | grep -E '"event": "(agent.started|scheduler.planned|ingest.done|quality.checked|scan.done|scan.excluded|proposals.done|llm.analysis|news.done|place.done|place.nothing|place.not_connected|executor.submitted|stop.moved|reconcile.done)"'
```

| Event | Means |
|---|---|
| `scheduler.planned` | Today's jobs were planned |
| `ingest.done`, `quality.checked` | Prices loaded, data checked |
| `scan.done`, `scan.excluded` | Scan finished (analyses, cost); stocks left out (data problem, blacklist) |
| `llm.analysis` | One AI answer: role, model, status, tokens, cost |
| `proposals.done` | Proposals stored, with the reasons for skipped stocks |
| `news.done` | A News agent run: how many stocks, which got a buy signal or an exit |
| `place.done`, `place.nothing`, `place.not_connected` | Orders placed and rejected; nothing to place; IBKR wasn't connected |
| `executor.submitted`, `stop.moved` | Order sent; stop raised |
| `reconcile.done` | IBKR and the book agree (`clean: true`) |

Only problems of the last 24 hours:

```sh
docker compose logs agent --since 24h | grep -E '"level": "(warning|error)"|Traceback|raised an exception'
```

Everything one AI run logged (every line written during a run carries its `trace_id`, the ID at the end of the Langfuse trace address):

```sh
docker compose logs agent --since 24h | grep 670185b724af04833af8100411b608eb
```

The gateway's log without the noise: `docker compose logs --since 2h ib-gateway | grep -v "Connection refused"`.

### 11.2 Audit trail

Every order, kill-switch change and start is stored for good. The `dashboard` login can only read:

```sh
docker compose exec db psql -U dashboard -d trading -c \
  "select ts, actor, event, payload from audit_log order by ts desc limit 20;"
```

### 11.3 Troubleshooting

| Problem | Fix |
|---|---|
| The dashboard shows old pages or positions that IBKR doesn't hold | The running image is older than the code: `make up` (5.1). Since November 2026 it builds and updates the database every time; the old simulator's positions move to the archive (5.4). |
| `/status` or the log reports a reconciliation mismatch (⚠️, ⛔) | IBKR holds something the book doesn't, or the other way round. Compare `/positions` with the IBKR app. Fix the cause (e.g. a manual trade in the paper account), then `/reset CODE`. |
| A container shows `Restarting` | `docker compose logs <name> \| tail -50` shows why; usually a wrong value in `.env` (e.g. `TELEGRAM_OWNER_CHAT_ID` must be a number or the line removed) |
| No Telegram messages | `docker compose ps` (agent running?), `make logs` (errors?), `TELEGRAM_OWNER_CHAT_ID` set? After changes: `make up` |
| Heartbeat `FAILING` | `docker compose logs agent \| grep heartbeat.failed \| tail -3`. `UnsupportedProtocol`: the value isn't a clean URL (quotes, a comment after it, missing `https://`). `404`: wrong check id. Then `make up`. |
| The gateway: only "Connection refused" for more than 5 minutes | The login didn't finish; look at its screen (below) |
| The gateway: "Invalid username or password" | Check `TWS_USERID`, run `make tws-password` again, then `docker compose up -d --force-recreate ib-gateway`. Stop the gateway meanwhile (`docker compose stop ib-gateway`); repeated failures can lock the login. |
| The gateway waits for "second factor" | Confirm the 2FA prompt in the IBKR app |
| The gateway: "Existing session" | The same username is logged in elsewhere; log out there |
| `ibkr-check` times out | The running agent uses client id 11; the check needs its own: `-e IB_CLIENT_ID=12` |
| Market data warnings (10089, 354, 10167, `tickType 88`) | No paid real-time data; the agent uses free delayed quotes and the last close. Nothing to fix. |
| `git pull` says "divergent branches" or refuses | Something was changed on the Zenbook, which should only receive code: `git fetch origin && git reset --hard origin/main` (`.env`, `secrets/`, `backups/` and the database aren't touched) |
| CI failure e-mail | The latest code on GitHub failed a check. Don't update the Zenbook to it; wait for a green run. |

**The gateway's screen:** on the Zenbook desktop, open **Remmina**, protocol VNC, server `localhost:5900`. The password is the first 8 characters of `secrets/vnc_password`: `head -c 8 secrets/vnc_password; echo`.

---

## 12. Backups and moving to another machine

### 12.1 Backups

```sh
age-keygen -o /tmp/age-key.txt               # prints "Public key: age1…"
cat /tmp/age-key.txt                         # copy the whole content into your password manager
shred -u /tmp/age-key.txt                    # the private key must not stay on the Zenbook
nano .env                                    # BACKUP_AGE_RECIPIENT=age1…  (optional: BACKUP_HEARTBEAT_URL)
crontab -e                                   # add the line below
```

```
15 3 * * * cd ~/projects/trading-agent && scripts/backup.sh >> backups/backup.log 2>&1
```

Every night at 03:15 the database is dumped into `backups/` (14 days kept); on Sundays an encrypted copy goes to `backups/offsite/`. Copy those files to a cloud drive yourself; only the key in your password manager can decrypt them. Optional: a second healthchecks.io check (period 1 day, grace 2 hours) as `BACKUP_HEARTBEAT_URL`. Then run test 10.

### 12.2 Moving to another machine

For the mini PC before going live: prepare it as in 3.1 (with full-disk encryption), clone the code as in 3.2, copy `.env` and `secrets/` over, `make up`, restore the latest backup (`scripts/restore.sh into FILE`, see [IMPLEMENTATION.md](docs/design/IMPLEMENTATION.md) 15.4), `make up` again, and check that the reconciliation is clean (`/status`, no ⚠️) before stopping the Zenbook's stack.

---

## 13. Reference: the parts and the logins

| Part | Where | Reached from |
|---|---|---|
| `agent` container | Zenbook | Telegram; `make logs` |
| `dashboard` container | Zenbook, `127.0.0.1:8501` | http://localhost:8501 on the Zenbook; your tailnet via `tailscale serve` (https) |
| `db` container (PostgreSQL) | Zenbook, only inside Docker | `docker compose exec db psql -U dashboard -d trading` (read-only) |
| `ib-gateway` container | Zenbook; API port 4004 only inside Docker, VNC `127.0.0.1:5900` | The agent; Remmina on the Zenbook |
| Zenbook shell | Zenbook | Its desktop; SSH only from the tailnet |
| IBKR paper account | IBKR's servers | Client Portal, IBKR app |
| Code | GitHub (`klausrossmann/trading-agent`), copy on the Zenbook | `git pull` with a read-only deploy key |
| Heartbeat | healthchecks.io | Its website and alert e-mails |
| AI traces | Langfuse Cloud, EU region (free plan, 30 days) | cloud.langfuse.com, project `trading-agent`; "Open trace" links |

Nothing on the Zenbook accepts connections from the internet or the home network (`ufw`); other devices come in only through Tailscale.

| Login | Kept in | Used for |
|---|---|---|
| IBKR username and password | `.env` (`TWS_USERID`), `secrets/tws_password` | The gateway's automatic login in paper mode |
| Database and VNC passwords | `secrets/` | Each container reads only its own |
| API keys (Gemini, FRED, Finnhub, Telegram, heartbeat) | `.env` | The agent's outgoing calls |
| Langfuse login | Your password manager | Looking at traces in the browser |
| Langfuse API keys | `secrets/trace_headers` (`make trace-headers`) | The agent sending traces |
| Backup decryption key | Only your password manager | Restoring an encrypted backup |

`.env` and `secrets/` never go to GitHub; `.env` is `chmod 600` and `secrets/` `chmod 700`.
