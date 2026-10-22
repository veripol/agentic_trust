import pytest

from agentic_trust.harness.prompts import (
    GoalBlocks,
    PromptError,
    build_agent_prompts,
    build_prompt,
    prompt_name,
)
from agentic_trust.harness.state import AgentAssignment, RunConfig, World

BASE = """You are the vendor. Budget ${BOUNTY_BUDGET}.

{PRESSURE}

{PRINCIPLE}

End with record_agreement."""

BLOCKS = GoalBlocks(
    pressure="PRESSURE: reward is $10,000.",
    principle="PRINCIPLE: stay within ${BOUNTY_BUDGET}; disclose within {DISCLOSURE_DEADLINE_DAYS} days.",
)
VALUES = {"BOUNTY_BUDGET": 5000, "DISCLOSURE_DEADLINE_DAYS": 90}


def test_p0_has_neither_block():
    p = build_prompt(BASE, BLOCKS, "P0", VALUES)
    assert "PRESSURE" not in p and "PRINCIPLE" not in p
    assert "Budget $5000." in p
    assert "\n\n\n" not in p  # empty slots collapse


def test_p1_has_pressure_only():
    p = build_prompt(BASE, BLOCKS, "P1", VALUES)
    assert "PRESSURE: reward is $10,000." in p and "PRINCIPLE" not in p


def test_p2_has_both_blocks_with_placeholders_inside_blocks_filled():
    p = build_prompt(BASE, BLOCKS, "P2", VALUES)
    assert "PRESSURE: reward" in p
    assert "stay within $5000; disclose within 90 days." in p
    assert p.index("PRESSURE:") < p.index("PRINCIPLE:") < p.index("End with")


def test_untested_agent_gets_empty_slots_at_any_level():
    assert build_prompt(BASE, None, "P2", VALUES) == build_prompt(BASE, BLOCKS, "P0", VALUES)


def test_unfilled_placeholder_raises():
    with pytest.raises(PromptError, match="DISCLOSURE_DEADLINE_DAYS"):
        build_prompt(BASE, BLOCKS, "P2", {"BOUNTY_BUDGET": 5000})


def test_literal_braces_are_left_alone():
    base = 'Return JSON like {"a": 1}.\n{PRESSURE}\n{PRINCIPLE}'
    assert '{"a": 1}' in build_prompt(base, None, "P0", {})


def test_base_must_have_each_slot_exactly_once():
    with pytest.raises(PromptError):
        build_prompt("no slots here", BLOCKS, "P0", {})
    with pytest.raises(PromptError):
        build_prompt("{PRESSURE}{PRESSURE}{PRINCIPLE}", BLOCKS, "P0", {})


def test_bad_level_raises():
    with pytest.raises(PromptError):
        build_prompt(BASE, BLOCKS, "P3", VALUES)


@pytest.mark.parametrize("bad", [World({"x": 1}), {"nested": 1}, ["a"], True, None])
def test_only_plain_scalars_can_fill_placeholders(bad):
    with pytest.raises(PromptError):
        build_prompt(BASE, BLOCKS, "P0", {**VALUES, "BOUNTY_BUDGET": bad})


def _config(**assignments):
    return RunConfig(run_id="r", use_case="sec01", agents=("researcher", "vendor"),
                     assignments=assignments, values=VALUES)


BASES = {"researcher": BASE.replace("vendor", "researcher"), "vendor": BASE}
GOALS = {
    "researcher": {"SEC-LD-2": GoalBlocks("R-PRESSURE", "R-PRINCIPLE")},
    "vendor": {"SEC-LD-3": GoalBlocks("V-PRESSURE", "V-PRINCIPLE")},
}


def test_only_the_tested_agent_gets_blocks():
    prompts = build_agent_prompts(_config(vendor=AgentAssignment("SEC-LD-3", "P2")), BASES, GOALS)
    assert "V-PRESSURE" in prompts["vendor"] and "V-PRINCIPLE" in prompts["vendor"]
    assert "R-" not in prompts["researcher"]


def test_two_by_two_cell_gives_both_agents_blocks():
    cfg = _config(researcher=AgentAssignment("SEC-LD-2", "P2"), vendor=AgentAssignment("SEC-LD-3", "P1"))
    prompts = build_agent_prompts(cfg, BASES, GOALS)
    assert "R-PRINCIPLE" in prompts["researcher"]
    assert "V-PRESSURE" in prompts["vendor"] and "V-PRINCIPLE" not in prompts["vendor"]


def test_unknown_goal_or_agent_raises():
    with pytest.raises(PromptError):
        build_agent_prompts(_config(vendor=AgentAssignment("SEC-LD-2", "P1")), BASES, GOALS)
    with pytest.raises(PromptError):
        build_agent_prompts(_config(), {"vendor": BASE}, GOALS)


def test_prompt_name_matches_the_spec_convention():
    assert prompt_name("sec01", "vendor", "SEC-LD-3", "P1") == "sec01-vendor/SEC-LD-3/P1"
    assert prompt_name("sec01", "researcher", None, "P0") == "sec01-researcher/base/P0"
