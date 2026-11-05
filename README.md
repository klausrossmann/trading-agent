# Trading Agent

A personal, self-hosted robot that **swing trades** large US and German stocks through Interactive Brokers (IBKR). It buys stocks it expects to rise over the next days to three weeks and sells them again, with every exit planned before it buys.

**Goal: automated paper trading first, then automated trading with real money**, once the paper results prove that it works.

> Personal project for a single account. Not financial or tax advice, and not a service for others.

## Documentation

| Document | Read it for |
|---|---|
| [MANUAL.md](MANUAL.md) | **Running it**: setup on the Zenbook, `make up`, where the dashboard is, Telegram, the daily routine, every setting, tests, troubleshooting, backups. Section 1 is a one-page quick reference. |
| [HOW-IT-WORKS.md](HOW-IT-WORKS.md) | **What it does and when**: the trading day hour by hour, from data to a trade, when it buys and sells, the agents, start-up, late starts, outages, safety nets. |
| [CODE.md](CODE.md) | **The code**: one folder per component (market data, news, screen, analysts, proposer, critic, risk, IBKR, portfolio, Telegram, dashboard), what each file does, where to change what. |
| [docs/design/](docs/design/) | Background: why it's designed this way ([CONCEPT.md](docs/design/CONCEPT.md)), how it was built ([IMPLEMENTATION.md](docs/design/IMPLEMENTATION.md)), the multi-agent design ([MAS-DESIGN.md](docs/design/MAS-DESIGN.md)), AI tracing ([OBSERVABILITY.md](docs/design/OBSERVABILITY.md)). |

## What it does

Every trading day, without anyone pressing a button:

1. **Collects data**: daily prices, earnings dates, exchange rates, macro data, and company news (Finnhub for US, Yahoo for EU stocks).
2. **Finds candidates (code + AI)**: a fixed rule, "a stock in an uptrend that has dipped a little", screens about 140 stocks (S&P 100 and DAX 40). The 10 strongest setups per market go on. A News agent adds up to 3 stocks per market with a strong positive news catalyst.
3. **Analyses them (AI)**: a technical analyst and an earnings analyst judge each setup and pick entry, stop and target **from a menu of price levels** computed by code. They can't invent a price.
4. **Proposes and challenges (AI)**: a proposer decides whether to trade; a critic from a different AI vendor looks for reasons not to and can veto. Code ranks what's left by confidence × R:R.
5. **Decides in plain code**: a risk engine checks 9 groups of hard limits and decides how many shares to buy. AI output can't bypass it.
6. **Trades at IBKR**: approved trades go out as bracket orders (limit buy + stop + target). Stop and target sit at the broker, so they work even if the agent is down. Without a gateway connection nothing is placed.
7. **Manages positions**: stop to breakeven at +1R, time stop after 15 sessions, and a news exit when the News agent is at least 70 % sure the reason for the trade is gone.
8. **Reports**: Telegram brings a morning briefing, proposals, orders, fills and an evening digest. The dashboard shows, for any day, every stock's path through every step with its inputs and outputs.

## A multi-agent system showcase

Five LLM agents (technical analyst, earnings analyst, proposer, critic, News agent) work with deterministic agents (screen, risk engine, executor, reconciler, scheduler) and one human. A code orchestrator (`orchestrator.py`) runs the day stage by stage; any agent can stop a trade, but only the deterministic risk engine can approve one. The agents have no tools and no path to the broker, which CI enforces with import contracts. Details: [MAS-DESIGN.md](docs/design/MAS-DESIGN.md).

## From paper to real money

| Phase | What runs | Moves on when |
|---|---|---|
| Build (done in the repo) | Data, screen, AI agents, News agent, risk engine, IBKR orders, Telegram, dashboard | Each milestone's "done when" is met |
| Paper trading (next) | Fully autonomous on the IBKR **paper** account, US and Germany, one notional budget of €15,000 | ≥ 3 months, ≥ 50 closed trades, positive after all costs (fees and AI) |
| Live | Fully autonomous with **€1,000 of real money**, US stocks only | 3 months in line with paper results |
| Scale | More capital, EU stocks live | Same KPIs hold |

Paper and live use the same code, risk limits and fee model. Live mode needs three switches at once: `APP_MODE=live`, the gateway in live mode, and a confirmation code sent via Telegram.

## Status (2026-11-05)

Everything is built and tested. In November 2026 the system was simplified (every order goes to IBKR or nowhere, a step log shows what happened to each stock, a News agent triggers buys and exits) and the code was reorganised by component. Next: the update on the Zenbook ([MANUAL.md](MANUAL.md) 5.4) and the tests in [MANUAL.md](MANUAL.md) 10. The build history is in [IMPLEMENTATION.md](docs/design/IMPLEMENTATION.md) 17–19.

## Development

Code is written on a development machine (no secrets) and runs on the Zenbook with Docker.

```sh
uv sync                  # Python 3.13 environment
make lint                # ruff, pyright, import-linter contracts
make test                # unit tests (100 % branch coverage on the risk engine and the order code)
make test-db             # tests against a throwaway Postgres container
make dev-up              # local stack with a fake AI model and no IBKR, dashboard on http://127.0.0.1:8502
```

On the Zenbook: `git pull && make up` ([MANUAL.md](MANUAL.md)).
