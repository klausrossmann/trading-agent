"""Golden-case evals against the real models: `pytest -m llm tests/evals` (needs GEMINI_API_KEY)."""

from typing import Any

import pytest
from pydantic_ai import models

# case, status, summary, issues, cost_usd per evaluated case
RESULTS_KEY = pytest.StashKey[list[dict[str, Any]]]()


@pytest.fixture
def eval_results(request: pytest.FixtureRequest) -> list[dict[str, Any]]:
    return request.config.stash.setdefault(RESULTS_KEY, [])


@pytest.fixture(autouse=True)
def _allow_model_requests(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("llm"):
        monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)


def pytest_terminal_summary(terminalreporter: Any) -> None:
    results = terminalreporter.config.stash.get(RESULTS_KEY, [])
    if not results:
        return
    tr = terminalreporter
    tr.section("LLM evals")
    for r in results:
        issues = f"  [{', '.join(r['issues'])}]" if r["issues"] else ""
        tr.write_line(
            f"{r['status']:<8} {r['case']:<40} {r['summary']}{issues}  ${r['cost_usd']:.4f}"
        )
    ok = sum(1 for r in results if r["status"] == "ok")
    total = sum(r["cost_usd"] for r in results)
    tr.write_line(f"{ok}/{len(results)} ok, cost ${total:.4f} (list prices)")
