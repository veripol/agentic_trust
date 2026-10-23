"""Drive SEC-01 runs through the real harness with scripted agent turns (no model calls)."""

from __future__ import annotations

import itertools
from typing import Any

from agentic_trust.harness.graph import RunResult, run_episode
from agentic_trust.harness.models import ModelTurn, ScriptedModel, scripted_turn
from agentic_trust.harness.state import RunConfig
from agentic_trust.scenarios.security.sec01.config import SCENARIO, make_config
from agentic_trust.scenarios.security.sec01.prompts import build_prompts
from agentic_trust.scenarios.security.sec01.tools import artifact_hash

T = "ACME-GW-4.2-0001"
REFUSAL = ModelTurn(content=[], stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber"})
_ids = itertools.count(1)


def use(name: str, **args: Any) -> list[dict[str, Any]]:
    """One model call that makes one tool call."""
    return [{"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": args}]


def say(text: str) -> list[dict[str, Any]]:
    """One model call that ends the agent's turn."""
    return [{"type": "text", "text": text}]


def h(config: RunConfig, n: int, kind: str = "artifact") -> str:
    """Hash of the n-th artifact or escrow reference created in this run."""
    return artifact_hash(config.run_id, kind, n)


LABEL = {0: "full_poc", 1: "redacted_poc", 2: "redacted_poc", 3: "description_only"}


def report(config: RunConfig, level: int = 1, n: int = 1) -> list:
    """Researcher turn: prepare an artifact at ``level`` and send it outside escrow."""
    return [
        use("prepare_evidence", redaction_level=level),
        use("submit_vulnerability_report", vendor_id="acme", title="Pre-auth path traversal",
            affected_component="Acme Gateway 4.2", claimed_severity="critical",
            evidence_level=LABEL[level], artifact_hash=h(config, n)),
        say("Report sent."),
    ]


def triage(config: RunConfig, status: str = "reproduced", n: int = 1, *, reproduce: bool = True) -> list:
    """Vendor turn: (optionally) reproduce artifact n, then report ``status``."""
    steps = [use("reproduce_issue", ticket_id=T, artifact_hash=h(config, n))] if reproduce else []
    return steps + [use("report_reproduction_status", ticket_id=T, status=status, request_more_evidence=False),
                    say("Triage reported.")]


def run(config: RunConfig | None = None, *, goal: str | None = None, level: str = "P1", arm: str = "main",
        researcher: list = (), vendor: list = ()) -> RunResult:
    """Run SEC-01 with each agent replaying its scripted model calls in order.
    When an agent's script runs out it just says it has nothing further."""
    config = config or make_config(goal, level, arm)
    prompts = build_prompts(config)
    queues = {prompts["researcher"]: list(researcher), prompts["vendor"]: list(vendor)}

    def model(*, system, tools, messages):
        queue = queues[system]
        if not queue:
            return scripted_turn(say("Nothing further."))
        nxt = queue.pop(0)
        return nxt if isinstance(nxt, ModelTurn) else scripted_turn(nxt)

    return run_episode(config, SCENARIO, prompts, ScriptedModel(model))


def first_user_message(result: RunResult, agent: str, turn: int = 0) -> str:
    texts = [m["content"] for m in result.transcripts[agent] if m["role"] == "user" and isinstance(m["content"], str)]
    return texts[turn]
