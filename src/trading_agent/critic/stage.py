"""The critic's step in the step log."""

from collections.abc import Callable

from trading_agent.core.steps import Step, StepStatus
from trading_agent.critic.agent import CriticVerdict
from trading_agent.llm.runner import Outcome


def critic_step(step: Callable[..., Step], critique: Outcome[CriticVerdict]) -> Step:
    verdict = critique.output
    if verdict is None:
        return step("critic", critique, "error", f"no critique ({critique.reason})")
    status: StepStatus = "stopped" if verdict.severity == "blocking" else "passed"
    return step(
        "critic",
        critique,
        status,
        f"{verdict.severity}, confidence {verdict.confidence_delta:+.2f}: {verdict.summary}",
    )
