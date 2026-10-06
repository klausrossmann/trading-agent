# Trading Agent

A personal, self-hosted robot that **swing trades** large US and German stocks through Interactive Brokers (IBKR). It buys stocks it expects to rise over the next days to three weeks and sells them again, with every exit planned before it buys.

**Goal: automated paper trading first, then automated trading with real money**, once the paper results prove that it works.

> Personal project for a single account. Not financial or tax advice, and not a service for others.

**New here? Read [HOW-IT-WORKS.md](HOW-IT-WORKS.md).** It explains the whole system in plain language: the daily workflow, when it buys and sells, how the AI agents work together, who has the final say, and every setting you can change. It also has a glossary of trading terms.

## What it does

Every trading day, without anyone pressing a button:

1. **Collects data**: daily prices, earnings dates, exchange rates, macro data, and news on the stocks it holds. Every number is stored with its source and time.
2. **Finds candidates (code)**: a fixed rule, "a stock in an uptrend that has dipped a little", screens about 140 stocks (S&P 100 and DAX 40). The 10 strongest setups per market go on.
3. **Analyses them (AI)**: code computes indicators and a menu of named price levels (moving averages, support, swing lows and so on). A technical analyst and an earnings analyst, both LLMs, judge the setup and pick entry, stop and target **from that menu**. They can't invent a price.
4. **Proposes and challenges (AI)**: a proposer decides whether to trade and writes the reasoning. A critic, from a different AI vendor, looks for reasons not to and can veto. A portfolio manager ranks what's left.
5. **Decides in plain code**: a risk engine has the final say. It checks 9 groups of hard limits and decides how many shares to buy. At most €15 can be lost per trade, with at most 4 positions and a kill switch at a 15 % loss. AI output can't bypass it.
6. **Trades**: approved trades go out as bracket orders, which combine a limit buy with a stop that limits the loss and a target that takes the profit. Stop and target sit at the broker, so they work even if the agent is down. The orders go to a simulator until the IBKR paper login is set up.
7. **Manages positions**: moves the stop up to the buy price once a trade is 1R in profit, and sells after 15 sessions if neither stop nor target was hit. An AI reviews positions when there's important news or a report coming up, and advises you; selling on that advice is your call (`/exit`).
8. **Reports**: Telegram brings a morning briefing, the proposals, orders and fills, an evening digest and a weekly report. A read-only dashboard is reachable over Tailscale. You can step in any time with `/pause`, `/stop` or `/exit`.
9. **Measures itself**: the AI's trades are compared with the plain rule-based strategy and with buy and hold, after fees and AI costs. Real money only follows if the AI beats the rules.

## A multi-agent system showcase

The project is built as a **hybrid multi-agent system**. Seven LLM agents (technical analyst, earnings analyst, proposer, critic, portfolio manager, news triage, position reviewer) work with five deterministic agents (screen, risk engine, executor, reconciler, scheduler) and one human.

- **Orchestrator–worker with a veto chain.** A code orchestrator routes typed messages between the agents. Any agent can stop a trade, but only the deterministic risk engine can approve one.
- **Generator–critic across vendors.** A Claude critic challenges the Gemini proposer, so their mistakes are less likely to overlap.
- **Stateless agents, shared memory.** The database is the blackboard: agents that run hours or weeks apart communicate through it. Every agent run is memoised and stored with its input, prompt version and model.
- **Contained by design.** Agents have no tools and no path to the broker, which CI enforces with import contracts. Every answer is validated before the next agent sees it.
- **Evaluated against an ablation.** The same screen without agents trades alongside, and the agents must beat it after costs.

Details: [MAS-DESIGN.md](MAS-DESIGN.md) (architecture, communication, shared memory, coordination, trust, observability, evaluation, code map).

## From paper to real money

