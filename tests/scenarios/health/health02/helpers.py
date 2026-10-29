"""Drive HEALTH-02 runs through the real harness with scripted agent turns (no model calls)."""

from __future__ import annotations

import itertools
import json
import re
from typing import Any

from agentic_trust.harness.graph import RunResult, run_episode
from agentic_trust.harness.models import ModelTurn, ScriptedModel, scripted_turn
from agentic_trust.harness.state import RunConfig
from agentic_trust.scenarios.health.health02.config import SCENARIO, make_config
from agentic_trust.scenarios.health.health02.prompts import build_prompts

REFUSAL = ModelTurn(content=[], stop_reason="refusal", stop_details={"type": "refusal", "category": "general_harms"})
ALL = ["E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8"]
_ids = itertools.count(1)


def use(name: str, **args: Any) -> list[dict[str, Any]]:
    return [{"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": args}]


def say(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}]


def refer(*facilities: str, elements=ALL, packet: str = "PKT-0") -> list:
    """Hospital turn: read the record, assemble a packet, refer it to each facility."""
    return ([use("read_patient_record"), use("assemble_referral_packet", included_elements=list(elements))]
            + [use("send_referral", facility=f, packet_id=packet) for f in facilities] + [say("Referrals sent.")])


def respond(response: str, stated: str, internal: str, conditions=None) -> list:
    """SNF turn: check capacity, then respond."""
    args = dict(response=response, stated_reason=stated, internal_reason=internal)
    if conditions is not None:
        args["conditions"] = conditions
    return [use("check_capacity"), use("respond_to_referral", **args), say("Responded.")]


def run(config: RunConfig | None = None, *, goal: str | None = None, level: str = "P1", arm: str = "main",
        hospital: list = (), snf_a: list = (), snf_b: list = (), payer: list = ()) -> RunResult:
    """Run HEALTH-02 with each agent replaying its scripted model calls in order. The two SNF
    agents share a prompt, so they are told apart by the facility in their first message."""
    config = config or make_config(goal, level, arm)
    prompts = build_prompts(config)
    queues = {"hospital": list(hospital), "snf_a": list(snf_a), "snf_b": list(snf_b), "payer": list(payer)}

    def agent_of(system, messages) -> str:
        names = [a for a, p in prompts.items() if p == system]
        if len(names) == 1:
            return names[0]
        first = messages[0]["content"]
        m = re.search(r'"facility": "SNF-([AB])"', first if isinstance(first, str) else json.dumps(first))
        return "snf_a" if m and m.group(1) == "A" else "snf_b"

    def model(*, system, tools, messages):
        queue = queues[agent_of(system, messages)]
        if not queue:
            return scripted_turn(say("Nothing further."))
        nxt = queue.pop(0)
        return nxt if isinstance(nxt, ModelTurn) else scripted_turn(nxt)

    return run_episode(config, SCENARIO, prompts, ScriptedModel(model))


def user_messages(result: RunResult, agent: str) -> list[str]:
    return [m["content"] for m in result.transcripts[agent] if m["role"] == "user" and isinstance(m["content"], str)]
