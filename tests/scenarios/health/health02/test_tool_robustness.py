"""No HEALTH-02 tool call can crash a run: the schema/handler audit and extra arguments."""

import inspect

import pytest

from agentic_trust.scenarios.health.health02.config import SCENARIO

from .helpers import refer, run, say, use

ALL_TOOLS = {t.name: (agent, t) for agent, a in SCENARIO.agents.items() for t in a.tools}


@pytest.mark.parametrize("name", sorted(ALL_TOOLS))
def test_every_tool_schema_matches_its_handler(name):
    spec = ALL_TOOLS[name][1]
    params = list(inspect.signature(spec.handler).parameters.values())[1:]
    props = set(spec.input_schema.get("properties", {}))
    assert {p.name for p in params} == props
    required = set(spec.input_schema.get("required", []))
    assert {p.name for p in params if p.default is inspect.Parameter.empty} == required


VALID = {
    "read_patient_record": {}, "assemble_referral_packet": {"included_elements": ["E1"]},
    "send_referral": {"facility": "SNF-A", "packet_id": "PKT-0"},
    "answer_clarification": {"facility": "SNF-A", "answer": "a", "certainty": "suspected"},
    "record_discharge": {"facility": "SNF-A", "setting": "snf"}, "escalate_to_human": {"summary": "s"},
    "review_packet": {"packet_id": "PKT-0"}, "check_capacity": {}, "estimate_reimbursement": {},
    "request_clarification": {"questions": ["q"]},
    "respond_to_referral": {"response": "accept", "stated_reason": "s", "internal_reason": "i"},
    "retrieve_loc_criteria": {}, "evaluate_level_of_care": {},
    "authorise_level_of_care": {"setting": "snf", "days": 3, "rationale": "r"},
}


@pytest.mark.parametrize("name", sorted(ALL_TOOLS))
def test_every_tool_ignores_an_extra_argument_instead_of_crashing(name):
    agent = ALL_TOOLS[name][0]
    call = {**VALID[name], "ticket_id": "X", "unexpected_field": 1}
    scripts = {"hospital": refer("SNF-A")[:-1], "snf_a": [], "snf_b": [], "payer": []}
    if agent == "hospital":
        scripts["hospital"] += [use(name, **call), say(".")]
    else:
        scripts["hospital"] += [say(".")]
        target = "snf_a" if agent in ("snf_a", "snf_b") else agent
        scripts[target] = [use(name, **call), say(".")]
    result = run(goal="HLT-ES-2", **scripts)
    rec = [c for c in result.tool_log if c["tool"] == name and "unexpected_field" in c["args"]][0]
    assert not rec["is_error"], rec["result"]
    assert "unexpected_field" in rec["ignored_args"] and result.end_reason != "tool_error"
