"""Drive COMMERCE-01 runs through the real harness with scripted agent turns (no model calls)."""

from __future__ import annotations

import itertools
import json
from typing import Any

from agentic_trust.harness.graph import RunResult, run_episode
from agentic_trust.harness.models import ModelTurn, ScriptedModel, scripted_turn
from agentic_trust.harness.state import RunConfig
from agentic_trust.scenarios.commerce.commerce01.config import SCENARIO, make_config
from agentic_trust.scenarios.commerce.commerce01.prompts import build_prompts

REFUSAL = ModelTurn(content=[], stop_reason="refusal", stop_details={"type": "refusal", "category": "general_harms"})
_ids = itertools.count(1)
CL = "CL-1"
ORDER = "ORD-5512"


def use(name: str, **args: Any) -> list[dict[str, Any]]:
    return [{"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": args}]


def say(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}]


def item(item_id: str, value: str, source: str) -> dict[str, str]:
    return {"item_id": item_id, "value": value, "source": source}


def file(desc: str = "Laptop won't power on; screen dead, no charge light.") -> list:
    return [use("file_claim", order_id=ORDER, fault_description=desc), say("Filed.")]


def submit(*items: dict, note: str | None = None) -> list:
    args = {"claim_id": CL, "items": list(items)}
    if note:
        args["note"] = note
    return [use("submit_claim_info", **args), say("Sent.")]


EXACT = "2027-03-01"
# The usual full answer: serial and exact fault date from the shopper, stating the need.
FULL_ANSWER = [use("ask_shopper", question="When did you first notice the fault, what's the serial number, "
                                           "and what do you need?"),
               *submit(item("serial_number", "SN-4471-K", "shopper"), item("fault_first_noticed", EXACT, "shopper"),
                       item("shopper_need", "A working laptop within 7 days; no preference between repair, "
                                            "replacement or refund.", "shopper"))]


def decide(decision="approve", remedy="replace", inputs=("serial_number", "fault_first_noticed", "W1", "W2")) -> list:
    args = {"claim_id": CL, "decision": decision, "inputs_used": list(inputs)}
    if remedy:
        args["remedy"] = remedy
    return [use("decide_claim", **args), say("Decided.")]


def propose(remedy, alternatives=(), note=None, compensation=None) -> list:
    args = {"claim_id": CL, "remedy": remedy}
    if alternatives:
        args["alternatives"] = list(alternatives)
    if note:
        args["note"] = note
    if compensation is not None:
        args["compensation_usd"] = compensation
    return [use("propose_remedy", **args), say("Proposed.")]


def run(config: RunConfig | None = None, *, goal: str | None = None, level: str = "P1", arm: str = "main",
        pp: list = (), rt: list = ()) -> RunResult:
    """Run COMMERCE-01 with each agent replaying its scripted model calls in order; when a
    script runs out the agent says it has nothing further."""
    config = config or make_config(goal, level, arm)
    prompts = build_prompts(config)
    queues = {prompts["post_purchase"]: list(pp), prompts["returns"]: list(rt)}

    def model(*, system, tools, messages):
        queue = queues[system]
        if not queue:
            return scripted_turn(say("Nothing further."))
        nxt = queue.pop(0)
        return nxt if isinstance(nxt, ModelTurn) else scripted_turn(nxt)

    return run_episode(config, SCENARIO, prompts, ScriptedModel(model))


def user_messages(result: RunResult, agent: str) -> list[str]:
    return [m["content"] for m in result.transcripts[agent] if m["role"] == "user" and isinstance(m["content"], str)]


def saved(result: RunResult) -> dict:
    """What the digest reads: the run's outputs after a JSON round trip (int keys -> strings)."""
    return json.loads(json.dumps(result.outputs(), default=str))
