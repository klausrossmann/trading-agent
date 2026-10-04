from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from trading_agent.risk.config import RiskConfig, load_risk_config

ROOT = Path(__file__).resolve().parents[2]


def test_repo_risk_config_matches_concept() -> None:
    risk = load_risk_config(ROOT / "config")
    assert risk.capital.agent_budget_eur == 1000
    assert risk.per_trade.max_risk_pct == Decimal("1.5")
    assert risk.portfolio.max_open_positions == 4
    assert risk.loss_limits.max_drawdown_pct == 15
    assert risk.markets.live == ["US"]


def test_paper_sleeves() -> None:
    risk = load_risk_config(ROOT / "config")
    sleeves = [(ms, r.capital.agent_budget_eur) for ms, r in risk.sleeves(["US", "EU"], "paper")]
    assert sleeves == [(["US"], 1000), (["EU"], 5000)]
    assert [(ms, r.capital.agent_budget_eur) for ms, r in risk.sleeves(["US", "EU"], "live")] == [
        (["US", "EU"], 1000)
    ]


def test_unknown_keys_are_rejected() -> None:
    raw = yaml.safe_load((ROOT / "config" / "risk.yaml").read_text())
    raw["per_trade"]["max_risk_pc"] = 2  # typo
    with pytest.raises(ValidationError, match="max_risk_pc"):
        RiskConfig.model_validate(raw)


def test_stop_atr_bounds_must_be_ordered() -> None:
    raw = yaml.safe_load((ROOT / "config" / "risk.yaml").read_text())
    raw["per_trade"]["stop_atr_min"] = 4
    with pytest.raises(ValidationError, match="stop_atr_min must be below stop_atr_max"):
        RiskConfig.model_validate(raw)
