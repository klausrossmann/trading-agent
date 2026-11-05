# What happens once the agent runs

Everything the agent does after `make up`, and when. Times are Berlin time on a normal trading day (Xetra 09:00–17:30, NYSE 15:30–22:00). The source of truth is [config/schedule.yaml](config/schedule.yaml); the code that runs each step is named so you can look it up in [CODE.md](CODE.md). Telegram `/status` shows the next jobs at any time.

**Contents**

1. [Start-up](#1-start-up)
2. [Always running in the background](#2-always-running-in-the-background)
3. [A trading day, hour by hour](#3-a-trading-day-hour-by-hour)
4. [What each stage does in detail](#4-what-each-stage-does-in-detail)
5. [Weekends, holidays, half-days, summer time](#5-weekends-holidays-half-days-summer-time)
6. [Starting late, stopping, restarting](#6-starting-late-stopping-restarting)
7. [When something is down](#7-when-something-is-down)

---

## 1. Start-up

`make up` starts four containers. `db` and `dashboard` also come back by themselves after a reboot; `agent` and `ib-gateway` never do: they start only with `make up`.

| # | What | Where | You see |
|---|---|---|---|
| 1 | `db` (PostgreSQL) starts and reports healthy; the agent waits for it | `compose.yaml` | |
| 2 | `ib-gateway` starts and logs in to the IBKR paper account (30–90 s; possibly a 2FA prompt on your phone) | `ib-gateway` container | `docker compose logs ib-gateway`: `Login has completed` |
| 3 | The agent reads `.env`, the secrets and `config/*.yaml`, sets up logging and tracing | `__main__.run`, `settings.py` | |
| 4 | It writes `agent.started` to the audit log and syncs `config/universe.yaml` into the database (new stocks added, removed ones deactivated) | `scheduler.serve`, `data/ingest.sync_universe` | log `universe.synced` |
| 5 | It builds the contexts (risk rules, models, agents, news sources) and starts the Telegram bot | `contexts.py`, `notify/telegram.py` | |
| 6 | It starts the IBKR link in the background. Once connected: contract ids for new stocks, then a **reconciliation** (IBKR positions vs. the book; any difference halts trading) | `ibkr/link.py`: `BrokerLink`, `on_connect` | log `ibkr.connected`, `reconcile.done`; ⚠️ on a mismatch |
| 7 | It plans the jobs: fixed-time jobs (cron) and today's session jobs from the exchange calendars | `scheduler.build_scheduler`, `plan_session_jobs` | log `scheduler.planned` |
| 8 | It sends ▶️ to Telegram | | `▶️ Agent started (paper), kill switch active` |

The dashboard (`dashboard` container) is independent: it only reads the database and works whether the agent runs or not.

---

## 2. Always running in the background

| What | How often | Code | You see |
|---|---|---|---|
| Heartbeat ping to healthchecks.io | every 5 min | `notify/heartbeat.py` | healthchecks.io e-mails you if pings stop for 20 min |
| IBKR connection check, reconnect with backoff (30 s up to 5 min) | every 30 s | `ibkr/link.py`: `BrokerLink.step` | 🔌 after 10 min down, 🔌 again when it's back; every reconnect runs contract ids + reconciliation |
| Telegram commands and buttons | whenever you send one | `notify/telegram.py`, `controls.py`, `trading.py`, `journal.py` | the answer |
| Job watchdog | on every job | `scheduler.alert_on_job_failures` | ⚠️ `Job X failed: ...` or `Job X missed its run time and was skipped` |
| Planning the day's session jobs | 00:05 every night (and at start) | `scheduler.plan_session_jobs` | log `scheduler.planned` |
| IB Gateway's own restart | 23:45 every night | `ib-gateway` container (`AUTO_RESTART_TIME`) | a short disconnect; the agent reconnects and reconciles |

---

## 3. A trading day, hour by hour

| Time | Job | What happens | AI | Telegram | Dashboard |
|---|---|---|---|---|---|
| 00:05 | (planner) | Today's session jobs are planned from the exchange calendars | | | |
| 07:00 | `ingest_macro` | US macro data (FRED: yields, VIX, credit spreads, ...) and the ECB deposit rate | | | Day → Data freshness |
| 07:00, then every 30 min to 22:30 | `news_positions` | News on **held** stocks → News agent → exit or hold (4.1) | ✓ | 📰 important news, 🚪 news exit, 🧐 advice | Day → news column |
| 07:15 | `ingest_earnings` | Earnings dates and results (Yahoo) | | | |
| 07:45 | `news_eu` | News on every **German stock not held** → News agent → buy signals (4.2) | ✓ | 📰 buy signal | Day → news column |
| 08:15 | `scan_eu` | **EU scan**: screen + news candidates → technical → earnings → proposer → critic → rank → proposals (4.3) | ✓ | 🧠 proposals | Day: screen to rank |
| 08:30 | `briefing` | Morning briefing: markets, EUR/USD, earnings ahead, the screen's setups, data status | | 📰 | |
| 09:00 | | Xetra opens | | | |
| 09:00, then every 10 min to 22:50 | `monitor` | IBKR sync: order states and fills into the book (4.5) | | 🟢 bought, 🔴 sold, ⌛ entry ended | Day → order, fill; Portfolio |
| 09:15 | `place_eu` | **EU placement**: today's EU proposals in rank order → risk engine → bracket orders at IBKR (4.4) | | 📤 placed and rejected | Day → risk, order |
| 14:15 | `news_us` | As at 07:45, for US stocks | ✓ | 📰 | |
| 14:45 | `scan_us` | **US scan**, as at 08:15 | ✓ | 🧠 | |
| 15:30 | | NYSE opens | | | |
| 15:45 | `place_us` | **US placement**, as at 09:15 | | 📤 | |
| 17:30 | | Xetra closes | | | |
| 18:00 | `ingest_eod_eu` | EU closing prices, ECB EUR/USD, data quality check | | ⚠️ newly blocked stocks | Day → Data freshness |
| 18:05 | `execution_eu` | **EU end of day**: IBKR sync, stop to breakeven / trailing, time stops, book, loss limits (4.6) | | 🟢 🔴 ⌛, ⏸ if a loss limit hit | Portfolio, Risk |
| 22:00 | | NYSE closes | | | |
| 22:30 | `ingest_eod_us` | US closing prices, data quality check | | | |
| 22:35 | `execution_us` | **US end of day**, as at 18:05 | | | |
| 22:40 | `reconcile` | IBKR positions vs. the book; any difference halts | | ⚠️ + ⛔ on a mismatch | Risk → kill switch |
| 23:05 | `evening_digest` | The day's proposals, orders, fills, sales, labels to do, AI spend | | 🌙 | |
| 23:45 | | IB Gateway restarts itself (2FA may be needed about once a week) | | 🔌 only if it stays down 10 min | |
| 03:15 | (host crontab, not the agent) | Database backup | | | |

---

## 4. What each stage does in detail

### 4.1 News on held stocks (`news_positions`, `news.watch_positions`)

1. Takes every held position (filled, not being sold).
2. Fetches the last 2 days of news per stock: Finnhub for US (needs `FINNHUB_API_KEY`), Yahoo for EU. New headlines are stored once.
3. A stock is read if it has new headlines, or (once a day) if its next report is within 3 sessions.
4. The News agent gets, per stock: the newest new headlines (at most 5, as untrusted text), the position (entry, stop, target, last closes, R now, report date), the original thesis and invalidation, and earlier news. Up to 8 stocks per call.
5. Per stock it answers: importance per headline, `exit` or `none`, a confidence and a reason.
6. Code acts:
   - `high` headline → 📰 with the headline and why.
   - `exit` with confidence ≥ 0.7 → market sell at IBKR now (at the next open if the market is closed) → 🚪. The sale arrives later as 🔴 `(news)`.
   - `exit` below 0.7 → 🧐 advice with `/exit SYMBOL`; nothing is sold.
   - IBKR not connected → 🔌, nothing is sold.
7. Every verdict is a `news` step on the Day page.

### 4.2 News before a scan (`news_eu` / `news_us`, `news.scan_news`)

1. Every stock of that market that isn't held (about 40 EU, 100 US): news of the last 2 days. Finnhub is paced to 60 requests a minute, so the US run takes 2–3 minutes.
2. The News agent reads the stocks with new headlines in batches of 8 and answers `buy` or `none` per stock.
3. A `buy` with confidence ≥ 0.6 (and at least one `high` headline) is stored as a buy signal → 📰. It's valid for 24 hours.

### 4.3 Scan (`scan_eu` / `scan_us`, `orchestrator.scan`)

1. **Screen**: every stock of the market against the pullback rules on the last closing price; blocked (data problem), blacklisted and held stocks are dropped; the top 10 by relative strength go on (5 in lean mode). Stocks with a news buy signal only need the basic rules (fresh prices, price, liquidity, no report within 3 sessions); up to 3 of them join.
2. **Technical analyst** per stock: only `buy` / `strong_buy` go on.
3. **Earnings analyst** per stock: advice for the proposer.
4. **Proposer**: propose (entry, stop, target from the level menu) or pass. News candidates come with their news as `catalyst`.
5. **Critic**: `blocking` stops the proposal; otherwise its confidence change is applied.
6. **Rank**: confidence × R:R, best first.
7. The proposals and every stock's steps are stored → 🧠. **No orders yet.**

Answers already given for the same input are reused from the cache at no cost (e.g. a re-run).

### 4.4 Placement (`place_eu` / `place_us`, `orchestrator.place`)

Only today's proposals for that market (from this morning's scan), in rank order:

1. IBKR not connected → every proposal gets an error step, 🔌, nothing is placed.
2. Per proposal: news trade but already 1 placed today → stopped.
3. **Risk engine**: 9 checks (kill switch, trading window, instrument, levels, size, fees, portfolio limits, loss limits, orders per day). Rejected → a stopped risk step with the reason.
4. Approved → **bracket order** at IBKR: buy limit valid until the next session's close, stop and target attached (both stay at IBKR, good till cancelled). Order references look like `TA-AAPL-entry-1a2b3c4d`; that's how you find them in the IBKR app.
5. 📤 with what was placed and rejected.

A placement more than 2 minutes late is skipped, so orders never go out at an unexpected time.

### 4.5 Monitor (`monitor`, `orchestrator.monitor`)

Every 10 minutes while connected: reads IBKR's orders and fills, moves each bracket through its states (working, filled, exiting, closed, expired, cancelled), records fills and IBKR's messages, and sends 🟢 / 🔴 / ⌛.

### 4.6 End of day (`execution_eu` / `execution_us`, `orchestrator.end_of_day`)

After that market's closing prices are stored:

1. IBKR sync (as 4.5).
2. For each filled position of that market, on the day's bar:
   - held 15 sessions → market sell at the next open (time stop);
   - high reached +1R → stop to the entry price (breakeven);
   - optional ATR trailing stop (`trail_atr`, off by default).
   Stops only ever move up; the new stop is sent to IBKR.
3. The book is rebuilt from the brackets and fills, and the account's equity is stored.
4. Loss limits at the close: a day's loss of 3 % pauses new entries through the next session, a week's loss of 6 % until Monday, a drawdown of 15 % halts (⏸ / ⛔).

Without an IBKR connection steps 1–2 are skipped (🔌); the book is still rebuilt.

---

## 5. Weekends, holidays, half-days, summer time

- **Weekends**: nothing runs except the heartbeat and the IBKR link (and the gateway's restart).
- **Holidays**: session jobs (news before the scan, scan, placement, end of day, digest, reconciliation) follow each exchange's calendar and simply don't run when that exchange is closed. On a German holiday with NYSE open, only the US jobs run, and the other way round. The morning briefing is skipped when both are closed.
- **Half-days**: jobs anchored to the close move with it.
- **Summer time**: Europe and the US switch on different dates. For one to three weeks in spring and autumn, all US jobs run one hour earlier in Berlin time (e.g. the US scan at 13:45). Nothing to do.
- Fixed-time jobs (`ingest_macro`, `ingest_earnings`, `briefing`, `monitor`, `news_positions`) run Monday to Friday.

---

## 6. Starting late, stopping, restarting

**Start before 07:45** on a trading day to get the full EU day, and before 14:15 for the full US day. Each job has a grace time; a job whose time passed longer ago than that is skipped and reported once as ⚠️ "missed":

| Job | Runs late if started within | Otherwise |
|---|---|---|
| `place_eu`, `place_us` | 2 min | Skipped: no orders that day for that market. Today's proposals are never placed later. |
| `news_positions` | 20 min | The next half-hour run catches up |
| `news_eu`, `news_us` | 25 min | Skipped; the scan runs without news candidates |
| `scan_eu`, `scan_us` | 30 min | Skipped: no proposals, so no orders that day for that market |
| `briefing` | 60 min | Skipped |
| `monitor` | 5 min | The next run catches up |
| Data jobs, end of day, reconciliation, digest | 6 hours | Run late |

**Stop**: `make down`. The agent sends ⏹, stops the jobs, disconnects from IBKR and writes `agent.stopped` to the audit log. Orders already at IBKR (buy limits, stops, targets) **stay active there**, but nothing watches them, moves stops or sells by time stop or news. Stop outside sessions if you can.

**Restart** (after an update or a crash): `make up`. On reconnect the agent reconciles with IBKR; fills that happened while it was down are picked up by the next sync, because it compares IBKR's filled quantities with its own.

---

## 7. When something is down

| What's down | Effect | You see |
|---|---|---|
| IB Gateway / IBKR | No placements (error step per proposal), no stop moves, no time stops, no news exits; stops and targets already at IBKR keep working; scans, news reading and data jobs continue | 🔌 after 10 min; 🔌 per skipped placement / end of day / news exit |
| Internet | All outside calls fail; the jobs report errors | ⚠️ per failed job; heartbeat e-mail after 20 min |
| A data source (Yahoo, ECB, FRED, Finnhub) | That job reports a failure; the others continue. Stale prices block the affected stocks from the scan | ⚠️ |
| AI provider | Retries after 20 s and 60 s; then that stock is skipped today | 🧠 lists skipped stocks |
| AI budget used up (100 %) | No AI calls until next month: no scans, no news reading | `/budget`, 🧠 empty |
| Telegram | Messages are logged instead; trading continues | nothing in Telegram; `make logs` |
| The Zenbook | Nothing runs; stops and targets at IBKR keep working | heartbeat e-mail after 20 min |
| A kill switch is on (paused/halted) | No new entries; held positions keep their stops and targets; a halt also cancels unfilled entries | ⏸ / ⛔, `/status` |