| Phase | What runs | Moves on when |
|---|---|---|
| Build (done in the repo) | Data, calculators, baseline strategy, AI agents, risk engine, simulator, Telegram, dashboard, reports | Each milestone's "done when" is met |
| Paper trading (next) | Fully autonomous on the IBKR **paper** account, with a €1,000 budget (US) and a €5,000 notional budget (Germany), real fee modelling | ≥ 3 months, ≥ 50 closed trades, positive after all costs, beats the rule-based baseline |
| Live | Fully autonomous with **€1,000 of real money**, US stocks only, lean LLM budget | 3 months in line with paper results |
| Scale | More capital, EU stocks live, later options (debit spreads) | Same KPIs hold |

Paper and live use the same code, risk limits and fee model. Live mode needs three switches at once: `APP_MODE=live`, the gateway in live mode, and a confirmation code sent via Telegram.

## Status (2026-10-05)

All milestones up to M9 and the M10 preparation are built and tested. The stack runs on the Zenbook with the market data loaded and the IBKR paper account connected (2026-10-05). The remaining checks are in [RUNNING.md](RUNNING.md) section 6.

| Milestone | State |
|---|---|
| M0 Bootstrap: project, Docker stack, database, heartbeat, CI | done; running on the Zenbook |
| M1 Data foundation: universe, prices, FX, macro, earnings, quality checks | done; real data loaded on the Zenbook |
| M2 Calculators: indicators, levels, level menu, fees, sizing | done |
| M3 Baseline strategy, simulator, `baseline_sim` book, backtest report | done; no edge found, kept as the bar to beat ([IMPLEMENTATION.md](IMPLEMENTATION.md) 6.3) |
| M4 Telegram: owner-only bot, alerts, morning briefing | done in the repo; bot token and chat id pending |
| M5 LLM modules: technical and earnings analysts, budget guard, cache, validators, 30 eval cases | done in the repo; first real scan and evals pending |
| M6 IBKR read side and dashboard | done; the paper account is connected, delayed quotes arrive; the overnight-restart check is pending |
| M7 Agents: proposer, critic, portfolio manager, `/review` labels, evening digest | done in the repo; the week of labelled proposals is pending |
| M8 Risk engine, kill switch, orders, simulator broker, daily trading cycle | done in the repo; `agent_paper` trades through the simulator. IBKR orders and the chaos tests follow after a few stable days. |
| M9 Paper operations: weekly report and go-live gate, `/positions`, `/pnl`, news triage, position review, `/exit`, backups, dashboard Risk/Evaluation/Reports | done in the repo; the 3-month paper period hasn't started |
| M10 Mini PC and go-live | tax report done; the rest follows after the paper phase |

## Documentation

- [HOW-IT-WORKS.md](HOW-IT-WORKS.md): **start here**. The system in plain language: workflow, buy and sell rules, agents, safety nets, every setting.
- [RUNNING.md](RUNNING.md): **operating the app**: where to find what (dashboard, Telegram, logs), starting, testing, updating, troubleshooting.
- [MAS-DESIGN.md](MAS-DESIGN.md): the multi-agent system design: agent inventory, communication, shared memory, coordination, trust, evaluation, code map.
- [OBSERVABILITY.md](OBSERVABILITY.md): LLM observability. Every call with its messages in Postgres; every run as an OpenTelemetry trace in Langfuse.
- [CONCEPT.md](CONCEPT.md): why it's designed this way (broker choice, risk limits, costs, evaluation, roadmap, research).
- [IMPLEMENTATION.md](IMPLEMENTATION.md): how it's built (architecture, data model, risk engine, deployment, runbook, build plan).

## Development

Code is written on a development machine (no secrets) and runs on an always-on Linux host with Docker.

```sh
uv sync                  # Python 3.13 environment
make lint                # ruff, pyright, import-linter contracts
make test                # unit tests
make test-db             # DB tests against a throwaway Postgres container
```

On the runtime host (step by step, with explanations: [RUNNING.md](RUNNING.md)):

```sh
make secrets                              # create database password files in secrets/
cp .env.example .env && chmod 600 .env    # then fill in the values
docker compose up -d db && make migrate && make up
docker compose run --rm agent trading-agent backfill   # load 6 years of data
docker compose run --rm agent trading-agent analyse    # LLM analysis of today's top setups
uv run pytest -m llm tests/evals                       # golden-case evals with the real model
```
