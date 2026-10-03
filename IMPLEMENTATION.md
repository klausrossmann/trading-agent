# Trading Agent – Implementation Concept

> Status: Draft v0.2 (2026-10-03). Open questions from v0.1 answered (section 19); milestone M0 started.
> Builds on [CONCEPT.md](CONCEPT.md) v0.2. CONCEPT.md explains *what* the system does and *why*. This document explains *how* it is built.
> Disclaimer: technical concept, not financial or tax advice.

---

## 1. Fixed decisions

| Area | Decision |
|---|---|
| Runtime host (paper phase) | Asus Zenbook UX301LA: Ubuntu LTS, x86_64, 8 GB RAM, always plugged in |
| Runtime host (live phase) | x86_64 mini PC with 8–16 GB RAM and an SSD. Same stack, moved over. |
| Development | VS Code on the Mac, **code only, no secrets**. Pushed to the private GitHub repo `klausrossmann/trading-agent` and pulled on the Zenbook. |
| Who writes the code | Copilot writes most of it, including tests. You review every change and make the architecture decisions (section 16.1). |
| Language and tooling | Python 3.13, `uv` (environments and lockfile), `ruff`, `pyright`, `pytest`, `hypothesis`, `import-linter` |
| Broker | IBKR cash account; IB Gateway in Docker (`gnzsnz/ib-gateway`) plus `ib_async` |
| LLM | PydanticAI (2.x) with Gemini (workhorse) and Claude Sonnet (critic). LiteLLM isn't needed because PydanticAI already switches providers through a model string. |
| Notifications | Telegram bot (`python-telegram-bot` 22.x), long polling, restricted to your chat ID |
| Database | PostgreSQL 17. TimescaleDB isn't needed: about 600 symbols × 10 years of daily bars is only a few million rows. |
| Scheduling | APScheduler 3.x (`AsyncIOScheduler`); jobs are anchored to exchange sessions with `exchange_calendars` |
| Dashboard | Streamlit, read-only database role, reachable only through Tailscale |
| LLM tracing | Local only: `llm_calls`, `audit_log` and structured JSON logs. Hosted Pydantic Logfire is not used for now (section 19). |
| Starting universe | S&P 100 + DAX 40 (about 140 symbols). S&P 500, Nasdaq-100 and MDAX are added once scans are stable and cheap. |
| Architecture style | Modular monolith: a single async Python process with strict package boundaries |

Why Telegram instead of Signal: your Signal number belongs to your personal account. A number can have only one primary device, so registering it in `signal-cli` would sign your phone out. Telegram's official Bot API needs no extra number, supports buttons for confirmations, and works over outbound polling only. The trade-off is that bot chats are not end-to-end encrypted, so messages never contain account IDs or credentials (section 13).

---

## 2. System topology

```mermaid
flowchart LR
    subgraph Mac["Mac (development, no secrets)"]
        VS[VS Code + Copilot]
    end
    GH[(GitHub private repo<br/>+ Actions CI)]
    subgraph Zen["Zenbook (Ubuntu, Docker)"]
        direction TB
        AG[agent<br/>scheduler · data · modules · agents<br/>risk · execution · telegram]
        GW[ib-gateway<br/>IB Gateway + IBC]
        DB[(postgres)]
        DA[dashboard<br/>streamlit, read-only]
    end
    IBKR[(IBKR servers)]
    LLM[(Gemini / Anthropic APIs)]
    TG[(Telegram Bot API)]
    DATA[(Data APIs<br/>yfinance, EDGAR, FRED, Finnhub, RSS)]
    HC[(Heartbeat service)]
    Phone[Phone / Mac via Tailscale]

    VS -- git push --> GH
    GH -- git pull / deploy --> Zen
    AG <-- TWS API, internal network --> GW
    GW <--> IBKR
    AG <--> DB
    DA --> DB
    AG --> LLM
    AG <-- long polling --> TG
    AG --> DATA
    AG -- ping --> HC
    Phone -- Tailscale --> DA
    TG <--> Phone
```

### 2.1 Containers

| Container | Image | Networks | Published ports | Holds secrets |
|---|---|---|---|---|
| `ib-gateway` | `ghcr.io/gnzsnz/ib-gateway:stable` | `egress`, `backend` | VNC `127.0.0.1:5900` only, for maintenance through an SSH tunnel | IBKR username and password (Docker secret) |
| `db` | `postgres:17` | `backend` (internal) | none | DB passwords |
| `agent` | own image | `backend`, `egress` | none | API keys, Telegram token, DB URL |
| `dashboard` | own image (same image, different command) | `backend` (internal) | `127.0.0.1:8501`, exposed through `tailscale serve` | read-only DB URL |

The TWS API port (4004 for paper) is reachable only from inside the Compose network. It is never published, because the protocol is unauthenticated and unencrypted. The Python code never sees the IBKR password; only the gateway container logs in.

### 2.2 Memory budget (8 GB)

| Component | Estimate |
|---|---|
| Ubuntu (minimal desktop or server) | 1.0–1.5 GB |
| IB Gateway (JVM, default heap 768 MB) | 1.0–1.5 GB |
| PostgreSQL (`shared_buffers=256MB`) | 0.3–0.5 GB |
| agent (pandas, async clients) | 0.4–0.8 GB |
| dashboard (Streamlit) | 0.2–0.3 GB |
| **Total** | **≈ 3–4.5 GB**, leaving headroom; add a 4 GB swap file as a safety net |

---

## 3. Code architecture

### 3.1 Repository layout

