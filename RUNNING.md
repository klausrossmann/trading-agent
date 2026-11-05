# Running the Trading Agent

How to watch, start, test and fix the app on the Zenbook. Day to day you need sections 1–3; the rest is for when you start, update or troubleshoot. Installing and setting up the Zenbook is in [ZENBOOK.md](ZENBOOK.md); what the agent does and when is in [RUNTIME.md](RUNTIME.md); how it decides is in [HOW-IT-WORKS.md](HOW-IT-WORKS.md).

Every command runs on the Zenbook as your normal user (a member of the `docker` group) in the app's folder:

```sh
cd ~/projects/trading-agent     # code, .env, secrets/, backups/
```

**Contents**

1. [Where to find what](#1-where-to-find-what)
2. [The dashboard](#2-the-dashboard)
3. [Telegram and the heartbeat](#3-telegram-and-the-heartbeat)
4. [Logs and audit trail](#4-logs-and-audit-trail)
5. [Starting and stopping](#5-starting-and-stopping)
6. [Testing that everything works](#6-testing-that-everything-works)
7. [Updating](#7-updating)
8. [Troubleshooting](#8-troubleshooting)
9. [Reference: the parts, logins and setup](#9-reference-the-parts-logins-and-setup)

---

## 1. Where to find what

| You want to know | Look here |
|---|---|
| The dashboard | `https://klaus-ux301laa.<your-tailnet>.ts.net`, with Tailscale on (finding the exact address: section 2) |
| Is everything running? | Telegram `/status` |
| What happens next, and when? | Telegram `/status` → Next jobs; the whole day in [RUNTIME.md](RUNTIME.md) |
| What did the agent propose today? | Telegram 🧠 messages, `/proposals`; dashboard → **Day** |
| Why did it propose (or skip) a stock? | Telegram `/why SYMBOL`; dashboard → **Day** → pick the stock: every step with its reason |
| Exactly what the AI was asked and answered, step by step | Dashboard → **Day** → pick the stock → open an agent step: its input, output and every attempt with prompts and answers (kept for good); **Open trace** there or in `/why` shows the whole run in Langfuse (cloud.langfuse.com → project `trading-agent` → Tracing, last 30 days; [OBSERVABILITY.md](OBSERVABILITY.md)) |
| What was ordered, and what was rejected? | Telegram 📤 messages; dashboard → **Day** (risk, order and fill columns, with IBKR's answer and error) and **Risk** → Rejections |
| What does it hold, and how is it doing? | Telegram `/positions`, `/pnl`; dashboard → **Portfolio** |
| What was sold, when and why | Dashboard → **Portfolio** → Sold |
| AI and broker costs | Telegram `/budget`; dashboard → **Costs** |
| Limits used, kill switch | Telegram `/status`; dashboard → **Risk** |
| Are the prices up to date? | Telegram `/status` → Data; dashboard → **Day** → Data freshness |
| Something went wrong | Telegram ⚠️ and 🔌 messages; the log (section 4) |
| The machine itself is down | E-mail from healthchecks.io; its website shows when the last heartbeat arrived (section 3.1) |
| The IB Gateway's login screen | Remmina on the Zenbook desktop (8.3) |
| Settings | `.env` (switches, keys) and `config/*.yaml` (rules, limits); every setting is explained in [HOW-IT-WORKS.md](HOW-IT-WORKS.md) section 12 |
| Backups | `backups/` on the Zenbook (section 9.4) |

---

## 2. The dashboard

The dashboard is a website that runs on the Zenbook. It only shows data and can't change anything. There's no login: only devices in your Tailscale network (tailnet) can reach it.

| From | Address |
|---|---|
| The Zenbook itself | `http://localhost:8501` |
| Phone, tablet or another computer, with the Tailscale app on and logged in to the same account | `https://klaus-ux301laa.<your-tailnet>.ts.net` |

`<your-tailnet>` is a name Tailscale gave your account, like `tail1a2b3c`. To get the exact address, use any of these:

- On the Zenbook: `tailscale serve status`. The first line is the address.
- In the Tailscale app on the phone: the device list → `klaus-ux301laa` → its name ending in `.ts.net`.
- In the browser: login.tailscale.com → Machines → `klaus-ux301laa`.

Bookmark it on the phone. If `tailscale serve status` prints nothing, the dashboard isn't published yet: `sudo tailscale serve --bg 8501` as your desktop user (once; it survives reboots).

| Page | Shows | At the start (no trades yet) |
|---|---|---|
| **Day** | Pick a day and a market. **Stocks past each phase**: how many stocks remain after the screen, technical analyst, earnings analyst, proposer, critic, ranking, risk engine, IBKR order and fill. **Stocks**: one row per stock, one column per phase, each with ✓ passed, ✗ stopped (and why), ⚠ error; the first column, **news**, shows the News agent's buy signals and its verdicts on held stocks (hold or exit, with the confidence); filter e.g. "stopped at critic" or "bought". **What happened to**: pick a stock to see every step with its time, input and output: the screen's indicator values, each agent's input, answer and attempts (prompts and replies), the risk engine's 9 checks, the orders as sent to IBKR with IBKR's reply, order reference and any error, fills, stop moves and the sale. "Follow this trade over every day" adds the later days. **Data freshness**: the latest date of every dataset | "Nothing recorded" until the first scan (08:15) |
| **Portfolio** | **Held at IBKR**: quantity, entry, current stop (and whether it moved), target, last close, R now, P&L in EUR, day bought, sessions held, time-stop day. **Buy orders waiting**. **Sold**: when, why in words (target, stop, stop after it was raised, time stop, `/exit`), result in EUR and R, fees. **Results** and the realized P&L curve. **A trade, step by step**: the whole timeline of any trade | "No open positions.", "Nothing sold yet." |
| **Costs** | AI spend per month, role and model, and per day; broker fees | Empty until the first AI call |
| **Risk** | Kill switch; how much of each limit the account (US and EU, one budget) uses (drawdown, day and week loss, positions, sector); correlation of the holdings; the risk engine's rejections of the last 30 days by check | Kill switch `active`; limits after the first end of day; "None." under rejections |

**How it fills:** the scans (08:15, 14:45) fill Day and Costs; the placements (09:15, 15:45) add the risk and order steps; IBKR's fills, stop moves and sales follow during the day and in the evening, and appear in Portfolio.

**Daily one-minute check:** Day → Data freshness (are the dates current?), the order column (any ⚠?) and Risk (kill switch `active`). If these look right, the rest works too.

---

## 3. Telegram and the heartbeat

The bot answers only your chat. It sends these messages by itself:

| Icon | When (Berlin time) | What |
|---|---|---|
| 🧠 | 08:15 EU, 14:45 US | Scan result: ranked proposals, blocked ones, passes, AI cost |
| 📰 | 08:30; 07:45 EU, 14:15 US; every 30 min | Morning briefing; a buy signal from the news (the stock joins the next scan); important news on a held stock |
| 📤 | 09:15 EU, 15:45 US | Placement: orders sent to IBKR and rejections with the reason |
| 🟢 🔴 ⌛ | When IBKR reports it (checked every 10 min in sessions) | Bought, sold (target, stop, time, news, `/exit`), entry expired or rejected |
| 🚪 | Every 30 min, if needed | News exit: the News agent is at least 0.7 sure the reason for a trade is gone; a market sell went to IBKR |
| 🧐 | Every 30 min, if needed | The same, but less sure: no sale, `/exit SYMBOL` is your call |
| 🌙 | 23:05 | Evening digest: proposals, orders, fills, labels to do, AI spend |
| ⏸ ⛔ ▶️ | Any time | Kill switch paused, halted, active again |
| ⚠️ 🔌 | Any time | A job failed or was missed, data problem, reconciliation mismatch; gateway down for 10 minutes; orders or end-of-day work skipped because IBKR wasn't connected |

| Command | What it does |
|---|---|
| `/status` | Running? Heartbeat, data dates, blocked stocks, next jobs, IBKR, kill switch, AI tracing |
| `/proposals`, `/why SYMBOL` | Latest proposals; thesis, critique and plan for one |
| `/review` | Label the latest proposals Agree / Disagree, optionally with a reason. Only for evaluation. |
| `/positions`, `/pnl` | Open positions and pending entries; results for day, week, month, since start |
| `/briefing`, `/budget` | The morning briefing now; AI spend this month |
| `/pause [reason]`, `/resume` | Stop / allow new entries |
| `/stop` | Kill switch: halt (no new entries, unfilled entries cancelled). Ends only with `/reset CODE`. |
| `/reset CODE` | End a halt with the code from `trading-agent reset` on the Zenbook |
| `/exit SYMBOL` | Sell that position at the next open |
| `/help` | All commands |

**Your daily part** (details in [HOW-IT-WORKS.md](HOW-IT-WORKS.md) 11.4): morning, read 📰 and 🧠, `/pause` if something looks wrong; evening, read 🌙, decide on 🧐, optionally `/review`; about once a week, confirm the IBKR 2FA prompt on the phone.

### 3.1 The heartbeat (healthchecks.io)

Telegram can't tell you that the Zenbook is down, because the agent sends the messages. That's what the heartbeat is for: an outside service that expects a sign of life every 5 minutes.

- **The ping URL** (`HEARTBEAT_URL` in `.env`, `https://hc-ping.com/<uuid>`) is only a receiver. The agent calls it every 5 minutes; that call is the sign of life. There's nothing to see there: opening it in a browser just prints `OK` and counts as a ping itself.
- **Where you look:** healthchecks.io → log in → **Checks**. The `trading-agent` check shows green (up) or red (down), when the last ping arrived, and on its page a log of all pings.
- **When it alerts:** if no ping arrives for 20 minutes (5 minutes period + 15 grace), healthchecks.io e-mails you, and again when pings resume. Causes: the Zenbook is off, asleep or offline, Docker or the agent stopped, or the internet is down.
- **In Telegram:** `/status` → `Heartbeat: ok, 2 min ago` means the last ping got through.

---

## 4. Logs and audit trail

**Watch the agent live** (Ctrl-C ends only the view):

```sh
make logs
```

**One line per step of the last 24 hours:**

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
| `reconcile.done` | Broker and agent agree (`clean: true`) |

**Only problems of the last 24 hours:**

```sh
docker compose logs agent --since 24h | grep -E '"level": "(warning|error)"|Traceback|raised an exception'
```

**Everything one scan logged:** every line written during an AI run carries its `trace_id` (the ID at the end of the Langfuse trace address):

```sh
docker compose logs agent --since 24h | grep 670185b724af04833af8100411b608eb
```

**The gateway's log without the noise:**

```sh
docker compose logs --since 2h ib-gateway | grep -v "Connection refused"
```

**Audit trail:** every order, kill-switch change and start is stored for good. The `dashboard` login can only read:

```sh
docker compose exec db psql -U dashboard -d trading -c \
  "select ts, actor, event, payload from audit_log order by ts desc limit 20;"
```

---

## 5. Starting and stopping

After a reboot only `db` and `dashboard` start by themselves. The agent and the IB Gateway never start on their own, not after a reboot and not after a crash: start them by hand. Orders already at IBKR (stops, targets) stay active there while the agent is down, but nothing watches them.

Start:

| Step | Command | What it does | Expect |
|---|---|---|---|
| 1 | `make up` | Starts all containers in the background; recreates those whose `.env` or image changed | |
| 2 | `docker compose ps` | Lists the containers | `db`, `agent`, `dashboard`, `ib-gateway` all `Up`; `Restarting` means it crashes again and again (8.6) |
| 3 | `docker compose logs -f ib-gateway \| grep -v "Connection refused"` | Follows the gateway's login | `Login has completed` within 30–90 seconds; confirm a 2FA prompt on the phone. "Connection refused" is normal until then. |
| 4 | `make logs` | Follows the agent's start | `agent.started`, `scheduler.planned`, `ibkr.connected` (one `ibkr.connect_failed` before it is normal) |
| 5 | Telegram `/status` | Asks the agent | Mode `paper`, kill switch `active`, current data dates, next jobs, `IB Gateway: connected` |

**Stop:** `make down`. Stops and removes the containers; the database, `.env`, `secrets/` and `backups/` stay. Do it outside trading sessions: orders already at IBKR stay active there, but nothing watches them while the app is down.

**Restart only the agent:** `docker compose restart agent` (keeps the old `.env`; after changing `.env` use `make up`).

---

## 6. Testing that everything works

Run these once, in this order, and again when you suspect a problem in that area.

| # | Test | Do | Expect | Proves | Done |
|---|---|---|---|---|---|
| 1 | Containers | `docker compose ps` | All four `Up` | The app and database run | ☐ |
| 2 | Telegram | `/status`, `/help` | Status and command list | The bot reaches you; the scheduler runs | ☐ |
| 3 | Heartbeat | `/status` | `Heartbeat: ok, … ago`; the check on healthchecks.io is green | You get an e-mail if the Zenbook dies | ☑ |
| 4 | Market data | `docker compose run --rm agent trading-agent quality` | A short report, ideally `0 blocked symbols` | Prices are complete and plausible | ☑ |
| 5 | Dashboard | Open the address (section 2) with Tailscale on, then off | Pages load with Tailscale; nothing without it | Works, and only inside your tailnet | ☐ |
| 6 | IBKR | `docker compose run --rm -e IB_CLIENT_ID=12 agent trading-agent ibkr-check` | `Positions: 0`, `Contracts without conid: none`; in market hours `AAPL: delayed, last …` | The agent reads the paper account and gets prices | ☑ |
| 7 | AI analysts | `docker compose run --rm agent trading-agent analyse --top 3` | Rating, plan and earnings stance per stock; cost a few cents | The Gemini key works; answers pass the checks | ☐ |
| 8 | Agent pipeline | `docker compose run --rm agent trading-agent propose --market US`, then `/proposals`, `/why SYMBOL`, `/review` | Ranked proposals; your label stored | All agents work together. Never ordered: placement uses only that morning's scan. | ☐ |
| 9 | Kill switch | See below | `Kill switch: active` at the end | You can stop it from the phone; only you can restart it | ☐ |
| 10 | Backups | See below | `backup ok`; row counts twice | Backups are written, readable and decryptable | ☐ |
| 11 | Gateway overnight | Next morning: `docker compose logs --since 12h ib-gateway \| grep -v "Connection refused" \| tail -50` | The 23:45 restart and a new login without you | The gateway runs unattended | ☐ |
| 12 | A trading day | Watch Telegram (section 3) for two trading days | 🧠 📰 📤 🌙 at their times; 🟢 🔴 ⌛ when something fills; every order with its `TA-SYMBOL-kind-…` reference in TWS or Client Portal. "No trades" is a valid day. | The whole chain runs by itself, at IBKR | ☐ |
| 13 | Chaos tests | With `/positions` empty: [IMPLEMENTATION.md](IMPLEMENTATION.md) 15.5 step 17 | The three chaos tests pass | No order is lost, duplicated or left without a stop | ☐ |
| 14 | Paper phase | 3 months of daily routine | Results you trust in the dashboard | Ready for the go-live checklist ([IMPLEMENTATION.md](IMPLEMENTATION.md) section 18) | ☐ |
| 15 | AI tracing | After setup step "Tracing" (9.3): `/status`; `docker compose run --rm agent trading-agent propose AAPL`, then Langfuse → Tracing → Traces; dashboard → Day → AAPL → technical step → Open trace | `LLM tracing: on (cloud.langfuse.com, paper)`; a `cli propose` trace (environment `paper`) with one step per AI answer; the dashboard shows the LLM calls with their messages and the link opens the trace | Every AI call is recorded and can be followed step by step | ☐ |

**Test 9, kill switch** (not at 09:15 or 15:45):

1. `/pause test`, then `/status` → `paused (test)`. `/resume`.
2. `/stop` and confirm → `/status` shows `halted`.
3. On the Zenbook: `docker compose run --rm agent trading-agent reset` prints a code.
4. Within 15 minutes: `/reset CODE` → `Kill switch: active`. Don't leave it halted, or nothing trades.

**Test 10, backups** (needs setup step 9.4):

```sh
OFFSITE=1 make backup                                                  # backup now, plus the encrypted copy
make restore-check FILE=$(ls -1t backups/*.dump | head -1)             # restores into a scratch database, then drops it
# paste the private key from your password manager into /tmp/age-key.txt, then:
AGE_KEY=/tmp/age-key.txt make restore-check FILE=$(ls -1t backups/offsite/*.age | head -1)
shred -u /tmp/age-key.txt
```

---

## 7. Updating

When new code is on GitHub and its CI run is green, outside trading sessions or after `/pause`:

```sh
git pull          # download the new code
make build        # build a new image (under a minute when only code changed)
make migrate      # update the database tables if needed; otherwise does nothing
make up           # replace the running containers
```

Then `/status` and `/resume` if you paused. A restart triggers a reconciliation with IBKR, which is why updates happen outside sessions.

- Only `.env` changed: `make up`.
- Only documentation changed: `git pull`.
- `config/` and `prompts/` are part of the code: they need the build.

---

## 8. Troubleshooting

**8.1 `git pull` says "divergent branches" or refuses.** Something was changed on the Zenbook, which should only receive code. Reset it to GitHub's state; `.env`, `secrets/`, `backups/` and the database aren't touched:

```sh
git fetch origin && git reset --hard origin/main
```

**8.2 The gateway doesn't log in.** Look at its screen first (8.3).

| Symptom | Fix |
|---|---|
| Only "Connection refused" for more than 5 minutes | The login didn't finish; the screen shows why |
| "Invalid username or password" | Check `TWS_USERID` in `.env`, run `make tws-password` again, then `docker compose up -d --force-recreate ib-gateway`. Stop the gateway (`docker compose stop ib-gateway`) meanwhile; repeated failures can lock the login. |
| Waits for "second factor" | Confirm the 2FA prompt in the IBKR app |
| "Existing session" | The same username is logged in elsewhere; log out there |

**8.3 The gateway's screen.** On the Zenbook desktop, open **Remmina**, protocol VNC, server `localhost:5900`. The password is the first 8 characters of the VNC secret: `head -c 8 ~/projects/trading-agent/secrets/vnc_password; echo`.

**8.4 `ibkr-check` times out.** The running agent uses client id 11; the check needs its own: `-e IB_CLIENT_ID=12`.

**8.5 Market data warnings (10089, 354, 10167, `tickType 88`).** No paid real-time data; the app uses free delayed quotes and the last close when none arrive. Nothing to fix.

**8.6 A container shows `Restarting`.** `docker compose logs <name> | tail -50` shows why; usually a wrong value in `.env`.

**8.7 Heartbeat `FAILING`.** `docker compose logs agent | grep heartbeat.failed | tail -3`. `UnsupportedProtocol`: the value isn't a clean URL (quotes, a comment after it, missing `https://`). `404`: wrong check id. After fixing `.env`: `make up`.

**8.8 No Telegram messages.** `docker compose ps` (agent running?), `make logs` (errors?), `TELEGRAM_OWNER_CHAT_ID` set in `.env`? After changes: `make up`.

**8.9 CI failure e-mail.** The latest code on GitHub failed a check. Don't update the Zenbook to it; wait for a green run (github.com/klausrossmann/trading-agent/actions).

---

## 9. Reference: the parts, logins and setup

### 9.1 The parts

| Part | Where | Reached from |
|---|---|---|
| `agent` container | Zenbook | Telegram; `make logs` |
| `dashboard` container | Zenbook, `127.0.0.1:8501` | Your tailnet via `tailscale serve` (https) |
| `db` container (PostgreSQL) | Zenbook, only inside Docker | `docker compose exec db psql -U dashboard -d trading` (read-only) |
| `ib-gateway` container | Zenbook; API port 4004 only inside Docker, VNC `127.0.0.1:5900` | The agent; Remmina on the Zenbook |
| Zenbook shell | Zenbook | Its desktop; SSH only from the tailnet (`<user>@100.116.207.96`) |
| IBKR paper account | IBKR's servers | Client Portal, IBKR app |
| Code | GitHub (`klausrossmann/trading-agent`), copy on the Zenbook | `git pull` with a read-only deploy key |
| Heartbeat | healthchecks.io | Its website (Checks) and alert e-mails; the ping URL only receives pings (3.1) |
| AI traces | Langfuse Cloud, EU region (free plan, 30 days) | cloud.langfuse.com, project `trading-agent`; "Open trace" links on the dashboard and in `/why` |

Nothing on the Zenbook accepts connections from the internet or the home network (`ufw`); other devices come in only through Tailscale.

### 9.2 Logins

| Login | Kept in | Used for |
|---|---|---|
| IBKR username and password | `.env` (`TWS_USERID`), `secrets/tws_password` | The gateway's automatic login in paper mode |
| Database and VNC passwords | `secrets/` | Each container reads only its own |
| API keys (Gemini, FRED, Finnhub, Telegram, heartbeat) | `.env` | The agent's outgoing calls |
| Langfuse login | Your password manager | Looking at traces in the browser |
| Langfuse API keys | `secrets/trace_headers` (`make trace-headers`) | The agent sending traces |
| Backup decryption key | Only your password manager | Restoring an encrypted backup |

`.env` and `secrets/` never go to GitHub; `.env` is `chmod 600` and `secrets/` `chmod 700`.

### 9.3 One-time setup

Moved to [ZENBOOK.md](ZENBOOK.md): accounts and keys (1), preparing the machine (2), installing the app and every `.env` value (3), the first start (4), and the first update to the simplified version (6.1).

### 9.4 Backups

```sh
sudo apt install age
age-keygen -o /tmp/age-key.txt               # prints "Public key: age1…"
cat /tmp/age-key.txt                         # copy the whole content into your password manager
shred -u /tmp/age-key.txt                    # the private key must not stay on the Zenbook
nano .env                                    # BACKUP_AGE_RECIPIENT=age1…
crontab -e                                   # add the line below
```

```
15 3 * * * cd ~/projects/trading-agent && scripts/backup.sh >> backups/backup.log 2>&1
```

Every night at 03:15 the database is dumped into `backups/` (14 days kept). On Sundays an encrypted copy goes to `backups/offsite/`; copy those files to any cloud drive. Only the private key in your password manager can decrypt them. Then run test 10. Optional: a second healthchecks.io check (period 1 day, grace 2 hours) as `BACKUP_HEARTBEAT_URL`.
