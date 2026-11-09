"""Drive COMMERCE-02 runs through the real harness with scripted agent turns (no model calls)."""

from __future__ import annotations

import itertools
import json
from typing import Any

from agentic_trust.harness.graph import RunResult, run_episode
from agentic_trust.harness.models import ModelTurn, ScriptedModel, scripted_turn
from agentic_trust.harness.state import RunConfig
from agentic_trust.scenarios.commerce.commerce02.config import LINES, QUOTES, SCENARIO, make_config
from agentic_trust.scenarios.commerce.commerce02.prompts import build_prompts

REFUSAL = ModelTurn(content=[], stop_reason="refusal", stop_details={"type": "refusal", "category": "general_harms"})
_ids = itertools.count(1)
INFERRED = ("L4", "L7")


def use(name: str, **args: Any) -> list[dict[str, Any]]:
    return [{"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": args}]


def say(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}]


def quote_of(lid: str) -> str:
    return next(q for q, v in QUOTES.items() if lid in v["lines"])


def criteria_turn(mislabel: tuple[str, ...] = ()) -> list:
    """Criteria agent: read history, set a hard nut-free criterion and one per line, with
    L4 and L7 inferred (unless relabelled in ``mislabel``)."""
    crit = [{"id": "C_nutfree", "text": "no tree nuts or peanuts", "hardness": "hard", "source": "shopper_stated"}]
    for lid, ln in LINES.items():
        inferred = lid in INFERRED and lid not in mislabel
        crit.append({"id": f"C_{lid}", "text": ln["item"], "hardness": "soft" if inferred else "hard",
                     "source": "inferred" if inferred else "shopper_stated", "line_id": lid})
    return [use("read_order_history"), use("set_criteria", criteria=crit), say("Criteria set.")]


def price_turn() -> list:
    return [use("check_prices", lines=[{"line_id": lid} for lid in LINES]), say("Quoted.")]


def order_lines(relabel: tuple[str, ...] = (), drop_quote: tuple[str, ...] = ()) -> list[dict]:
    out = []
    for lid, ln in LINES.items():
        inferred = lid in INFERRED and lid not in relabel
        deps = [f"C_{lid}"] + ([] if lid in drop_quote else [quote_of(lid)])
        out.append({"line_id": lid, "item": ln["item"], "set_by": "criteria", "quoted_by": "price",
                    "criterion_source": "inferred" if inferred else "shopper_stated", "depends_on": deps})
    return out


def place(**kw) -> list:
    return [use("place_order", lines=order_lines(**kw)), say("Order placed.")]


def amend(line_id, change, original_ref, reason, **kw) -> list:
    return [use("amend_order", line_id=line_id, change=change, original_ref=original_ref, reason=reason, **kw)]


def run(config: RunConfig | None = None, *, goal: str | None = None, level: str = "P1", arm: str = "main",
        criteria: list = (), price: list = (), purchase: list = (), store: list = ()) -> RunResult:
    """Run COMMERCE-02 with each agent replaying its scripted model calls in order."""
    config = config or make_config(goal, level, arm)
    prompts = build_prompts(config)
    scripts = {"criteria": list(criteria), "price": list(price), "purchase": list(purchase), "store": list(store)}
    queues = {prompts[a]: scripts[a] for a in config.agents}

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
