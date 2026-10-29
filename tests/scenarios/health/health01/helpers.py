"""Drive HEALTH-01 runs through the real harness with scripted agent turns (no model calls)."""

from __future__ import annotations

import itertools
from typing import Any

from agentic_trust.harness.graph import RunResult, run_episode
from agentic_trust.harness.models import ModelTurn, ScriptedModel, scripted_turn
from agentic_trust.harness.state import RunConfig
from agentic_trust.scenarios.health.health01.config import SCENARIO, make_config
from agentic_trust.scenarios.health.health01.prompts import build_prompts

REFUSAL = ModelTurn(content=[], stop_reason="refusal", stop_details={"type": "refusal", "category": "general_harms"})
_ids = itertools.count(1)


def use(name: str, **args: Any) -> list[dict[str, Any]]:
    """One model call that makes one tool call."""
    return [{"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": args}]


def say(text: str) -> list[dict[str, Any]]:
    """One model call that ends the agent's turn."""
    return [{"type": "text", "text": text}]


def left_out(*pairs: str) -> list[dict[str, str]]:
    return [{"element_id": e, "reason": "not relevant to the request"} for e in pairs]


def submit(included=("E1", "E2", "E3"), not_included=("E4", "E5"), search=True) -> list:
    """Provider turn: search the chart (optional), submit, end the turn."""
    steps = [use("search_chart", query="lumbar")] if search else []
    return steps + [
        use("submit_prior_auth", study="MRI lumbar spine", clinical_elements=list(included),
            narrative="New foot drop with 8 weeks of low back pain.", elements_not_included=left_out(*not_included)),
        say("Submitted."),
    ]


def determine(decision="approve", cited=("C1", "C2", "C3"), reason="C1, C2 and C3 are met.",
              rationale="Criteria met.", evaluate=True) -> list:
    """Payer turn: (evaluate,) then issue a determination."""
    steps = [use("evaluate_submission")] if evaluate else []
    return steps + [use("issue_determination", decision=decision, criteria_cited=list(cited),
                        rationale_to_provider=rationale, options_considered=["approve", "deny"], reason=reason)]


def run(config: RunConfig | None = None, *, goal: str | None = None, level: str = "P1", arm: str = "main",
        provider: list = (), payer: list = ()) -> RunResult:
    """Run HEALTH-01 with each agent replaying its scripted model calls in order.
    When an agent's script runs out it just says it has nothing further."""
    config = config or make_config(goal, level, arm)
    prompts = build_prompts(config)
    queues = {prompts["provider"]: list(provider), prompts["payer"]: list(payer)}

    def model(*, system, tools, messages):
        queue = queues[system]
        if not queue:
            return scripted_turn(say("Nothing further."))
        nxt = queue.pop(0)
        return nxt if isinstance(nxt, ModelTurn) else scripted_turn(nxt)

    return run_episode(config, SCENARIO, prompts, ScriptedModel(model))


def user_messages(result: RunResult, agent: str) -> list[str]:
    return [m["content"] for m in result.transcripts[agent] if m["role"] == "user" and isinstance(m["content"], str)]
