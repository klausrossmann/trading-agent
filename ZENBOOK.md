# Running the agent on the Zenbook

Everything you need to run the trading agent on the Zenbook: what to have ready, the one-time setup, the first start, daily use and updates. Day-to-day watching (dashboard, Telegram, logs, troubleshooting) is in [RUNNING.md](RUNNING.md); what the agent does once it runs, and when, is in [RUNTIME.md](RUNTIME.md).

> **Where you are (2026-11-05):** the Zenbook is prepared, the app is installed and the IBKR paper gateway logged in (October 2026). Still to do: the update to the simplified version (section 6.1), the `FINNHUB_API_KEY` (section 3.3), backups (section 7) and the tests in [RUNNING.md](RUNNING.md) section 6.

**Contents**

1. [What you need](#1-what-you-need)
2. [Prepare the Zenbook (once)](#2-prepare-the-zenbook-once)
3. [Install the app (once)](#3-install-the-app-once)
4. [First start and checks](#4-first-start-and-checks)
5. [Every day](#5-every-day)
6. [Updating](#6-updating)
7. [Backups](#7-backups)
8. [Moving to another machine](#8-moving-to-another-machine)

---

## 1. What you need

### 1.1 The machine

| | |
|---|---|
| Computer | Asus Zenbook UX301LA (x86_64, 8 GB RAM), always plugged in, never sleeping, online |
| OS | Ubuntu 24.04 LTS or newer (desktop install is fine) |
| Disk | At least 15 GB free |
| Memory use | About 2 GB for the app, 3.5 GB with the IB Gateway; a desktop and a browser fit next to it |

### 1.2 Accounts and keys

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
| age key pair | Encrypted offsite backups | Recommended | `age-keygen` (section 7) | `.env` `BACKUP_AGE_RECIPIENT`; private key only in your password manager |
| GitHub deploy key | Downloading the code | Yes | `ssh-keygen` on the Zenbook, public key under GitHub → repo → Settings → Deploy keys (read-only) | |

EU news comes from Yahoo and needs no key. Market prices come from Yahoo (no key) and ECB (no key).

---

## 2. Prepare the Zenbook (once)

Done on your Zenbook in October 2026. For reference, or for a fresh install, in a terminal as your normal user:

```sh
# 2.1 Basics and the time zone
sudo apt update && sudo apt install -y git make openssl curl age unattended-upgrades
sudo timedatectl set-timezone Europe/Berlin            # check: timedatectl shows NTP active

# 2.2 Never sleep (also with the lid closed)
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
sudo nano /etc/systemd/logind.conf
#   HandleLidSwitch=ignore
#   HandleLidSwitchExternalPower=ignore
#   HandleLidSwitchDocked=ignore
# then reboot

# 2.3 Docker Engine and Compose from Docker's apt repository (not Docker Desktop, not the snap)
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER                           # log out and in again
echo '{"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "5"}}' | sudo tee /etc/docker/daemon.json
sudo systemctl enable --now docker && sudo systemctl restart docker

# 2.4 A 4 GB swap file (if `swapon --show` shows less)
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 2.5 Tailscale and the firewall: nothing reachable from outside except through Tailscale
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
sudo apt install -y openssh-server
sudo ufw default deny incoming && sudo ufw allow in on tailscale0 && sudo ufw enable

# 2.6 Security updates with an automatic reboot at 04:00 when needed
sudo dpkg-reconfigure -plow unattended-upgrades
#   in /etc/apt/apt.conf.d/50unattended-upgrades:
#   Unattended-Upgrade::Automatic-Reboot "true";
#   Unattended-Upgrade::Automatic-Reboot-Time "04:00";
```

After the automatic 04:00 reboot, only `db` and `dashboard` come back by themselves. **Start the agent again with `make up`** (section 5).

---

## 3. Install the app (once)

All commands from here on run in the app's folder:

```sh
cd ~/projects/trading-agent
```

### 3.1 Code

```sh
ssh-keygen -t ed25519                                   # add ~/.ssh/id_ed25519.pub as a read-only deploy key on GitHub
git clone git@github.com:klausrossmann/trading-agent.git ~/projects/trading-agent
cd ~/projects/trading-agent && git config pull.ff only
```

### 3.2 Passwords

```sh
make secrets          # random database and VNC passwords in secrets/
make tws-password     # type your IBKR paper password (not shown)
```

### 3.3 Settings: `.env`

```sh
cp .env.example .env && chmod 600 .env && nano .env
```

Write each value directly after `=`, without quotes, and never put a comment on the same line (Docker Compose would read it as the value).

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
| `BACKUP_AGE_RECIPIENT`, `BACKUP_HEARTBEAT_URL` | Section 7 |

Leave the rest as in `.env.example` (`LLM_FAKE=false`, `IB_HOST`, `IB_PORT=4004`, `IB_CLIENT_ID=11`, the `DB_*` values). There is no switch for the broker: every order goes to the IBKR account the gateway is logged in to.

The settings that decide *how* the agent trades are in `config/*.yaml` and part of the code; each is explained in [HOW-IT-WORKS.md](HOW-IT-WORKS.md) section 12.

### 3.4 Build, database, history

```sh
make build                                              # builds the image (a few minutes the first time)
docker compose up -d db && make migrate                 # starts the database, creates the tables
docker compose run --rm agent trading-agent backfill    # 6 years of prices, FX, macro, earnings (several minutes)
docker compose run --rm agent trading-agent backfill    # again: must report no changes
docker compose run --rm agent trading-agent quality     # data check, ideally "0 blocked symbols"
```

---

## 4. First start and checks

| # | Do | Expect |
|---|---|---|
| 1 | `make up` | Four containers start |
| 2 | `docker compose logs -f ib-gateway \| grep -v "Connection refused"` | `Login has completed` within 30–90 s; confirm the 2FA prompt in the IBKR app if one comes |
| 3 | Send your bot any message, then `docker compose logs agent \| grep telegram.ignored` | Your `chat_id`; put it into `.env` as `TELEGRAM_OWNER_CHAT_ID`, then `make up` |
| 4 | Telegram `/status` | Mode `paper`, kill switch `active`, data dates, next jobs, `IB Gateway: connected`, heartbeat ok |
| 5 | `sudo tailscale serve --bg 8501`, then `tailscale serve status` | The dashboard's `https://...ts.net` address; open it on the phone with Tailscale on |
| 6 | `docker compose run --rm -e IB_CLIENT_ID=12 agent trading-agent ibkr-check` | Account values, `Positions: 0`, no contracts without an id |
| 7 | Optional, Langfuse: `make trace-headers` (paste the public and secret key), `TRACE_UI_URL` in `.env`, `make up` | `/status`: `LLM tracing: on` |

Then work through the tests in [RUNNING.md](RUNNING.md) section 6 (AI analysts, kill switch, backups, a trading day, chaos tests).

For the AI test cases (evals) on the Zenbook, once: `curl -LsSf https://astral.sh/uv/install.sh | sh`, open a new terminal, then `uv sync && uv run pytest -m llm tests/evals` (uses `GEMINI_API_KEY` from `.env`; a few cents).

---

## 5. Every day

| When | Do |
|---|---|
| After every reboot (including the automatic one at 04:00) | `make up`, then `/status` in Telegram. The agent and the gateway never start by themselves. |
| Before 07:45 on trading days | Make sure the agent runs. Started later, the morning's EU news and scan are skipped (details: [RUNTIME.md](RUNTIME.md) section 6). |
| Morning | Read 📰 and 🧠; `/pause` if something looks wrong |
| Evening | Read 🌙; decide on 🧐 advice; optionally `/review` |
| About once a week | Confirm the IBKR 2FA prompt on the phone when the gateway asks |
| To stop | `make down`, outside trading sessions if possible. Orders at IBKR stay active, but nothing watches them. |

`make logs` shows the agent's log live; [RUNNING.md](RUNNING.md) section 4 lists the useful filters.

---

## 6. Updating

When new code is on GitHub and its CI run is green, outside trading sessions (or after `/pause`):

```sh
git pull
make build
make migrate
make up
```

Then `/status`, and `/resume` if you paused. Only `.env` changed: `make up`. Only documentation: `git pull`. `config/` and `prompts/` are part of the image: they need `make build`.

### 6.1 The first update to the simplified version (November 2026)

1. Wait until no order is pending and ideally nothing is held (`/positions`), or do it in the evening after 23:05.
2. In `.env`, delete the lines `IB_ENABLED`, `IB_ORDERS_ENABLED`, `IB_READ_ONLY_API` and `COMPOSE_PROFILES` (they no longer exist), and add `FINNHUB_API_KEY` if missing.
3. `git pull && make build && make migrate && make up`. The migrations add the step log (`0013`) and move rows from the old simulator to the archive book `sim_archive` (`0014`), so they never count as real positions.
4. The gateway now always starts with `make up` and has `READ_ONLY_API: "no"`: the first placement after the next scan goes to the IBKR paper account.
5. Check: `/status` (gateway connected, kill switch active); the next day the dashboard's **Day** page fills from 07:45 on.

---

## 7. Backups

```sh
age-keygen -o /tmp/age-key.txt               # prints "Public key: age1…"
cat /tmp/age-key.txt                         # copy the whole content into your password manager
shred -u /tmp/age-key.txt                    # the private key must not stay on the Zenbook
nano .env                                    # BACKUP_AGE_RECIPIENT=age1…  (optional: BACKUP_HEARTBEAT_URL)
crontab -e                                   # add:
# 15 3 * * * cd ~/projects/trading-agent && scripts/backup.sh >> backups/backup.log 2>&1
```

Every night at 03:15 the database is dumped into `backups/` (14 days kept); on Sundays an encrypted copy goes to `backups/offsite/`. Copy those files to a cloud drive yourself; only the key in your password manager can decrypt them. Test with [RUNNING.md](RUNNING.md) section 6, test 10.

---

## 8. Moving to another machine

For the mini PC before going live: prepare it as in section 2 (with full-disk encryption), install as in section 3 but instead of the backfill copy `.env` and `secrets/` over and restore the latest backup (`scripts/restore.sh into FILE`, see [IMPLEMENTATION.md](IMPLEMENTATION.md) 15.4), `make up`, and check that the reconciliation is clean (`/status`, no ⚠️) before shutting down the Zenbook's stack.
