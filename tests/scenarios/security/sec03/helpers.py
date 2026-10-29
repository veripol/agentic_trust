"""Drive SEC-03 runs through the real harness with scripted agent turns (no model calls)."""

from __future__ import annotations

import itertools
from typing import Any

from agentic_trust.harness.graph import RunResult, run_episode
from agentic_trust.harness.models import ModelTurn, ScriptedModel, scripted_turn
from agentic_trust.harness.state import RunConfig
from agentic_trust.scenarios.security.sec03.config import SCENARIO, make_config
from agentic_trust.scenarios.security.sec03.prompts import build_prompts

REFUSAL = ModelTurn(content=[], stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber"})
_ids = itertools.count(1)

# The real provenance of Kestrel's level-2 bundle, as the ISAC should state it.
TRUE_L2 = {"source_type": "security_provider", "independent_sources": 1, "sanitisation_level": 2,
           "submitted_tlp": "TLP:AMBER"}


def use(name: str, **args: Any) -> list[dict[str, Any]]:
    """One model call that makes one tool call."""
    return [{"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": args}]


def say(text: str) -> list[dict[str, Any]]:
    """One model call that ends the agent's turn."""
    return [{"type": "text", "text": text}]


def query(kinds=("c2_ip",), framing="unspecified") -> list:
    """Org A turn: one query, then end the turn."""
    return [use("query_indicators", indicators=list(kinds), framing=framing), say("Query sent.")]


def contribute(level=2, tlp="TLP:AMBER", ask_terms=True) -> list:
    """Org B turn: (optionally) ask the terms, sanitise, submit, end the turn."""
    steps = [use("request_handling_terms")] if ask_terms else []
    return steps + [use("sanitise_bundle", bundle_id="kestrel-b1", level=level),
                    use("submit_indicators", bundle_id=f"kestrel-b1-l{level}", tlp=tlp, attributable=False),
                    say("Submitted.")]


def relay(bundle="kestrel-b1-l2", tlp="TLP:AMBER", provenance=None, include_submitter=False, member="org_a") -> list:
    return [use("relay_to_member", member=member, bundle_id=bundle, tlp=tlp,
                provenance=dict(provenance or TRUE_L2), include_submitter=include_submitter)]


def findings(confidence="medium") -> list:
    return [use("record_findings", summary="Peers have seen the C2 IP.", actor_seen=True, confidence=confidence),
            say("Done.")]


# The usual main-arm flow: Org A queries (round 0); the ISAC finds nothing yet; Org B
# contributes; the ISAC relays (round 1); Org A records findings (round 2).
def isac_relays_after_contribution(**relay_kw) -> list:
    return [use("match_query", query_id="q1"), say("No match yet."),
            use("match_query", query_id="q1"), *relay(**relay_kw), say("Relayed.")]


def run(config: RunConfig | None = None, *, goal: str | None = None, level: str = "P1", arm: str = "main",
        org_a: list = (), isac: list = (), org_b: list = ()) -> RunResult:
    """Run SEC-03 with each agent replaying its scripted model calls in order."""
    config = config or make_config(goal, level, arm)
    prompts = build_prompts(config)
    scripts = {"org_a": org_a, "isac": isac, "org_b": org_b}
    queues = {prompts[a]: list(scripts[a]) for a in config.agents}

    def model(*, system, tools, messages):
        queue = queues[system]
        if not queue:
            return scripted_turn(say("Nothing further."))
        nxt = queue.pop(0)
        return nxt if isinstance(nxt, ModelTurn) else scripted_turn(nxt)

    return run_episode(config, SCENARIO, prompts, ScriptedModel(model))


def user_messages(result: RunResult, agent: str) -> list[str]:
    return [m["content"] for m in result.transcripts[agent] if m["role"] == "user" and isinstance(m["content"], str)]
