import pytest
from anthropic.types import Message

from agentic_trust.harness.models import (
    DEFAULT_MODEL,
    PRICING,
    AnthropicModel,
    ModelSettings,
    ScriptedModel,
    Usage,
    scripted_turn,
    turn_from_response,
)

TOOLS = [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
MESSAGES = [{"role": "user", "content": "hi"}]


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response):
        self.messages = FakeMessages(response)


def _message(**overrides) -> Message:
    data = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig-abc"},
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "toolu_1", "name": "t", "input": {"x": 1}},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 0,
        },
    }
    data.update(overrides)
    return Message.model_validate(data)


def test_default_agent_model_is_sonnet_5_5():
    assert DEFAULT_MODEL == "claude-sonnet-5-5"
    assert ModelSettings().model == "claude-sonnet-5-5"


def test_request_shape():
    model = AnthropicModel(client=FakeClient(_message()))
    req = model.build_request(system="SYS", tools=TOOLS, messages=MESSAGES)
    assert req["model"] == "claude-sonnet-5-5"
    assert req["tool_choice"] == {"type": "auto"}  # forced tool use is rejected on this model
    assert req["output_config"] == {"effort": "medium"}
    for banned in ("temperature", "top_p", "top_k", "thinking", "fallbacks"):
        assert banned not in req
    # caching: explicit breakpoint on system (caches tools + system) and automatic tail caching
    assert req["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    assert req["cache_control"] == {"type": "ephemeral"}


def test_request_without_tools_or_cache_or_effort():
    model = AnthropicModel(ModelSettings(cache=False, effort=None), client=FakeClient(_message()))
    req = model.build_request(system="SYS", tools=[], messages=MESSAGES)
    assert "tools" not in req and "tool_choice" not in req
    assert "cache_control" not in req and "cache_control" not in req["system"][0]
    assert "output_config" not in req


def test_request_copies_history():
    model = AnthropicModel(client=FakeClient(_message()))
    history = [{"role": "user", "content": [{"type": "text", "text": "a"}]}]
    req = model.build_request(system="S", tools=TOOLS, messages=history)
    req["messages"][0]["content"][0]["text"] = "mutated"
    assert history[0]["content"][0]["text"] == "a"


def test_complete_sends_the_request_and_returns_plain_blocks():
    client = FakeClient(_message())
    turn = AnthropicModel(client=client).complete(system="S", tools=TOOLS, messages=MESSAGES)
    assert client.messages.calls[0]["model"] == "claude-sonnet-5-5"
    assert turn.stop_reason == "tool_use"
    assert turn.tool_uses == [{"type": "tool_use", "id": "toolu_1", "name": "t", "input": {"x": 1}}]
    assert turn.text == "Checking."
    # thinking blocks come back unchanged so they can be replayed (preserved thinking)
    assert turn.content[0] == {"type": "thinking", "thinking": "", "signature": "sig-abc"}
    assert turn.usage.to_dict() == {
        "input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 0, "requests": 1,
    }


def test_refusal_is_surfaced_not_retried():
    msg = _message(
        content=[], stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": "x"},
    )
    turn = turn_from_response(msg)
    assert turn.stop_reason == "refusal" and turn.stop_details["category"] == "cyber"


def test_pricing_for_sonnet_5_5():
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000,
                  cache_read_input_tokens=2_000_000, cache_creation_input_tokens=400_000)
    p = PRICING["claude-sonnet-5-5"]
    expected = 2.00 + 1.00 + 0.40 + 1.00
    assert p.cost(usage) == pytest.approx(expected)
    assert p.cost(usage, batch=True) == pytest.approx(expected / 2)


def test_usage_adds_up():
    total = Usage()
    total.add(Usage(1, 2, 3, 4, 1))
    total.add(Usage(1, 2, 3, 4, 1))
    assert total.to_dict()["requests"] == 2 and total.total_tokens == 20


def test_scripted_model_replays_and_records():
    model = ScriptedModel([[{"type": "text", "text": "done"}]])
    turn = model.complete(system="S", tools=TOOLS, messages=MESSAGES)
    assert turn.stop_reason == "end_turn" and turn.text == "done"
    assert model.requests[0]["system"] == "S"
    with pytest.raises(RuntimeError):
        model.complete(system="S", tools=TOOLS, messages=MESSAGES)


def test_scripted_turn_stop_reason_follows_content():
    assert scripted_turn([{"type": "tool_use", "id": "1", "name": "t", "input": {}}]).stop_reason == "tool_use"
