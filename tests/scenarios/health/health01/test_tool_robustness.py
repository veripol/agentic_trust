"""No agent tool call can crash a HEALTH-01 run: the schema/handler audit and extra arguments."""

import inspect

import pytest

from agentic_trust.harness.claim import ToolSpec
from agentic_trust.scenarios.health.health01.config import SCENARIO, make_config

from .helpers import determine, left_out, run, say, submit, use

ALL_TOOLS = {t.name: t for a in SCENARIO.agents.values() for t in a.tools}


@pytest.mark.parametrize("name", sorted(ALL_TOOLS))
def test_every_tool_schema_matches_its_handler(name):
    spec: ToolSpec = ALL_TOOLS[name]
    params = list(inspect.signature(spec.handler).parameters.values())[1:]  # after ctx
    props = set(spec.input_schema.get("properties", {}))
    assert {p.name for p in params} == props, f"{name}: handler {[p.name for p in params]} vs schema {sorted(props)}"
    required = set(spec.input_schema.get("required", []))
    no_default = {p.name for p in params if p.default is inspect.Parameter.empty}
    assert no_default == required, f"{name}: required {sorted(required)} vs handler without defaults {sorted(no_default)}"


# A valid call for every tool, made after a submission and a denial exist.
VALID = {
    "search_chart": dict(query="pain"),
    "submit_prior_auth": dict(study="MRI", clinical_elements=["E1"], narrative="n", elements_not_included=left_out("E5")),
    "respond_to_rfi": dict(clinical_elements=["E5"], narrative="n"),
    "submit_appeal": dict(narrative="n"),
    "request_peer_to_peer": dict(reason="r"),
    "abandon_request": dict(reason="r"),
    "retrieve_criteria": dict(),
    "evaluate_submission": dict(),
    "request_additional_info": dict(requested_items=["x"], disclose_criterion=False),
    "issue_determination": dict(decision="deny", criteria_cited=["C1"], rationale_to_provider="r",
                                options_considered=["deny"], reason="r"),
}


@pytest.mark.parametrize("name", sorted(ALL_TOOLS))
def test_every_tool_ignores_an_extra_argument_instead_of_crashing(name):
    cfg = make_config("HLT-ES-1", "P1")
    call = {**VALID[name], "request_id": "PA-0001", "unexpected_field": 1}
    agent = "provider" if SCENARIO.agents["provider"].tool(name) else "payer"
    # Provider: submit; payer: deny (not final). Then the tool under test on the next turn.
    provider = submit(included=("E1", "E3", "E4"), not_included=("E2", "E5"))
    payer = determine("deny", ["C1"], reason="C1.") + [say(".")]
    if agent == "provider":
        provider = provider + [use(name, **call), say(".")]
    else:
        payer = payer[:-1] + [use(name, **call), say(".")]
    result = run(cfg, provider=provider, payer=payer)
    rec = [c for c in result.tool_log if c["tool"] == name and "unexpected_field" in c["args"]][0]
    assert not rec["is_error"], rec["result"]
    assert "unexpected_field" in rec["ignored_args"] and result.end_reason != "tool_error"
