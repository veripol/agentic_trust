import pytest

from agentic_trust.harness.claim import ClaimLogger, ToolSpec, execute_tool
from agentic_trust.harness.state import RunState, World

from .toy import OFFER, OPEN, toy_config


@pytest.fixture
def state():
    rs = RunState(config=toy_config(), world=World({"floor": 80}))
    execute_tool(rs, "alice", OPEN, {})
    return rs


def test_claim_records_both_the_claim_and_the_truth(state):
    execute_tool(state, "alice", OFFER, {"ticket_id": "T-1", "amount": 100, "floor_claim": 95})
    [claim] = state.world.claims
    assert (claim.agent, claim.tool, claim.field) == ("alice", "make_offer", "floor_claim")
    assert claim.claimed == 95 and claim.truth == 80 and not claim.matches


def test_no_claim_when_the_asserting_field_is_not_set(state):
    execute_tool(state, "alice", OFFER, {"ticket_id": "T-1", "amount": 100})
    assert state.world.claims == []


def test_truth_is_read_at_call_time(state):
    execute_tool(state, "alice", OFFER, {"ticket_id": "T-1", "amount": 100, "floor_claim": 80})
    state.world["floor"] = 90
    execute_tool(state, "alice", OFFER, {"ticket_id": "T-1", "amount": 100, "floor_claim": 80})
    assert [c.matches for c in state.world.claims] == [True, False]


def test_invalid_arguments_return_an_error_and_never_reach_the_handler(state):
    outcome = execute_tool(state, "alice", OFFER, {"ticket_id": "T-1", "amount": "lots"})
    assert outcome.is_error and "amount" in outcome.result["error"]
    assert state.wire.messages == []  # the handler would have posted
    assert state.tool_log[-1].is_error and state.tool_log[-1].args["amount"] == "lots"


def test_tool_error_is_returned_and_logs_no_claim(state):
    outcome = execute_tool(state, "alice", OFFER, {"ticket_id": "T-9", "amount": 5, "floor_claim": 80})
    assert outcome.is_error and "unknown ticket" in outcome.result["error"]
    assert state.world.claims == []


def test_internal_fields_are_logged_but_never_forwarded(state):
    execute_tool(state, "alice", OFFER,
                 {"ticket_id": "T-1", "amount": 100, "private_note": "floor is 80, push to 95"})
    [msg] = state.wire.messages
    assert "private_note" not in msg.content
    assert "floor is 80" not in msg.render()
    assert state.tool_log[-1].args["private_note"] == "floor is 80, push to 95"


def test_scripted_calls_are_flagged_in_the_log_and_claims(state):
    execute_tool(state, "alice", OFFER, {"ticket_id": "T-1", "amount": 1, "floor_claim": 1}, scripted=True)
    assert state.tool_log[-1].scripted and state.world.claims[-1].scripted
    assert state.wire.messages[-1].scripted


def test_tool_log_has_round_day_and_sequence(state):
    state.round, state.clock.day = 2, 4
    execute_tool(state, "alice", OFFER, {"ticket_id": "T-1", "amount": 1})
    rec = state.tool_log[-1]
    assert (rec.seq, rec.round, rec.day, rec.agent, rec.tool) == (2, 2, 4, "alice", "make_offer")


def test_tool_spec_rejects_internal_or_claim_fields_missing_from_schema():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    with pytest.raises(ValueError):
        ToolSpec("t", "d", schema, lambda ctx, **a: None, internal_fields=("b",))
    with pytest.raises(ValueError):
        ToolSpec("t", "d", schema, lambda ctx, **a: None, claims={"b": lambda w, a, r: None})


def test_tool_definition_is_the_messages_api_shape():
    d = OFFER.definition()
    assert set(d) == {"name", "description", "input_schema"}
    d["input_schema"]["properties"].clear()
    assert OFFER.input_schema["properties"]  # definition() returns a copy


def test_claim_logger_can_be_used_directly(state):
    claim = ClaimLogger(state.world).log(state=state, agent="isac", tool="relay_to_member",
                                         field="provenance.independent_sources", claimed=3, truth=1)
    assert state.world.claims[-1] is claim
    assert state.world.snapshot()["claims"][-1]["claimed"] == 3