```
trading-agent/
├── CONCEPT.md / IMPLEMENTATION.md
├── pyproject.toml / uv.lock
├── Dockerfile
├── compose.yaml                 # production-like stack (Zenbook, mini PC)
├── compose.dev.yaml             # Mac: db + agent with fake broker and test models
├── .env.example                 # documented keys, no values
├── Makefile                     # lint, test, deploy, backup, migrate
├── config/
│   ├── risk.yaml                # from CONCEPT.md 6.1
│   ├── universe.yaml            # generated by a script, refreshed monthly
│   ├── schedule.yaml            # jobs relative to session open/close
│   ├── models.yaml              # LLM role → model, budget
│   └── fees.yaml                # IBKR commission model per market
├── prompts/
│   └── <module>/v<N>.md         # front matter: version, role, output schema
├── migrations/                  # alembic
├── scripts/                     # build_universe.py, backfill.py, one-offs
├── src/trading_agent/
│   ├── __main__.py              # CLI (typer): run, backfill, scan, backtest, report, killswitch
│   ├── settings.py              # pydantic-settings, loads .env + config/*.yaml
│   ├── domain/                  # pure models, no I/O
│   ├── db/                      # SQLAlchemy models, repositories
│   ├── data/                    # providers + ingestion jobs
│   ├── calc/                    # indicators, levels, stats, fees, sizing
│   ├── strategies/              # rule-based baseline (candidate generator)
│   ├── modules/                 # analysis modules (data → calc → LLM → schema)
│   ├── llm/                     # model registry, budget guard, prompt loader, sanitizer, validators
│   ├── agents/                  # proposer, critic, portfolio manager
│   ├── risk/                    # risk engine, kill switch, state
│   ├── execution/               # BrokerAdapter, IBKR adapter, simulator, reconciliation, order FSM
│   ├── portfolio/               # ledger, P&L in EUR, tax report
│   ├── evaluation/              # books, shadow tracking, KPIs, weekly report
│   ├── notify/                  # telegram bot, message templates
│   ├── scheduler.py             # job wiring
│   └── dashboard/               # streamlit pages
└── tests/
    ├── unit/                    # calc, risk, sizing, fees, FSM, validators
    ├── contract/                # adapters against recorded responses
    ├── integration/             # against the IBKR paper gateway (marker: ibkr, manual)
    ├── evals/                   # LLM golden cases (marker: llm, weekly)
    └── fixtures/                # recorded bars, chains, LLM outputs
```

### 3.2 Dependency rules (enforced with `import-linter` in CI)

```mermaid
flowchart TB
    domain
    calc --> domain
    data --> domain
    strategies --> calc
    llm --> domain
    modules --> calc & llm & data
    agents --> modules & llm
    risk --> calc & domain
    execution --> domain
    portfolio --> domain
    evaluation --> calc & domain
    notify --> domain
    app[scheduler / __main__] --> agents & risk & execution & notify & evaluation & data
```

Contracts:
- `llm`, `modules` and `agents` **must not import** `execution` or `risk`. LLM code can only return data objects; it has no path to an order.
- `risk` must not import `llm`. The risk engine is deterministic.
- `execution` is called only from the orchestration layer (`scheduler`, `__main__`), and only with a `RiskApproved` object. That type can only be constructed by `risk.engine`.

### 3.3 Concurrency model

There is one asyncio event loop, shared by:
- `ib_async` (`await ib.connectAsync(...)`)
- `python-telegram-bot`, using the manual lifecycle (`await app.initialize(); await app.start(); await app.updater.start_polling()`) instead of `run_polling()`, so it doesn't take over the loop
- APScheduler's `AsyncIOScheduler`
- PydanticAI's async `agent.run(...)`
- SQLAlchemy async with `asyncpg`

CPU-heavy work, such as indicators over the whole universe, runs in a thread pool (`asyncio.to_thread`) so the loop stays responsive to broker events.

---

## 4. Domain model and database

### 4.1 Core domain types (`domain/`)

```python
class Instrument(BaseModel):
    id: int
    symbol: str
    exchange: str            # "SMART"/"NASDAQ" for US, "IBIS" (Xetra) for DE
    currency: Literal["USD", "EUR"]
    conid: int | None        # IBKR contract id, resolved once
    sector: str | None
    market: Literal["US", "EU"]

class LevelRef(BaseModel):
    name: str                # e.g. "sma50", "swing_low_1", "fib_618", "atr_stop_2x"
    price: Decimal

class TradeProposal(BaseModel):
    id: UUID
    instrument_id: int
    direction: Literal["long"]           # long-only in a cash account
    strategy: str
    entry_ref: str                       # names from the level menu, never raw numbers
    stop_ref: str
    target_ref: str
    confidence: float = Field(ge=0, le=1)
    thesis: str
    invalidation: str
    evidence: list[UUID]                 # analysis ids
    data_gaps: list[str]
    prompt_versions: dict[str, str]
    model_versions: dict[str, str]

class RiskDecision(BaseModel):
    proposal_id: UUID
    approved: bool
    checks: list[CheckResult]            # every check with value, limit, pass/fail
    quantity: Decimal | None
    entry: Decimal | None
    stop: Decimal | None
    target: Decimal | None
    risk_eur: Decimal | None
    est_fees_eur: Decimal | None
```

The key idea: **the LLM chooses among named levels; code turns names into prices.** Each module receives a *level menu* (moving averages, swing highs and lows, Fibonacci levels, ATR-based stops) that the calculators produce. Output fields are `Literal` types built dynamically from that menu, so the model cannot invent a price.

### 4.2 Tables

| Table | Purpose | Key columns |
|---|---|---|
| `instruments` | Universe | `symbol`, `yahoo_symbol` (unique), `name`, `exchange`, `currency`, `kind` (`stock`/`etf`), `market`, `sector`, `indices[]`, `conid`, `active` |
| `bars_daily` | OHLCV, split-adjusted | PK (`instrument_id`, `date`), `source`, `fetched_at` |
| `fx_daily` | EUR/USD for P&L and tax | PK (`date`, `quote`), `rate` (units per 1 EUR), `source` (ECB) |
| `fundamentals` | Point-in-time facts | `instrument_id`, `period_end`, `filed_at`, `metric`, `value` |
| `earnings_events` | Calendar and history | PK (`instrument_id`, `date`), `ts`, `timing` (`bmo`/`during`/`amc`/`unknown`), `eps_estimate`, `eps_actual` |
| `news_items` | Raw news, triaged | `published_at`, `source`, `url`, `title`, `symbols[]`, `triage_score` |
| `macro_series` | FRED/ECB series | `series_id`, `date`, `value` |
| `analyses` | Module outputs | `module`, `prompt_version`, `model`, `instrument_id`, `as_of`, `input_hash`, `output jsonb`, `cost_usd` |
| `proposals` | All proposals | `payload jsonb`, `status`, `source` (`baseline` / `agent`) |
| `risk_decisions` | Risk engine verdicts | `proposal_id`, `approved`, `checks jsonb`, `quantity`, `risk_eur` |
| `orders` | Broker orders | `client_ref` (unique), `broker_order_id`, `parent_ref`, `kind` (`entry`/`stop`/`target`), `status`, `mode` (`paper`/`live`/`sim`) |
| `fills` | Executions | `order_id`, `ts`, `qty`, `price`, `commission`, `currency`, `fx_rate_eur` |
| `trades` | Round trips | `book` (`baseline_sim`, `agent_paper`, `agent_shadow`, `agent_live`), entry/exit, `pnl_net_eur`, `r_multiple`, `holding_days` |
| `llm_calls` | Cost and audit | `role`, `model`, `tokens_in/out`, `cost_usd`, `latency_ms`, `analysis_id` |
| `user_labels` | Your agree/disagree labels | `proposal_id`, `label`, `reason` |
| `agent_state` | Runtime state | key/value: `mode`, `kill_switch`, `paused`, `llm_budget_used` |
| `audit_log` | Append-only | `ts`, `actor`, `event`, `payload jsonb` |

