# LLM Observability

Every LLM call is logged with its full conversation, and every scan can be followed step by step as a trace.

## What it does

- **Every call is stored for good** in Postgres (`llm_calls`): one row per attempt with the full messages (instructions, prompt, every model answer including rejected ones, retry prompts), tokens, cost, latency, error and the trace ID. Failed runs and rate-limit retries are included. The record is written even when tracing is off.
- **Every run is one trace** in Langfuse: a scheduled scan (`scan_eu`, `scan_us`) or a CLI run (`analyse`, `propose`) shows each analysis per stock (`technical AAPL`, `proposer AAPL`, …), each attempt (`invoke_agent`) and each model request (`chat …`), with input, output, tokens and timings. News triage and position reviews appear as their own small traces. Cache hits link to the trace that produced the answer.
- **Grouping:** environment = `paper` / `live` (`dev`, `evals` for tests), session = trading day (Berlin).
- **Logs:** every log line written during a run carries its `trace_id`.
- **Tracing never blocks trading.** Spans are sent in the background. If Langfuse is down, they're dropped and the database record is still complete.

## Where to find it

| What | Where |
|---|---|
| Traces | [cloud.langfuse.com](https://cloud.langfuse.com) (EU region) → project `trading-agent` → **Tracing → Traces**. Filter by environment `paper`; **Sessions** shows one trading day. History: 30 days (free Hobby plan). |
| One analysis with all its LLM calls | Dashboard → **Analyses** → Details → *LLM calls* (each attempt's prompts and answers, kept indefinitely) and **Open trace** |
| The trace behind a proposal | Dashboard → **Proposals** → Details → **Open trace**; Telegram `/why SYMBOL` (last line `Trace: …`) |
| Is tracing on? | Telegram `/status` → `LLM tracing: on (cloud.langfuse.com, paper)`; the `agent.started` log line |
| The log lines of one run | `docker compose logs agent --since 24h \| grep <trace_id>` |

## How it's implemented

Standard OpenTelemetry (OTLP/HTTP, GenAI semantic conventions). PydanticAI emits the agent and model spans; we add one span per analysis and one root span per run. Langfuse is just the backend: another OTLP backend means a different endpoint, plus the attribute names in `llm/tracing.py`.

| Part | Where |
|---|---|
| Start with the app | `trading-agent run` (the agent container) and the CLI call `telemetry.configure()` at start-up and flush on exit. Tracing is **on whenever `secrets/trace_headers` holds the Langfuse keys**. `TRACE_ENDPOINT` defaults to Langfuse Cloud EU. |
| SDK setup, root spans, environment/session | [src/trading_agent/telemetry.py](src/trading_agent/telemetry.py) |
| Analysis spans, PydanticAI instrumentation, message capture | [src/trading_agent/llm/runner.py](src/trading_agent/llm/runner.py), [src/trading_agent/llm/tracing.py](src/trading_agent/llm/tracing.py) (Langfuse attribute names) |
| Call log | `llm_calls` (+ `analyses.trace_id`), migration [0011](migrations/versions/0011_llm_traces.py) |
| Root spans | [scheduler.py](src/trading_agent/scheduler.py) (`scan_eu`, `scan_us`), [\_\_main\_\_.py](src/trading_agent/__main__.py) (`analyse`, `propose`), [tests/evals](tests/evals/test_evals.py) |
| Dashboard, `/why`, `/status`, logs | [dashboard/pages.py](src/trading_agent/dashboard/pages.py), [journal.py](src/trading_agent/journal.py), [notify/messages.py](src/trading_agent/notify/messages.py), [log.py](src/trading_agent/log.py) |
| Deployment | `compose.yaml` (secret `trace_headers` for the agent, `TRACE_UI_URL` for the dashboard), `make trace-headers`, `.env.example` |
| Tests | [tests/unit/llm/test_tracing.py](tests/unit/llm/test_tracing.py) (span tree, retries, failures, no API key in spans), [tests/unit/test_telemetry.py](tests/unit/test_telemetry.py) |

Settings:

| Key | Default | Meaning |
|---|---|---|
| secret `trace_headers` | empty | Langfuse keys as an OTLP header (`make trace-headers`). Empty = tracing off. |
| `TRACE_ENDPOINT` | Langfuse Cloud EU | Empty switches tracing off |
| `TRACE_CONTENT` | `true` | `false`: spans without prompts and answers (the database keeps them anyway) |
| `TRACE_ENVIRONMENT` | `APP_MODE` | Langfuse environment |
| `TRACE_UI_URL` | empty | `https://cloud.langfuse.com/project/<id>` for the "Open trace" links |

What goes to Langfuse: module inputs (prices, levels, holdings, theses, news headlines), prompts and answers. No API keys or account data. Volume: about 5–10k of the 50k free units a month.

Why Langfuse Cloud: it's free, has no infrastructure to run, and keeps 30 days (≥ 28 required). LangSmith's free tier keeps only 14 days. Self-hosted Langfuse needs about 16 GB RAM. Self-hosted Arize Phoenix is the fallback if traces must stay on the host.

## Setup

1. At [cloud.langfuse.com](https://cloud.langfuse.com), sign up and choose the **EU** region. Create the organisation, then the project `trading-agent`. Under **Project settings → API keys → Create**, keep the public key (`pk-lf-…`) and the secret key (`sk-lf-…`).
2. On the Zenbook as `trader`: `git pull && make trace-headers`. Paste the public key, then the secret key (not echoed).
3. In `.env`: `TRACE_UI_URL=https://cloud.langfuse.com/project/<id>` (the project page's address up to the id).
4. `make build && make migrate && make up`.
5. Check: `/status` shows `LLM tracing: on (cloud.langfuse.com, paper)`. `docker compose run --rm agent trading-agent analyse --top 1` creates a `cli analyse` trace in Langfuse, and dashboard → Analyses → **Open trace** opens it.
