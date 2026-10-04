# Trading Agent

A personal, self-hosted trading agent for **swing trading** US and German stocks (holding periods of days to weeks) through Interactive Brokers.

**Goal: automated paper trading first, then automated real-money trading**, once the paper results prove that it works.

> Personal project for a single account. Not financial or tax advice, and not a service for others.

## What it does

Every trading day, without anyone pressing a button:

1. **Collects data**: daily prices, earnings dates, macro data, exchange rates and news, each with its source and timestamp.
2. **Finds candidates**: a rule-based strategy ("pullback in an uptrend") screens the universe (S&P 100 + DAX 40 to start) and ranks setups.
3. **Analyses them**: calculators compute indicators, support and resistance, and the levels where a trade could be entered, stopped out or taken profit. An LLM interprets the results and picks among those levels. It never invents prices.
4. **Proposes and challenges trades**: a proposer agent writes a trade thesis, a critic from a different AI vendor looks for reasons not to take it, and a portfolio manager ranks the survivors against what's already held.
5. **Checks risk in plain code**: a deterministic risk engine sizes every position and enforces hard limits: €15 risk per trade, at most 4 positions, fee and loss limits, and a kill switch at a 15 % drawdown. LLM output can't bypass it.
6. **Trades**: approved trades go to IBKR as bracket orders (limit entry, stop, target). The stop sits at the broker, so positions stay protected even if the agent is down.
7. **Manages positions**: monitors stops, time stops and the thesis of each open trade, and exits when it's invalidated.
8. **Reports**: a Telegram briefing in the morning, a digest in the evening and a weekly KPI report, plus a read-only dashboard reachable over Tailscale. Telegram commands: `/status`, `/positions`, `/pause`, `/stop` and more.
9. **Measures itself**: the LLM-driven trades are compared with the plain rule-based strategy and a buy-and-hold benchmark, after fees and LLM costs. If the LLM doesn't beat the rules, it is only used for briefings.

## From paper to real money

| Phase | What runs | Moves on when |
|---|---|---|
| Build (now) | Data, calculators, baseline strategy, LLM modules, Telegram, dashboard | Each milestone's "done when" is met |
| Paper trading | Fully autonomous on the IBKR **paper** account, with a €1,000 budget and real fee modelling | ≥ 3 months, ≥ 50 closed trades, positive after all costs, beats the rule-based baseline |
| Live | Fully autonomous with **€1,000 of real money**, US stocks only, lean LLM budget | 3 months in line with paper results |
| Scale | More capital, EU stocks live, later options (debit spreads) | Same KPIs hold |

Paper and live use the same code, risk limits and fee model. Live mode needs three switches at once: `APP_MODE=live`, the gateway in live mode, and a confirmation code sent via Telegram.

## Status

| Milestone | State |
|---|---|
| M0 Bootstrap: project, Docker stack, database, heartbeat, CI | done in the repo; Zenbook setup pending |
| M1 Data foundation: universe, prices, FX, macro, earnings, quality checks | done in the repo; first real backfill pending on the Zenbook |
| M2 Calculators: indicators, levels, level menu, fees, sizing | done |
| M3 Baseline strategy, simulator, `baseline_sim` book, backtest report | done; result and decision in [IMPLEMENTATION.md](IMPLEMENTATION.md) 6.3 |
| M4 Telegram: owner-only bot, `/status`, `/briefing`, alerts, morning briefing | done in the repo; bot token and chat ID pending on the Zenbook |
| M5 LLM modules: Gemini via PydanticAI, budget guard, cache, validators, `technical` and `earnings` modules, 30 golden eval cases | done in the repo; first scan and evals pending on the Zenbook |
| M6–M10 | see [IMPLEMENTATION.md](IMPLEMENTATION.md), section 17 |

## Documentation

- [CONCEPT.md](CONCEPT.md): what the system does and why (broker, risk limits, costs, evaluation, roadmap)
- [IMPLEMENTATION.md](IMPLEMENTATION.md): how it's built (architecture, data model, risk engine, deployment, build plan)

## Development

Code is written on the Mac (no secrets) and runs on an always-on Linux host with Docker.

```sh
uv sync                  # Python 3.13 environment
make lint                # ruff, pyright, import-linter contracts
make test                # unit tests
make test-db             # DB tests against a throwaway Postgres container
```

On the runtime host (full first-start runbook: [IMPLEMENTATION.md](IMPLEMENTATION.md) 15.5):

```sh
make secrets                              # create database password files in secrets/
cp .env.example .env && chmod 600 .env    # then fill in the values
docker compose up -d db && make migrate && make up
docker compose run --rm agent trading-agent backfill   # load 6 years of data
docker compose run --rm agent trading-agent analyse    # LLM analysis of today's top setups
uv run pytest -m llm tests/evals                       # golden-case evals with the real model
```
