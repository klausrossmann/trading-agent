import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from trading_agent.llm.models import build_model, load_models_config
from trading_agent.llm.prompts import load_prompt
from trading_agent.llm.runner import AnalysisModule, LlmRunner
from trading_agent.llm.store import MemoryStore
from trading_agent.modules.earnings import EarningsInput, EarningsModule
from trading_agent.modules.technical import TechnicalInput, TechnicalModule

ROOT = Path(__file__).resolve().parents[2]
CASES = sorted((ROOT / "tests" / "evals" / "cases").glob("*/*.json"))
INPUTS: dict[str, type[TechnicalInput] | type[EarningsInput]] = {
    "technical": TechnicalInput,
    "earnings": EarningsInput,
}


class EvalKeys(BaseSettings):
    """Only the LLM settings, from the environment or ./.env (no database needed)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_key: SecretStr | None = None
    llm_dev_overrides: bool = False


def _case(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _summary(output: BaseModel | None) -> str:
    if output is None:
        return "-"
    data = output.model_dump()
    if "rating" in data:
        refs = "/".join(str(data[k]) for k in ("entry_ref", "stop_ref", "target_ref"))
        return f"{data['rating']} q={data['setup_quality']:.2f} {refs}"
    return f"{data['stance']} risk={data['event_risk']} c={data['confidence']:.2f}"


def _modules() -> dict[str, AnalysisModule[Any, Any]]:
    prompts = ROOT / "prompts"
    return {
        "technical": TechnicalModule(load_prompt(prompts, "technical", 1)),
        "earnings": EarningsModule(load_prompt(prompts, "earnings", 1)),
    }


@pytest.mark.parametrize("path", CASES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_case_matches_the_input_schema(path: Path) -> None:
    case = _case(path)
    inp = INPUTS[case["module"]].model_validate(case["input"])
    assert inp.model_dump(mode="json") == case["input"]


def test_there_are_about_thirty_cases() -> None:
    assert len(CASES) >= 30


@pytest.fixture(scope="module")
def runner() -> LlmRunner:
    keys = EvalKeys()
    if keys.gemini_api_key is None or not keys.gemini_api_key.get_secret_value():
        pytest.skip("GEMINI_API_KEY is not set")
    # A fresh in-memory store: evals always call the model, never the cache.
    return LlmRunner(
        load_models_config(ROOT / "config"),
        MemoryStore(),
        lambda name: build_model(name, gemini_api_key=keys.gemini_api_key),
        dev_overrides=keys.llm_dev_overrides,
    )


@pytest.mark.llm
@pytest.mark.parametrize("path", CASES, ids=lambda p: f"{p.parent.name}/{p.stem}")
async def test_golden_case(
    path: Path, runner: LlmRunner, eval_results: list[dict[str, Any]]
) -> None:
    case = _case(path)
    inp = INPUTS[case["module"]].model_validate(case["input"])
    out = await runner.run(_modules()[case["module"]], inp, instrument_id=None, as_of=inp.as_of)
    eval_results.append(
        {
            "case": f"{case['module']}/{path.stem}",
            "status": out.status,
            "summary": _summary(out.output),
            "issues": [i.code for i in out.issues],
            "cost_usd": out.cost_usd,
        }
    )
    assert out.status == "ok", out.reason or [i.message for i in out.issues]
    assert out.output is not None
    if ratings := case["expect"].get("ratings"):
        assert out.output.model_dump()["rating"] in ratings
