"""The agent LLM behind one interface.

Scenarios and the graph never call the API directly; they call
``AgentModel.complete``. ``AnthropicModel`` is the Claude API implementation;
``ScriptedModel`` replays fixed turns for tests and dry runs.

Request shape for ``claude-sonnet-5-5`` (checked against the anthropic 2.x SDK):

- Adaptive thinking is the model default, so ``thinking`` is omitted. Depth is
  set with ``output_config.effort``.
- No ``temperature``/``top_p``/``top_k``: the API rejects them.
- ``tool_choice`` is ``auto``: forced tool use is rejected on this model.
- Caching: an explicit breakpoint on the system block (tools render first, so
  it caches tools + system), plus top-level automatic caching for the growing
  conversation tail.
- History is append-only and assistant content is passed back unchanged,
  thinking blocks included; editing earlier turns invalidates them.
- No server-side refusal fallback: a fallback would answer with a different
  model mid-run, which would contaminate the experiment. A refusal comes back
  as ``stop_reason == "refusal"`` and the harness ends the run on it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

DEFAULT_MODEL = "claude-sonnet-5-5"


@dataclass(frozen=True)
class ModelSettings:
    model: str = DEFAULT_MODEL
    max_tokens: int = 16000  # per response; non-streaming, so kept under SDK timeouts
    effort: str | None = "medium"  # low | medium | high | xhigh | max; None = API default
    cache: bool = True
    max_retries: int = 4


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    requests: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.requests += other.requests

    @property
    def capped_tokens(self) -> int:
        """Tokens counted against the per-run backstop: new input, cache writes and output.

        Cache reads are excluded: every request re-reads the whole cached history,
        so counting them would trip the cap on ordinary multi-turn runs.
        """
        return self.input_tokens + self.cache_creation_input_tokens + self.output_tokens

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "requests": self.requests,
        }


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens."""

    input: float
    output: float
    cache_write_5m: float
    cache_read: float
    batch_discount: float = 0.5  # Message Batches bill at half the standard rates

    def cost(self, usage: Usage, *, batch: bool = False) -> float:
        usd = (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cache_creation_input_tokens * self.cache_write_5m
            + usage.cache_read_input_tokens * self.cache_read
        ) / 1_000_000
        return usd * (self.batch_discount if batch else 1.0)


# First-party Claude API rates.
PRICING = {
    "claude-sonnet-5-5": Pricing(input=2.00, output=10.00, cache_write_5m=2.50, cache_read=0.20),
}


@dataclass
class ModelTurn:
    """One model response, as plain dicts that can be appended back to history unchanged."""

    content: list[dict[str, Any]]
    stop_reason: str | None
    usage: Usage = field(default_factory=Usage)
    stop_details: dict[str, Any] | None = None
    model: str | None = None
    request_id: str | None = None

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        return "\n".join(b["text"] for b in self.content if b.get("type") == "text")


class AgentModel(Protocol):
    settings: ModelSettings

    def complete(
        self,
        *,
        system: str,
        tools: Sequence[dict[str, Any]],
        messages: Sequence[dict[str, Any]],
        tool_choice: dict[str, Any] | None = None,
    ) -> ModelTurn: ...


class AnthropicModel:
    """The Claude API, via the official SDK. Credentials come from the environment (.env)."""

    def __init__(self, settings: ModelSettings | None = None, client: Any = None) -> None:
        self.settings = settings or ModelSettings()
        if client is None:
            import anthropic

            client = anthropic.Anthropic(max_retries=self.settings.max_retries)
        self.client = client

    def build_request(
        self,
        *,
        system: str,
        tools: Sequence[dict[str, Any]],
        messages: Sequence[dict[str, Any]],
        tool_choice: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        s = self.settings
        system_block: dict[str, Any] = {"type": "text", "text": system}
        request: dict[str, Any] = {
            "model": s.model,
            "max_tokens": s.max_tokens,
            "system": [system_block],
            "messages": copy.deepcopy(list(messages)),
        }
        if tools:
            request["tools"] = copy.deepcopy(list(tools))
            request["tool_choice"] = tool_choice or {"type": "auto"}
        if s.effort is not None:
            request["output_config"] = {"effort": s.effort}
        if s.cache:
            system_block["cache_control"] = {"type": "ephemeral"}
            request["cache_control"] = {"type": "ephemeral"}
        return request

    def complete(self, *, system, tools, messages, tool_choice=None) -> ModelTurn:
        request = self.build_request(system=system, tools=tools, messages=messages, tool_choice=tool_choice)
        response = self.client.messages.create(**request)
        return turn_from_response(response)


def turn_from_response(response: Any) -> ModelTurn:
    u = response.usage
    usage = Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_input_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
        cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
        requests=1,
    )
    details = getattr(response, "stop_details", None)
    return ModelTurn(
        content=[block.to_dict(mode="json") for block in response.content],
        stop_reason=response.stop_reason,
        usage=usage,
        stop_details=details.to_dict(mode="json") if details is not None else None,
        model=getattr(response, "model", None),
        request_id=getattr(response, "_request_id", None),
    )


class ScriptedModel:
    """Replays model turns for tests and dry runs; records every request it receives.

    ``script`` is either a list of ModelTurn/dict content lists consumed in order,
    or a callable ``(system, tools, messages) -> ModelTurn``.
    """

    def __init__(
        self,
        script: Sequence[Any] | Callable[..., ModelTurn],
        settings: ModelSettings | None = None,
    ) -> None:
        self.settings = settings or ModelSettings()
        self._script = script if callable(script) else list(script)
        self.requests: list[dict[str, Any]] = []

    def complete(self, *, system, tools, messages, tool_choice=None) -> ModelTurn:
        self.requests.append(
            {
                "system": system,
                "tools": copy.deepcopy(list(tools)),
                "messages": copy.deepcopy(list(messages)),
                "tool_choice": tool_choice,
            }
        )
        if callable(self._script):
            return self._script(system=system, tools=tools, messages=messages)
        if not self._script:
            raise RuntimeError("ScriptedModel ran out of scripted turns")
        nxt = self._script.pop(0)
        return nxt if isinstance(nxt, ModelTurn) else scripted_turn(nxt)


def scripted_turn(content: list[dict[str, Any]], *, usage: Usage | None = None) -> ModelTurn:
    """A ModelTurn from content blocks; stop_reason follows from whether a tool is called."""
    stop = "tool_use" if any(b.get("type") == "tool_use" for b in content) else "end_turn"
    return ModelTurn(content=copy.deepcopy(content), stop_reason=stop, usage=usage or Usage(requests=1))


def make_model(settings: ModelSettings | None = None) -> AnthropicModel:
    """The live agent model, with credentials loaded from .env."""
    from dotenv import load_dotenv

    load_dotenv()
    return AnthropicModel(settings)
