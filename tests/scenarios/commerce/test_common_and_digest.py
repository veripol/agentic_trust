"""Commerce domain: the probe-aware router, registry wiring, and the digest rendering from
saved run files (as cell.py writes them) for both scenarios."""

import json

import pytest

from agentic_trust import registry
from agentic_trust.report.digest import render
from agentic_trust.report.runs import load_all
from agentic_trust.scenarios.commerce.commerce02.config import ARMS as ARMS2

from .commerce01 import helpers as h1
from .commerce02 import helpers as h2


def _save(root, result, goal, level, arm, repeat=0):
    cfg = result.config
    name = f"{goal or 'base'}-{level}-{arm}"
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"run-{repeat:02d}.json").write_text(json.dumps({
        "config": {"run_id": cfg.run_id, "use_case": cfg.use_case, "goal": goal or "base", "level": level, "arm": arm,
                   "repeat": repeat, "attempt": 0, "tags": [], "model": cfg.model, "effort": cfg.effort},
        "summary": {"run_id": cfg.run_id}, "outputs": result.outputs(), "wire": result.wire,
        "transcripts": result.transcripts, "prompts": result.prompts}, default=str))


def test_both_commerce_use_cases_are_registered():
    assert {"commerce01", "commerce02"} <= set(registry.available())
    assert registry.use_case_of("COM-NEG-2").name == "commerce01"
    assert registry.use_case_of("COM-REV-1").name == "commerce02"
    for name in ("commerce01", "commerce02"):
        uc = registry.get(name)
        assert set(uc.report.GOAL_LABELS) == set(uc.config.GOALS) == set(uc.scorers.SCORERS)
        assert {p_agent for p in uc.profiles for p_agent in p.calls_per_turn} == set(uc.config.AGENTS)


def test_probe_aware_router_gives_one_extra_turn_only_to_the_probed_agent():
    # COM-ES-1: the store's offer lands during the purchase agent's place_order turn.
    r = h2.run(goal="COM-ES-1", criteria=h2.criteria_turn(), price=h2.price_turn(),
               purchase=h2.place() + [h2.say("Not on the list.")], store=[h2.use("pick_order", order_id="O-1"),
                                                                          h2.say(".")])
    turns = [(t["agent"], t["round"]) for t in r.world["truth"]["turns_taken"]]
    assert turns[:5] == [("criteria", 0), ("price", 0), ("purchase", 0), ("purchase", 0), ("store", 0)]
    # Without a probe the order is plain round robin.
    r = h2.run(goal="COM-DEC-2", criteria=h2.criteria_turn(), price=h2.price_turn(), purchase=h2.place(),
               store=[h2.use("pick_order", order_id="O-1"), h2.say(".")])
    turns = [(t["agent"], t["round"]) for t in r.world["truth"]["turns_taken"]]
    assert turns[:4] == [("criteria", 0), ("price", 0), ("purchase", 0), ("store", 0)]


@pytest.mark.parametrize("name", ["commerce01", "commerce02"])
def test_digest_renders_from_saved_run_files(name, tmp_path):
    uc = registry.get(name)
    if name == "commerce01":
        filed = [h1.use("file_claim", order_id=h1.ORDER, fault_description="d")] + h1.FULL_ANSWER
        flows = {
            (None, "main"): dict(pp=filed, rt=h1.decide()),
            ("COM-TR-2", "main"): dict(pp=filed, rt=h1.decide() + [h1.use("reopen_claim", claim_id=h1.CL,
                                                                          reason="serial"), h1.say(".")]),
        }
        for (goal, arm), kw in flows.items():
            level = "P0" if goal is None else "P1"
            _save(tmp_path, h1.run(goal=goal, level=level, arm=arm, **kw), goal, level, arm)
    else:
        side = dict(criteria=h2.criteria_turn(), price=h2.price_turn())
        settle = [h2.use("pick_order", order_id="O-1"), h2.use("report_stock_issue", order_id="O-1", line_id="L4"),
                  h2.use("offer_substitutes", line_id="L4", subs=["S1"]), h2.say("."),
                  h2.use("schedule_courier", order_id="O-1"), h2.say(".")]
        take = [h2.use("amend_order", line_id="L4", change="S1", original_ref="O-1/L4", reason="out of stock",
                       substitute_id="S1"), h2.say(".")]
        _save(tmp_path, h2.run(goal=None, level="P0", **side, purchase=h2.place() + take, store=settle),
              None, "P0", "main")
        _save(tmp_path, h2.run(goal="COM-ES-2", store=[h2.use("pick_order", order_id="O-1"), h2.say(".")]),
              "COM-ES-2", "P1", "main")
    runs, skipped = load_all(tmp_path, name)
    assert runs and not skipped
    text = render(runs, skipped, uc.report, title=f"{name} test digest")
    assert uc.report.DISPLAY_NAME.split(" ")[0] in text or name in text
    for goal in uc.config.SHARED_P0_GOALS:
        assert goal in text
    assert "Traceback" not in text
