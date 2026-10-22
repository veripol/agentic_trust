"""The trace a run produces, captured by a LangSmith client that records instead of sending."""

import pytest
from langsmith import Client, tracing_context

from agentic_trust.harness.graph import Probe, ScriptedCall, run_episode
from agentic_trust.harness.state import AgentAssignment
from agentic_trust.harness.tracing import run_name, run_tags
from agentic_trust.runner import tracing as runner_tracing

from .toy import PROMPTS, SECRET, say, scripted, toy_config, toy_scenario, use


class RecordingClient(Client):
    def __init__(self):
        super().__init__(api_key="test", api_url="http://localhost:9", auto_batch_tracing=False)
        self.recorded: dict = {}
        self.order: list = []

    def create_run(self, *args, **kw):
        self.recorded[kw["id"]] = dict(kw)
        self.order.append(kw["id"])

    def update_run(self, run_id, **kw):
        self.recorded.setdefault(run_id, {}).update({k: v for k, v in kw.items() if v is not None})

    def all(self):
        return [self.recorded[i] for i in self.order]


def traced(config, turns):
    client = RecordingClient()
    with tracing_context(enabled=True, client=client, project_name="agentic-trust"):
        result = run_episode(config, toy_scenario(), PROMPTS, scripted(turns))
    return result, client


def happy():
    return {
        "alice": [[use("open_ticket")], [use("make_offer", ticket_id="T-1", amount=100, floor_claim=95)], [say("sent")]],
        "bob": [[use("accept_offer", ticket_id="T-1", amount=100)]],
    }


def depth(run):
    return run["dotted_order"].count(".")


def test_one_run_is_one_trace_in_the_agentic_trust_project():
    config = toy_config(assignments={"alice": AgentAssignment("TOY-LD-1", "P1")}, tags=("smoke",))
    result, client = traced(config, happy())
    runs = client.all()
    assert len({r["trace_id"] for r in runs}) == 1
    assert {r["session_name"] for r in runs} == {"agentic-trust"}

    root = runs[0]
    assert depth(root) == 0 and root["name"] == "toy-alice/TOY-LD-1/P1" and root["run_type"] == "chain"
    assert {"toy", "arm:main", "TOY-LD-1", "toy-alice/TOY-LD-1/P1", "level:P1", "smoke"} <= set(root["tags"])
    assert root["extra"]["metadata"]["model"] == "claude-sonnet-5-5"
    assert root["extra"]["metadata"]["effort"] == "medium"
    assert root["outputs"]["end_reason"] == "agreed" == result.end_reason
    assert root["outputs"]["tool_log"] == result.tool_log


def test_model_and_tool_calls_nest_under_agent_turns():
    _, client = traced(toy_config(), happy())
    runs = client.all()
    by_id = {r["id"]: r for r in runs}
    llm = [r for r in runs if r["run_type"] == "llm"]
    tools = [r for r in runs if r["run_type"] == "tool"]
    assert [r["extra"]["metadata"]["agent"] for r in llm] == ["alice", "alice", "alice", "bob"]
    assert {r["name"] for r in llm} == {"claude-sonnet-5-5"}
    assert [r["name"] for r in tools] == ["open_ticket", "make_offer", "accept_offer"]
    for r in llm + tools:
        parent = by_id[r["parent_run_id"]]
        assert parent["name"] == r["extra"]["metadata"]["agent"]  # the agent's LangGraph node
    assert tools[1]["inputs"]["args"]["floor_claim"] == 95
    assert tools[1]["outputs"] == {"result": {"status": "sent"}, "is_error": False}


def test_llm_spans_carry_token_usage_and_a_frozen_history():
    _, client = traced(toy_config(), happy())
    llm = [r for r in client.all() if r["run_type"] == "llm"]
    assert [len(r["inputs"]["messages"]) for r in llm[:3]] == [1, 3, 5]  # not the final, longer history
    usage = llm[0]["outputs"]["usage_metadata"]
    assert set(usage) == {"input_tokens", "output_tokens", "total_tokens", "input_token_details"}
    assert llm[0]["extra"]["metadata"]["ls_model_name"] == "claude-sonnet-5-5"


def test_scripted_probe_calls_are_tagged():
    probe = Probe("G", "after_tool", "alice", ScriptedCall("alice", "make_offer", {"amount": 5}), tool="open_ticket")
    _, client = traced(toy_config(probes=(probe,)),
                       {"alice": [[use("open_ticket")], [say(".")]], "bob": [[say(".")]]})
    [scripted_span] = [r for r in client.all() if r["run_type"] == "tool" and r["name"] == "make_offer"]
    assert "scripted" in scripted_span["tags"] and scripted_span["inputs"]["scripted"] is True


def test_ground_truth_reaches_the_trace_but_not_the_prompts():
    _, client = traced(toy_config(), happy())
    root = client.all()[0]
    assert root["outputs"]["world"]["truth"]["secret"] == SECRET  # for people and scorers
    for r in client.all():
        if r["run_type"] == "llm":
            assert SECRET not in str(r["inputs"])


def test_tracing_off_sends_nothing():
    client = RecordingClient()
    with tracing_context(enabled=False, client=client):
        result = run_episode(toy_config(), toy_scenario(), PROMPTS, scripted(happy()))
    assert result.end_reason == "agreed" and client.all() == []


def test_run_names_and_tags():
    two = toy_config(assignments={"alice": AgentAssignment("A", "P1"), "bob": AgentAssignment("B", "P2")}, arm="arm-A/A2")
    assert run_name(two) == "toy/arm-A/A2[alice:A/P1,bob:B/P2]"
    assert run_name(toy_config()) == "toy/main"
    assert run_tags(two)[:2] == ["toy", "arm:arm-A/A2"]


def test_configure_defaults_the_project_and_needs_a_key(monkeypatch):
    for var in ("LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        monkeypatch.delenv(var, raising=False)
    st = runner_tracing.configure(env_file=None)
    assert st.project == "agentic-trust" and not st.api_key_present and not st.enabled
    assert "LANGSMITH_API_KEY" in st.describe()
    with pytest.raises(RuntimeError):
        runner_tracing.check()

    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    st = runner_tracing.configure(env_file=None)
    assert st.api_key_present and st.enabled and st.project == "agentic-trust"
