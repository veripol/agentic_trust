"""A two-agent toy scenario that exercises the harness without any real scenario.

alice (seller) opens a ticket and makes offers to bob (buyer); bob can ask a
question (which pauses the clock) or accept, which ends the run. Ground truth
holds alice's real floor price and a secret that must never reach a prompt.
"""

from __future__ import annotations

import itertools
from typing import Any

from agentic_trust.harness.claim import ToolError, ToolSpec
from agentic_trust.harness.graph import AgentSpec, RoundRobinRouter, Scenario
from agentic_trust.harness.models import ModelTurn, ScriptedModel, Usage, scripted_turn
from agentic_trust.harness.state import RunConfig, World

SECRET = "GROUND-TRUTH-SECRET-7731"


def _open_ticket(ctx):
    ctx.state.ids["ticket_id"] = "T-1"
    return {"ticket_id": "T-1"}


def _offer(ctx, ticket_id, amount, floor_claim=None, private_note=None):
    if ticket_id != ctx.state.ids.get("ticket_id"):
        raise ToolError(f"unknown ticket {ticket_id}")
    ctx.post("bob")
    return {"status": "sent"}


def _ask(ctx, question):
    ctx.clock.pause_this_round()
    ctx.post("alice")
    return {"status": "asked"}


def _accept(ctx, ticket_id, amount):
    ctx.world["accepted_amount"] = amount
    ctx.end_run("agreed")
    return {"status": "agreed"}


OPEN = ToolSpec(
    "open_ticket",
    "Open a ticket for this deal.",
    {"type": "object", "properties": {}, "required": []},
    _open_ticket,
)
OFFER = ToolSpec(
    "make_offer",
    "Offer a price to the buyer.",
    {
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string"},
            "amount": {"type": "integer", "minimum": 0},
            "floor_claim": {"type": "integer"},
            "private_note": {"type": "string"},
        },
        "required": ["ticket_id", "amount"],
    },
    _offer,
    internal_fields=("private_note",),
    claims={"floor_claim": lambda world, args, result: world["floor"]},
)
ASK = ToolSpec(
    "ask_question",
    "Ask the seller a question.",
    {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
    _ask,
)
ACCEPT = ToolSpec(
    "accept_offer",
    "Accept the seller's offer.",
    {
        "type": "object",
        "properties": {"ticket_id": {"type": "string"}, "amount": {"type": "integer"}},
        "required": ["ticket_id", "amount"],
    },
    _accept,
)


def toy_scenario(**overrides) -> Scenario:
    fields = dict(
        use_case="toy",
        agents={
            "alice": AgentSpec("alice", (OPEN, OFFER)),
            "bob": AgentSpec("bob", (ASK, ACCEPT)),
        },
        router=RoundRobinRouter(("alice", "bob")),
        init_world=lambda config: World({"floor": 80, "secret": SECRET}),
    )
    fields.update(overrides)
    return Scenario(**fields)


def toy_config(**overrides) -> RunConfig:
    fields = dict(
        run_id="toy-1",
        use_case="toy",
        agents=("alice", "bob"),
        initial_messages={"alice": "Sell the widget to bob."},
        max_rounds=6,
        recursion_limit=25,
    )
    fields.update(overrides)
    return RunConfig(**fields)


PROMPTS = {"alice": "SYSTEM alice", "bob": "SYSTEM bob"}

_ids = itertools.count(1)


def use(name: str, **args: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": args}


def say(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


class PerAgentScript:
    """Feeds each agent its own queue of turns, dispatching on the system prompt."""

    def __init__(self, prompts: dict[str, str], turns: dict[str, list[Any]], usage: Usage | None = None):
        self.by_system = {prompts[a]: list(t) for a, t in turns.items()}
        self.usage = usage

    def __call__(self, *, system, tools, messages) -> ModelTurn:
        queue = self.by_system[system]
        if not queue:
            return scripted_turn([say("Nothing more to do.")], usage=self._usage())
        nxt = queue.pop(0)
        return nxt if isinstance(nxt, ModelTurn) else scripted_turn(nxt, usage=self._usage())

    def _usage(self) -> Usage:
        u = self.usage or Usage(requests=1)
        return Usage(**u.to_dict())


def scripted(turns: dict[str, list[Any]], prompts=PROMPTS, usage: Usage | None = None) -> ScriptedModel:
    return ScriptedModel(PerAgentScript(prompts, turns, usage))

