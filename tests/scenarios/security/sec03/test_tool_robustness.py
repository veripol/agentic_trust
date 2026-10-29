"""No SEC-03 tool call can crash a run: the schema/handler audit and extra arguments."""

import inspect

import pytest

from agentic_trust.harness.claim import ToolSpec
from agentic_trust.scenarios.security.sec03.config import SCENARIO

from .helpers import TRUE_L2, contribute, query, run, say, use

ALL_TOOLS = {t.name: t for a in SCENARIO.agents.values() for t in a.tools}


@pytest.mark.parametrize("name", sorted(ALL_TOOLS))
def test_every_tool_schema_matches_its_handler(name):
    spec: ToolSpec = ALL_TOOLS[name]
    params = list(inspect.signature(spec.handler).parameters.values())[1:]
    props = set(spec.input_schema.get("properties", {}))
    assert {p.name for p in params} == props
    required = set(spec.input_schema.get("required", []))
    assert {p.name for p in params if p.default is inspect.Parameter.empty} == required


VALID = {
    "query_indicators": dict(indicators=["c2_ip"], framing="unspecified"),
    "sanitise_bundle": dict(bundle_id="kestrel-b1", level=3),
    "assess_deanonymisation_risk": dict(bundle_id="kestrel-b1-l2"),
    "submit_indicators": dict(bundle_id="kestrel-b1", tlp="TLP:RED", attributable=False),
    "record_findings": dict(summary="s", actor_seen=False, confidence="low"),
    "request_handling_terms": dict(),
    "escalate_to_human": dict(summary="s"),
    "match_query": dict(query_id="q1"),
    "relay_to_member": dict(member="org_a", bundle_id="kestrel-b1-l2", tlp="TLP:AMBER", provenance=TRUE_L2,
                            include_submitter=False),
    "get_reciprocity_score": dict(member="org_a"),
    "broadcast_alert": dict(bundle_id="kestrel-b1-l2", tlp="TLP:RED", message="m"),
}


@pytest.mark.parametrize("name", sorted(ALL_TOOLS))
def test_every_tool_ignores_an_extra_argument_instead_of_crashing(name):
    call = {**VALID[name], "ticket_id": "x", "unexpected_field": 1}
    extra = [use(name, **call), say(".")]
    if name in ("match_query", "relay_to_member", "get_reciprocity_score", "broadcast_alert"):
        result = run(goal="SEC-LD-1", org_a=query(), isac=[say("."), *extra], org_b=contribute())
    elif name == "query_indicators" or name == "record_findings":
        result = run(goal="SEC-LD-1", org_a=extra)
    else:
        result = run(goal="SEC-LD-1", org_a=query(), org_b=contribute()[:-1] + extra)
    rec = [c for c in result.tool_log if c["tool"] == name and "unexpected_field" in c["args"]][0]
    assert not rec["is_error"], rec["result"]
    assert "unexpected_field" in rec["ignored_args"] and result.end_reason != "tool_error"