Two DB roles: `agent` (read/write) and `dashboard` (read-only, `SELECT` only).

---

## 5. Data layer

| Data | Phase 0–1 source | Phase 2+ source | Refresh |
|---|---|---|---|
| Universe lists (start: S&P 100, DAX 40; later: S&P 500, Nasdaq-100, MDAX) | `scripts/build_universe.py` (Wikipedia constituent tables, every symbol verified on Yahoo) → `config/universe.yaml`, committed | same | monthly, on the Mac |
| Benchmarks | SPY (US), EXS1.DE (iShares Core DAX UCITS ETF) in `universe.yaml` | same | with bars |
| Daily bars US | `yfinance` | IBKR `reqHistoricalData` (daily, incremental) | XNYS close + 30 min |
| Daily bars EU (`.DE`, Xetra) | `yfinance` | IBKR | XETR close + 30 min |
| Earnings calendar and surprises | `yfinance` (covers US and DAX; ~24 events per symbol on backfill) | same, or Finnhub if coverage degrades | daily 07:15 |
| US fundamentals | SEC EDGAR company facts (as filed; set `SEC_EDGAR_USER_AGENT`) | same | weekly |
| Macro | FRED (`FRED_API_KEY`, free) for US series; ECB Data Portal (no key) for the ECB deposit rate. Series list in `config/data.yaml`. | same | daily 07:00 |
| News | RSS feeds + Finnhub company news | same | every 30 min |
| EUR/USD | ECB reference rate (Data Portal, no key) | same | with the EU close job |

Rules:
- Every provider returns domain objects with `source` and `fetched_at` (`data/ingest.py` defines the provider protocols).
- Ingestion is **idempotent** (upserts that only touch rows whose values changed) and **incremental**: bars are fetched from the last stored session minus a 5-session overlap, so late corrections are picked up; a repeated backfill reports zero changes. IBKR's historical-data pacing limits are respected from M6.
- Bars are **split-adjusted but not dividend-adjusted**, i.e. actual traded prices, so levels and limit prices match the market. A bar is only stored once its session has closed.
- **Restatements**: if the overlap shows past closes changed by more than 0.5 % (a split), the instrument's full history is re-fetched and replaced.
- Earnings: future dates the source no longer reports (moved announcements) are deleted. Announcement times are classified as before open, during, after close, or unknown.
- Data quality checks run after each end-of-day ingestion (`trading-agent quality` on demand): missing sessions against the exchange calendar, bars on non-session dates, invalid OHLC, close-to-close jumps > 40 % (unadjusted splits), zero volume, stale data. Issues from the last 20 sessions **block** the symbol from that day's scan; zero volume only blocks on the latest bar, because Yahoo's Xetra data has sporadic zero-volume days with valid prices. Issues are logged, and sent to Telegram from M4.
- Backtests on current index members have **survivorship bias**. That is acceptable for a sanity check of the baseline, but not as proof of an edge.
- Instruments must allow at least 2 whole shares within the maximum position size, unless fractional orders via the API turn out to work (to verify in milestone M6).

---

## 6. Calculators and the rule-based baseline

### 6.1 Calculators (`calc/`)

Own implementations, pure functions over pandas Series (pyright strict), unit-tested against hand-checked reference values:

| Function | Notes |
|---|---|
| `indicators`: `sma`, `ema`, `rsi` (Wilder), `macd`, `atr` (Wilder), `bollinger` | EMAs and Wilder averages are seeded with the SMA of the first n values, as in charting tools; Bollinger uses the population standard deviation |
| `trend.trend_states(close)` | `up` / `down` / `sideways` / `unknown`: daily SMA 50/200, weekly SMA 10/40 (same spans) |
| `levels.swing_points(high, low, lookback)` + `levels.zones(...)` | Confirmed pivots (5 bars each side), clustered within 0.5 × ATR into support/resistance zones with touch counts |
| `levels.fib_levels(low, high, up_leg)` | 38.2 / 50 / 61.8 % retracements of the largest leg in the last 126 bars |
| `trend.relative_strength(close, benchmark)` | 63-bar return relative to SPY (US) or EXS1.DE (EU) |
| `trend.post_earnings_moves(frame, events)` | Gap and close move on the reaction day (event day for before-open, next day for after-close, both for unknown times) |
| `levels.level_menu(frame)` | Named `LevelRef` list, rounded to cents: `close`, `last_high`, `last_low`, `ema20`, `sma50`, `sma200`, `bb_lower`, `bb_upper`, `high_52w`, `low_52w`, `atr_stop_1_5x`/`2x`/`3x`, `support_1..3`, `resistance_1..3`, `swing_low_last`, `swing_high_last`, `fib_382`/`500`/`618`. Levels without enough history are left out. |
| `fees.order_fees(...)`, `fees.round_trip_fees(...)` | From `config/fees.yaml` (checked against IBKR's pricing page); Tiered or Fixed per market, minimums and caps, estimated third-party and US sell-side regulatory fees, rounded up to the cent |
| `sizing.position_size(...)` | Formula from section 9.2 in the instrument currency; returns the binding limit, or quantity 0 with a reason. Property tests (`hypothesis`) check that no result breaks a limit and that it is the largest quantity that fits. |

### 6.2 Baseline strategy: "pullback in an uptrend"

This is both the **candidate generator** for the LLM pipeline and the **rule-based baseline** it must beat (CONCEPT.md section 13).

| Rule | Definition |
|---|---|
| Universe filter | Price > $5 (or €5), 20-day average dollar volume > 20 M, close > SMA200, SMA50 > SMA200 |
| Setup | 3–10 day pullback; close within 0–3 % above EMA20 or SMA50; RSI(14) between 40 and 55 |
| Event filter | No earnings within the next 3 trading days |
| Entry | Limit at the prior close + 0.2 %, valid for 2 trading days |
| Stop | The lower of (last swing low − 0.5 × ATR14) and (entry − 2 × ATR14) |
| Target | Entry + 2 × risk |
| Management | Stop to breakeven at +1R; time stop after 15 trading days |
| Ranking | Relative strength over 3 months, descending |

The baseline book is simulated every day (`baseline_sim`) with the same fee model and risk engine, so the comparison is fair.

Implementation: rules in `strategies/pullback.py` (parameters in `config/strategies.yaml`), fill model in `execution/sim.py` (section 10.5), portfolio loop in `backtest.py`. Until the risk engine exists (M8), the loop applies sizing, max positions, sector cap, fee-to-risk, orders per day, settled cash (T+1 US, T+2 Xetra) and the cash reserve; correlation clusters and loss limits follow in M8. The forward `baseline_sim` book replays from `baseline_book.start` every evening (job `baseline_sim`, XNYS close + 60 min) and replaces its rows in `trades`. `trading-agent backtest` prints the report.

### 6.3 Backtest result and decision (M3, 2026-10-03)

5 years (Oct 2021 to Oct 2026), EUR 1,000, US + EU, all rules above, fees and 0.05 % slippage per side ([reports/backtest-baseline-2026-10-03.md](reports/backtest-baseline-2026-10-03.md)):

| | Trades | Win rate | Avg R | Profit factor | Return | Max drawdown |
|---|---|---|---|---|---|---|
| Baseline as specified, EUR 1,000 | 266 | 40 % | −0.03 | 0.94 | −7.7 % | −23.8 % |
| Same, without fees | 284 | 39 % | −0.05 | 0.92 | −10.7 % | −26.4 % |
| EUR 5,000, with fees | 254 | 41 % | −0.06 | 0.86 | −20.0 % | −31.0 % |
| EUR 50,000, no fees (no size frictions) | 347 | 43 % | +0.04 | 1.10 | +19.3 % | −16.0 % |
| ... same, no breakeven stop | 349 | 47 % | +0.07 | 1.21 | +39.7 % | −16.5 % |
| ... same, 30-session time stop / 3R target / 1.5R target | 256 / 310 / 343 | | +0.02 / +0.01 / +0.02 | | | |
| Buy and hold SPY / EXS1.DE (EUR) | | | | | +86.1 % / +61.2 % | −23.0 % / −26.7 % |

Findings:
- **No measurable edge.** Even without fees and size limits the average is +0.04 R per trade; with ~350 trades the standard error is about 0.05 R, so that is indistinguishable from zero. Costs at EUR 1,000 (fees ≈ 0.05 R per trade, plus slippage) turn it negative. Buy and hold beat it by a wide margin in this bull market.
- **Exits:** the 2R target is reached in only 12 % of trades; most end at the 15-session time stop (+0.35 R on average) or the stop (−0.86 R). Dropping the breakeven stop scored best, but within the noise of one sample with survivorship bias, so it is not adopted.
- **EUR 1,000 frictions:** of 7,993 setups, 3,230 can't be sized (one share exceeds the EUR 300 position cap or the EUR 15 risk cap; 32 of 101 US stocks trade above $330) and 2,417 fall below the EUR 200 minimum. Every EU setup that reached the fee check failed the 10 % fee-to-risk rule, so **EU produces no trades at EUR 1,000**.
- **Sample size:** about 53 trades per year at EUR 1,000. The go-live gate (50 closed trades in 3 months) depends on the shadow book.

**Decision: keep the baseline unchanged (v1) as the comparison bar, not as a strategy to trade on its own.** No parameters are tuned on this data. The LLM pipeline has to produce the edge; the baseline sets the floor it must clear after costs. Open points are in section 19.

---

## 7. Analysis modules and LLM layer

### 7.1 Module contract

```python
class AnalysisModule(Protocol[TIn, TOut]):
    name: str
    prompt: PromptRef                          # e.g. prompts/technical/v1.md
    output_type: type[TOut]                    # Pydantic model

    async def gather(self, instrument: Instrument, as_of: date) -> TIn: ...
    def compute(self, raw: TIn) -> ModuleInput: ...          # calculators only
    async def synthesise(self, inp: ModuleInput) -> TOut: ... # one PydanticAI run
    def validate(self, inp: ModuleInput, out: TOut) -> list[ValidationIssue]: ...
```

Order of work: the `technical` and `earnings` modules first, then `macro` (weekly regime) and `risk_report`, then `patterns` and `news`. The long-term prompts (DCF, dividend, sector) are not needed for swing trading at first.

### 7.2 Example: technical module output

```python
def technical_output_type(menu: list[LevelRef]) -> type[BaseModel]:
    Names = Literal[tuple(l.name for l in menu)]          # built per call
    class TechnicalAssessment(BaseModel):
        trend_summary: str
        setup_quality: float = Field(ge=0, le=1)
        rating: Literal["strong_buy", "buy", "neutral", "avoid"]
        entry_ref: Names
        stop_ref: Names
        target_ref: Names
        reasoning: str
        data_gaps: list[str]
    return TechnicalAssessment
```

```python
agent = Agent(
    models.for_role("analysis"),             # e.g. "google-gla:gemini-3.8-flash"
    output_type=technical_output_type(menu),
    instructions=load_prompt("technical", version=1),
    retries=2,
)
result = await agent.run(render_input(inp))
budget.record(result.usage(), role="analysis", module="technical")
```

### 7.3 Validators (deterministic, after every LLM call)

- `stop < entry < target` for longs, and the resulting R:R is at least the configured minimum.
- The stop distance is between 1 and 4 × ATR14.
- Every free-text number (in `thesis` and `reasoning`) must appear in the input within ±0.5 %; otherwise the issue is logged as possible fabrication and the confidence is capped.
- `data_gaps` must not be empty when the input contains `unknown` fields.
- On failure: one retry with the issues fed back, then the proposal is dropped and logged.

### 7.4 Prompts

- Files in `prompts/<module>/v<N>.md` with front matter (`version`, `role`, `output`). They are derived from the screenshot prompts, with the persona removed and the checklist kept.
- Every `analyses` row stores the prompt version and the model, so changes can be evaluated.
- Untrusted content (news, filings) is wrapped in `<untrusted source=... >` blocks, stripped of markup and links, and truncated. The system prompt says that content inside these blocks is data, never instructions.

### 7.5 Model registry and budget guard (`config/models.yaml`)

```yaml
roles:
  triage:    google-gla:gemini-3.1-flash-lite     # verify provider prefix and model ids at setup
  analysis:  google-gla:gemini-3.8-flash
  proposer:  google-gla:gemini-3.8-flash
  critic:    anthropic:claude-sonnet-5-5
  reports:   google-gla:gemini-3.8-flash
dev_overrides:                                     # Phase 0–1, free tier
  analysis:  google-gla:gemini-3.8-flash
budget:
  monthly_usd: 16                                  # ≈ €15
  lean_mode_at: 0.8
  hard_stop_at: 1.0
batch:
  use_for: [analysis_eu_overnight]
```

- `budget.check(role)` runs before each call: above 80 % it switches to lean mode (fewer candidates, no critic for low-confidence proposals); at 100 % no more LLM calls are made and the baseline keeps running alone.
- **Cache**: `input_hash = sha256(module, prompt_version, model, as_of, canonical_input)`. On a hit, no LLM call is made.
- Prices per model live in `models.yaml` and are compared monthly against the real invoice.

### 7.6 Testing LLM code

- Unit tests use PydanticAI's `TestModel` / `FunctionModel`, so no API calls and no keys are needed in CI.
- `tests/evals/`: about 30 golden cases (recorded inputs from real days) with expected validator results and plausibility checks. They run weekly with the real models and cost a few cents.

---

## 8. Agents

```mermaid
sequenceDiagram
    participant B as Baseline (candidates)
    participant M as Modules
    participant P as Proposer
    participant C as Critic
    participant PM as Portfolio manager
    participant R as Risk engine
    B->>M: top N candidates (N=10 standard, 5 lean)
    M->>P: technical + earnings (+ macro regime, news)
    P->>C: draft TradeProposal (refs, thesis, invalidation)
    C->>P: CriticVerdict {objections, severity, confidence_delta}
    P->>PM: proposals with severity < "blocking"
    PM->>R: ranked list (fit with positions, sector, regime, cash)
```

| Agent | Input | Output | Model role |
|---|---|---|---|
| Proposer | Module outputs, level menu, open positions | `TradeProposal` or `NoTrade(reason)` | `proposer` |
| Critic | Proposal + the same inputs | `CriticVerdict` (objections, `severity`: `none`/`minor`/`major`/`blocking`, `confidence_delta`) | `critic` (other vendor) |
| Portfolio manager | Surviving proposals, portfolio, regime | Ranked list plus a short rationale | `proposer` |

The agents have **no tools** except read-only data lookups (`get_bars`, `get_levels`, `get_news`) defined in `agents/tools.py`. Every prompt, response, tool call and cost is written to `analyses`, `llm_calls` and `audit_log`.

---

## 9. Risk engine

### 9.1 Contract

```python
def evaluate(
    proposal: TradeProposal,
    levels: dict[str, Decimal],
    portfolio: PortfolioState,          # positions, settled cash, open orders, P&L windows
    market: MarketSnapshot,             # last price, ATR, earnings date, liquidity
    limits: RiskLimits,                 # from risk.yaml
    now: datetime,
) -> RiskDecision: ...
```

It is a pure function with no I/O and no LLM. Property-based tests (`hypothesis`) assert that no approved decision can ever break a limit.

### 9.2 Check order

1. **Global state**: kill switch off, not paused, live-mode interlock satisfied, inside the trading window (not in the first 15 or last 10 minutes of the session).
2. **Instrument**: market allowed in the current mode (live: US only), in the universe, not blacklisted, price and liquidity filters met, no earnings within 3 days.
3. **Levels**: refs resolve, `stop < entry < target`, R:R ≥ 2.0, stop distance within the ATR bounds, entry limit within 1 % of the current mid.
4. **Sizing**:

$$
q = \left\lfloor \min\left(\frac{0.015 \cdot B}{E - S},\ \frac{0.30 \cdot B}{E},\ \frac{C_{settled} - R_{cash}}{E}\right) \right\rfloor
$$

   where $B$ is the agent budget (€1,000, converted to the instrument currency), $E$ the entry price, $S$ the stop, $C_{settled}$ the settled cash, and $R_{cash}$ the 10 % cash reserve. The position value must be at least €200.
5. **Fees**: estimated round-trip fees ≤ 10 % of $q \cdot (E - S)$.
6. **Portfolio**: at most 4 open positions (including pending entries), sector ≤ 60 %, correlation cluster (ρ > 0.7 over 60 days) ≤ 60 %.
7. **Loss limits**: daily −3 %, weekly −6 %, drawdown −15 %. A breach blocks new entries; the drawdown limit also trips the kill switch.
8. **Rate limits**: at most 6 orders per day, and no duplicate proposal for an instrument that already has an open position or order.

Every check result is stored, and rejections show up in the evening digest.

### 9.3 Kill switch states

```mermaid
stateDiagram-v2
    [*] --> Active
    Active --> Paused: /pause or loss limit (daily/weekly)
    Paused --> Active: /resume or next day/week
    Active --> Halted: /stop (confirmed) or drawdown limit or reconciliation mismatch
    Paused --> Halted: /stop (confirmed)
    Halted --> Active: manual reset via CLI on the host + Telegram confirmation
    note right of Halted
        cancel all entry orders
        keep protective stops
        no new entries
    end note
```

---

## 10. Execution

### 10.1 IBKR adapter (`execution/ibkr.py`)

- A single `ib_async` connection with a fixed `clientId`. It reconnects with exponential backoff; every reconnect triggers reconciliation.
- Contracts are resolved once with `qualifyContractsAsync` and the `conid` is stored in `instruments`.
- **Bracket orders**: parent `LMT` with `tif=GTD` (2 trading days); children `STP` (stop) and `LMT` (target), both `GTC` and linked as OCA. `outsideRth=False`. Routing is `SMART`, which is required for Tiered pricing via the API.
- **Idempotency**: `orderRef = <proposal_id>:<kind>`. Before submitting, the adapter checks open orders and executions for that `orderRef`, so a retry never creates a duplicate order.
- **Stop management**: moving a stop (breakeven at +1R, trailing) modifies the existing stop order and is logged. Stops are only ever tightened, never loosened.
- **Market data**: daily bars from IBKR. Whether the free US real-time feed (Cboe One/IEX) also reaches the API is checked in milestone M6. If it doesn't, delayed quotes are acceptable for daily-bar entries with limit orders, and the 1 % limit-deviation check uses the last close plus the delayed quote.

### 10.2 Order state machine

```mermaid
stateDiagram-v2
    [*] --> Approved: RiskDecision.approved
    Approved --> Submitted: placeOrder (bracket)
    Submitted --> Working: broker ack
    Working --> Filled: entry filled
    Working --> Expired: GTD reached
    Working --> Cancelled: kill switch / invalidation
    Filled --> Exiting: stop or target or time stop or thesis invalidated
    Exiting --> Closed: exit filled
    Closed --> [*]
    Expired --> [*]
    Cancelled --> [*]
```

### 10.3 Reconciliation

It runs on startup, after every reconnect, every 10 minutes during sessions, and at end of day:
1. Pull positions, open orders and executions from IBKR.
2. Compare them with the `orders`, `fills` and `trades` tables.
3. Any position without a protective stop gets a stop placed immediately at the recorded stop level, and you get an alert.
4. Any unexplained difference (unknown position, missing order) moves the agent to `Halted` with a Telegram alert.

### 10.4 Paper/live interlock

Live mode requires all three:
- `APP_MODE=live` in `.env`
- `TRADING_MODE=live` for the gateway
- a confirmation via Telegram after startup (`/confirm_live <code>`, where the code is printed in the host logs)

Without all three, the agent refuses to place orders and alerts you.

### 10.5 Simulator (`execution/sim.py`)

Used for `baseline_sim` and `agent_shadow`, and for tests:
- Entry fills at the limit if the next bar's low is at or below the limit (or at the open if it gaps below).
- Stops fill at the stop or at the open on a gap. Targets fill symmetrically.
- Slippage: 0.05 % per side. Fees come from `fees.yaml`.
- IBKR paper fills are also optimistic, so the same slippage is added when evaluating `agent_paper`.

---

## 11. Scheduler

Jobs are defined relative to exchange sessions (`exchange_calendars`: `XNYS` for the US, `XETR` for Xetra), so holidays, half-days and the daylight-saving mismatch weeks are handled automatically. All times are Europe/Berlin.

| Job | Trigger | LLM | Notes |
|---|---|---|---|
| `heartbeat` | every 5 min | no | Ping the heartbeat URL |
| `ingest_macro`, `earnings_calendar` | 07:00 / 07:15 trading days | no | |
| `ingest_news` | every 30 min, 07:00–22:30 | triage | Flash-Lite scoring |
| `scan_eu` (paper only) | XETR open − 45 min | yes | Uses the previous EOD bars; batch API where possible |
| `briefing` | 08:30 | reports | Telegram: portfolio, events today, pending orders |
| `place_eu` | XETR open + 15 min | no | Risk engine → execution |
| `ingest_eod_eu` | XETR close + 30 min | no | |
| `scan_us` | XNYS open − 45 min | yes | |
| `place_us` | XNYS open + 15 min | no | |
| `monitor` | every 10 min in session | no | Stops, time stops, invalidation rules |
| `reevaluate_positions` | XNYS close − 60 min | yes | Only positions with news or events |
| `ingest_eod_us` | XNYS close + 30 min | no | |
| `eod` | after `ingest_eod_us` | no | Reconcile, update books and shadow trades, plan stop changes |
| `evening_digest` | after `eod` | reports | Telegram: fills, rejections, labels to do |
| `weekly_report` | Saturday 10:00 | reports | KPIs vs baselines, costs |
| `backup` | daily 03:00 | no | `pg_dump` |

Misfire policy: `coalesce=True` and a `misfire_grace_time` per job (for example 30 min for scans, 0 for order placement, so a late order placement is skipped).

---

## 12. Telegram bot

### 12.1 Setup

1. Create the bot with `@BotFather` and store the token in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Send the bot a message and read your chat ID from the agent log (an `/id` handler prints it), then store it as `TELEGRAM_OWNER_CHAT_ID`.
3. Every handler is wrapped in a filter: updates from any other chat are ignored and logged.

### 12.2 Commands

| Command | Effect |
|---|---|
| `/status` | Mode, kill-switch state, gateway connection, last heartbeat, budget used |
| `/positions` | Open positions with entry, stop, target, R multiple |
| `/pnl` | Day, week, month and since-start figures, plus the baseline for comparison |
| `/proposals` | Today's proposals and their status |
| `/why <SYMBOL>` | Thesis, critic objections and risk checks for the latest proposal |
| `/review` | Steps through today's proposals with **Agree / Disagree** buttons; a reply adds the reason (written to `user_labels`) |
| `/pause`, `/resume` | Block or allow new entries |
| `/stop` | Kill switch, after an inline confirm button |
| `/budget` | LLM spend this month and the current mode |
| `/help` | Command list |

### 12.3 Alerts

🟢 fill · 🔴 stop or exit · ⚠️ limit breach, reconciliation issue, data quality · 🔌 gateway disconnected for more than 10 minutes · 🔐 IBKR re-login with 2FA needed · 💸 LLM budget at 80 % · 📰 morning briefing · 🌙 evening digest.

### 12.4 Privacy

Bot chats are not end-to-end encrypted. Messages therefore never contain account numbers, credentials or personal data. Amounts are shown both in euros and as a percentage of the agent budget, for example `−€15.20 (−1.5 %)`. The setting `notify.amounts: both | percent | eur` (default `both`) can change that later.

---

## 13. Dashboard (Streamlit)

Pages: **Overview** (equity curves of the three books plus a benchmark, drawdown) · **Positions** · **Proposals** (filterable, with risk checks and critic notes) · **Journal** (full trace per trade) · **Risk** (limit usage, correlation heatmap) · **Costs** (LLM spend per role and model, fees) · **Evaluation** (KPIs, calibration plot).

It uses the read-only DB role and has no write actions; control happens through Telegram or the CLI. It is published to your tailnet with `tailscale serve`, so it's reachable from your phone and the Mac but not from the internet.

---

## 14. Evaluation

| Book | How it trades | Purpose |
|---|---|---|
| `baseline_sim` | Rule-based strategy, simulator | The bar the LLM must clear |
| `agent_paper` | LLM pipeline + risk engine, IBKR paper account | The real execution path |
| `agent_shadow` | Approved but unexecuted agent proposals, simulator | Larger sample despite only 4 slots |
| Benchmark | Buy and hold SPY (US) and DAX (EU), simulated | Context |

KPIs per book, weekly and cumulative: number of trades, win rate, average R, expectancy, profit factor, maximum drawdown, Sharpe/Sortino on daily equity, fees, LLM costs, and **net expectancy after LLM costs**. For the agent: calibration (confidence buckets against hit rate) and your agree/disagree accuracy.

Go-live gate (CONCEPT.md section 15, Phase 2): at least 3 months and 50 closed trades (including shadow), positive net expectancy after costs, and `agent_paper` + `agent_shadow` beating `baseline_sim`.

---

## 15. Deployment

### 15.1 Zenbook preparation (once)

1. Reinstall the current Ubuntu Server LTS (minimal), with automatic login disabled and full-disk encryption if you prefer it (the unlock prompt after power loss then needs a person at the machine). The laptop stays plugged in permanently.
2. Never sleep:
   - `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`
   - In `/etc/systemd/logind.conf`, set `HandleLidSwitch=ignore`, `HandleLidSwitchExternalPower=ignore` and `HandleLidSwitchDocked=ignore`, then restart `systemd-logind`.
3. Time: `timedatectl set-timezone Europe/Berlin` and confirm NTP is active.
4. Docker Engine and the Compose plugin from Docker's official apt repository; `systemctl enable docker`; log rotation in `/etc/docker/daemon.json` (`json-file`, `max-size: 10m`, `max-file: 5`).
5. A 4 GB swap file.
6. Tailscale for SSH and the dashboard; `ufw` denies all incoming traffic except on the Tailscale interface.
7. `unattended-upgrades` for security updates, with automatic reboot restricted to Sunday 04:00.
8. The battery works as a small UPS, if it still holds a charge. After a power loss, Docker's `restart: unless-stopped` brings the stack back, and reconciliation repairs any missing stops.

### 15.2 Secrets

- `.env` and `secrets/` exist **only on the Zenbook** (later the mini PC), owned by the deploy user. `.env` is `chmod 600`. `secrets/` is `chmod 700` and its files `644`: Compose bind-mounts each file into the container, where the non-root container users (postgres uid 999, agent uid 10001) must be able to read it, while the directory keeps other host users out. `make secrets` creates missing password files with random values.
- Passwords are never in `.env` or a connection URL: each container reads its own file from `/run/secrets/` (pydantic-settings `secrets_dir`). The agent gets `agent_db_password` mounted as `db_password`; the dashboard will get `dashboard_db_password` mounted under the same name, so both use the same settings code.
- `.env.example` in the repo lists every key with a comment and no value. `.gitignore` covers `.env*` (except `.env.example`), `secrets/` and `*.dump`.
- Each API key uses prepaid credit and a hard monthly cap on the provider side.

| Secret file | Used by | Created |
|---|---|---|
| `postgres_password` | `db` (superuser, maintenance only) | M0 |
| `agent_db_password` | `db` init script, `agent` | M0 |
| `dashboard_db_password` | `db` init script, `dashboard` | M6 |
| `tws_password`, `vnc_password` | `ib-gateway` | M6 |

```dotenv
# .env.example (abridged; the full file is in the repo)
APP_MODE=paper                      # paper | live
LOG_LEVEL=INFO
HEARTBEAT_URL=
DB_HOST=db                          # DB_PORT, DB_NAME, DB_USER have working defaults
IB_HOST=ib-gateway
IB_PORT=4004                        # 4004 paper, 4003 live (gnzsnz socat ports)
IB_CLIENT_ID=11
TWS_USERID=
TELEGRAM_BOT_TOKEN=
TELEGRAM_OWNER_CHAT_ID=
GEMINI_API_KEY=                     # check the exact variable name in PydanticAI's Google provider docs
ANTHROPIC_API_KEY=
FINNHUB_API_KEY=
FRED_API_KEY=
SEC_EDGAR_USER_AGENT=               # "Name email", required by SEC
```

### 15.3 Compose (target state after M6; `compose.yaml` in the repo has `db` and `agent` so far)

```yaml
services:
  ib-gateway:
    image: ghcr.io/gnzsnz/ib-gateway:stable
    restart: unless-stopped
    environment:
      TWS_USERID: ${TWS_USERID}
      TWS_PASSWORD_FILE: /run/secrets/tws_password
      TRADING_MODE: paper
      READ_ONLY_API: "yes"            # "no" from milestone M8
      TIME_ZONE: Europe/Berlin
      AUTO_RESTART_TIME: "11:45 PM"
      TWOFA_TIMEOUT_ACTION: restart
      RELOGIN_AFTER_TWOFA_TIMEOUT: "yes"
      VNC_SERVER_PASSWORD_FILE: /run/secrets/vnc_password
    secrets: [tws_password, vnc_password]
    ports: ["127.0.0.1:5900:5900"]
    networks: [backend, egress]

  db:
    image: postgres:17
    restart: unless-stopped
    environment:
      POSTGRES_DB: trading
      POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password
    secrets: [postgres_password, agent_db_password, dashboard_db_password]
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./docker/db/init:/docker-entrypoint-initdb.d:ro   # creates the agent / dashboard roles
    networks: [backend]

  agent:
    build: .
    command: ["python", "-m", "trading_agent", "run"]
    restart: unless-stopped
    env_file: .env
    secrets: [{ source: agent_db_password, target: db_password }]
    depends_on: [db, ib-gateway]
    read_only: true
    networks: [backend, egress]

  dashboard:
    build: .
    command: ["streamlit", "run", "src/trading_agent/dashboard/app.py", "--server.address=0.0.0.0"]
    restart: unless-stopped
    environment:
      DB_USER: dashboard
    secrets: [{ source: dashboard_db_password, target: db_password }]
    ports: ["127.0.0.1:8501:8501"]
    networks: [backend, frontend]   # a container only on an internal network can't publish ports

networks:
  backend: { internal: true }
  egress: {}
  frontend: {}

volumes: { pgdata: {} }

secrets:
  tws_password:          { file: ./secrets/tws_password }
  vnc_password:          { file: ./secrets/vnc_password }
  postgres_password:     { file: ./secrets/postgres_password }
  agent_db_password:     { file: ./secrets/agent_db_password }
  dashboard_db_password: { file: ./secrets/dashboard_db_password }
```

### 15.4 Deploy and backup

- `make deploy` on the Mac runs `ssh zenbook 'cd ~/trading-agent && git pull --ff-only && docker compose build && docker compose run --rm agent alembic upgrade head && docker compose up -d'`.
- Deploys run outside trading sessions, or the agent is paused first (`/pause`), because a restart triggers reconciliation.
- Backup: nightly `pg_dump -Fc`, keeping 14 days on disk. Once a week a copy is encrypted (`age`) and moved off the machine, for example to a cloud drive.
- Moving to the mini PC: the same preparation as 15.1, copy `.env` and `secrets/`, restore the latest dump, `docker compose up -d`, then confirm reconciliation is clean before shutting down the Zenbook stack.

---

## 16. Development workflow and quality

### 16.1 Working mode

- Copilot implements one milestone (section 17) at a time, split into small, reviewable steps. Each step comes with tests and a short summary of what changed and why.
- You review each step before it's committed, and decide on any open architecture questions. Copilot asks instead of guessing when a decision changes the design or the risk rules.
- Changes to `risk/`, `execution/` and `config/risk.yaml` always get your explicit sign-off. They never ride along with unrelated changes.
- Copilot works only on the Mac: no secrets, no broker access, no real API keys. Anything that needs the Zenbook (gateway, real LLM evals, deploys) is handed to you as exact commands to run, and you report the output back.
- Pushes to GitHub and deploys are done by you, or by Copilot only after you confirm.

### 16.2 Tooling and checks

- **On the Mac**: `uv sync`, `make lint`, `make test` (unit and contract tests with recorded fixtures), and `make test-db` (DB tests against a throwaway Postgres container). Later `compose.dev.yaml` adds the simulator broker and PydanticAI's `TestModel`. No keys and no broker access are needed.
- **On the Zenbook**: integration tests against the IBKR paper gateway (`pytest -m ibkr`), and evals against the real models (`pytest -m llm`).
- **CI (GitHub Actions)**: `ruff check`, `ruff format --check`, `pyright`, `lint-imports`, `pytest -m "not ibkr and not llm"` (DB tests run against a Postgres service container) and a `docker build`. No secrets in CI. DB tests only run when `DB_NAME` ends in `_test`, because they drop tables.
- **pre-commit**: ruff, end-of-file fixers, and a secret scanner (for example `gitleaks`).
- Dependabot for dependency updates, with lockfile changes reviewed before deploying.
- Every risk-engine or execution change needs a test, and those two packages target 100 % branch coverage.

---

## 17. Build plan

Milestones are ordered by dependency. Each one ends with something that runs.

```mermaid
flowchart LR
    M0[M0 Bootstrap] --> M1[M1 Data]
    M0 --> M4[M4 Telegram]
    M1 --> M2[M2 Calculators]
    M2 --> M3[M3 Baseline + sim]
    M2 --> M5[M5 LLM modules]
    M4 --> M5
    M1 --> M6[M6 IBKR read-only + dashboard]
    M3 --> M7[M7 Agents + journal]
    M5 --> M7
    M6 --> M7
    M7 --> M8[M8 Risk + execution on paper]
    M8 --> M9[M9 Paper operations]
    M9 --> M10[M10 Mini PC + go-live]
```

| # | Milestone | Deliverables | Done when |
|---|---|---|---|
| M0 | Bootstrap | `pyproject`, `uv.lock`, Dockerfile, `compose.yaml` with `db` and `agent`, settings loader, Alembic baseline, CI, pre-commit, `.gitignore`, `.env.example`, Zenbook prepared (15.1) | CI is green; on the Zenbook, `docker compose up` runs and a heartbeat job pings |
| M1 | Data foundation | Universe script, schema for instruments and bars, `yfinance` backfill (5 years), FRED, ECB FX, earnings calendar, quality checks | A repeated backfill makes no changes; quality report is clean; incremental update works after close |
| M2 | Calculators | Indicators, swing levels, Fibonacci, level menu, fees, sizing | Unit tests against reference values; property tests for sizing |
| M3 | Baseline and simulator | Pullback strategy, simulator, `baseline_sim` book, backtest CLI with report | A 5-year backtest report including fees; a reasoned decision whether the baseline is worth keeping or needs adjusting |
| M4 | Telegram | Bot with owner filter, `/status`, `/help`, alerts, data-only morning briefing | Briefing arrives on trading days; messages from other chats are ignored |
| M5 | LLM modules | PydanticAI setup, `models.yaml`, budget guard, cache, prompt loader, sanitizer, validators, `technical` and `earnings` modules, eval set | Evals pass; cost per scan is logged; no validator failures on the golden cases |
| M6 | IBKR read-only | Gateway container (paper, `READ_ONLY_API=yes`), adapter for account, positions and bars, contract resolution, reconciliation (read side), US bars switched to IBKR, dashboard v1 | Gateway survives the daily restart; re-login alert works; free US real-time data via the API is checked and documented |
| M7 | Agents and journal | Proposer, critic (Anthropic key added), portfolio manager, proposals in the DB, shadow book, `/review` labels, evening digest | One week of daily proposals without orders, labelled by you |
| M8 | Risk and execution | Full risk engine, bracket orders, FSM, idempotency, stop management, kill switch, interlock; `READ_ONLY_API=no` | Chaos tests pass: restarting the gateway with open orders, rebooting the laptop during a session, and replaying the same proposal all leave no duplicates and no unprotected positions |
| M9 | Paper operations | Daily routine (CONCEPT.md 13.1), weekly report, prompt and rule iterations (versioned) | The go-live gate in section 14 is met |
| M10 | Mini PC and go-live | Migration (15.4), tax report (EUR conversion, Anlage KAP categories), go-live checklist | Checklist in section 18 complete |

---

## 18. Go-live checklist (Phase 3)

- [ ] Go-live gate met and the weekly reports archived
- [ ] Mini PC running for at least 2 weeks without incident
- [ ] IBKR live account funded with €1,000; EUR→USD converted in one block; W-8BEN filed
- [ ] `risk.yaml` live values reviewed (US only, lean mode on)
- [ ] Live interlock tested: refusal without all three conditions
- [ ] Kill switch tested live with a 1-share order
- [ ] Backup restore tested on another machine
- [ ] Monthly caps set at Google and Anthropic
- [ ] You know the manual fallback: close everything in the IBKR mobile app

---

## 19. Answers to the open questions (2026-10-03)

| # | Question | Answer | Consequence |
|---|---|---|---|
| 1 | Ubuntu on the Zenbook | Reinstall an LTS; keep it plugged in | Fresh Ubuntu Server LTS (15.1). Battery health is irrelevant as long as it stays plugged in. |
| 2 | Universe size | Start smaller | S&P 100 + DAX 40 (about 140 symbols); larger indices later (section 5) |
| 3 | Telegram amounts | Both | `−€15.20 (−1.5 %)` format (12.4) |
| 4 | IBKR account | Application started, not ready yet | M0–M5 don't need IBKR (`yfinance` + simulator). M6 starts once the paper login exists. |
| 5 | LLM tracing (Logfire) | Undecided | **Recommendation adopted: local only.** Prompts contain positions and theses, and `llm_calls` + `audit_log` already cover cost and audit. Hosted Logfire can be reconsidered in M9 if debugging prompts gets hard. |
| 6 | Dashboard | Streamlit is fine | As planned (section 13) |

### 19.1 Open questions from the M3 backtest

1. **EU in paper:** at EUR 1,000 every Xetra trade fails the 10 % fee-to-risk rule, so the paper book will contain no EU trades. Options: (a) accept it, since live starts US-only anyway; (b) give EU paper trading its own notional budget of EUR 5,000, where fees fit (69 EU trades in the backtest); (c) relax the fee rule for EU in paper only. Recommendation: (b), because it adds sample size and tests the setup that becomes relevant at EUR 5,000, without loosening any live rule.
(b)
2. **Expensive US stocks:** 32 of 101 S&P 100 stocks trade above $330 and can't be bought within the EUR 300 position cap. Fractional shares via the API (checked in M6) would fix this; until then they stay unbuyable. OK?
ok
