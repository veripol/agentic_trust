"""The Claim logger and the tool wrapper.

Every agent tool is a ``ToolSpec``. The harness calls it through
``execute_tool``, which:

1. validates the arguments against the tool's JSON schema (an invalid call
   returns an error result to the agent and is logged, but the handler never runs);
2. runs the handler with a ``ToolContext``;
3. writes a ``Claim`` into ``world`` for every asserting field the call set,
   pairing what the agent claimed with the ground truth at that moment;
4. records the full call, internal fields included, in the run's tool log.

Internal fields (build spec: "recorded for scoring but removed before the call
reaches any other agent") are stripped from ``ctx.public_args``, which is what
handlers should forward to other agents.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import jsonschema

from .state import RunState, ToolCallRecord, World

# truth(world, args, result) -> the ground truth this claim is compared against
TruthFn = Callable[[World, Mapping[str, Any], Any], Any]


@dataclass(frozen=True)
class Claim:
    """One assertion an agent made through a tool, next to the truth at that moment."""

    seq: int
    round: int
    day: int
    agent: str
    tool: str
    field: str
    claimed: Any
    truth: Any
    scripted: bool = False

    @property
    def matches(self) -> bool:
        return self.claimed == self.truth

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "round": self.round,
            "day": self.day,
            "agent": self.agent,
            "tool": self.tool,
            "field": self.field,
            "claimed": copy.deepcopy(self.claimed),
            "truth": copy.deepcopy(self.truth),
            "scripted": self.scripted,
        }


class ClaimLogger:
    """Writes Claims into ``world.claims``. Tools normally log through ``ToolSpec.claims``."""

    def __init__(self, world: World) -> None:
        self.world = world

    def log(
        self,
        *,
        state: RunState,
        agent: str,
        tool: str,
        field: str,
        claimed: Any,
        truth: Any,
        scripted: bool = False,
    ) -> Claim:
        claim = Claim(
            seq=len(self.world.claims) + 1,
            round=state.round,
            day=state.day,
            agent=agent,
            tool=tool,
            field=field,
            claimed=copy.deepcopy(claimed),
            truth=copy.deepcopy(truth),
            scripted=scripted,
        )
        self.world.claims.append(claim)
        return claim


@dataclass
class ToolContext:
    """What a tool handler gets to work with.

    Handlers may read and write ``world`` (ground truth), read ``config``, use
    ``clock``, and send messages with ``post``. Forward ``public_args`` to other
    agents, never ``args``, so internal fields stay internal.
    """

    state: RunState
    agent: str
    tool: str
    args: dict[str, Any]
    public_args: dict[str, Any]
    scripted: bool = False

    @property
    def world(self) -> World:
        return self.state.world

    @property
    def config(self):
        return self.state.config

    @property
    def clock(self):
        return self.state.clock

    def post(self, recipients, content: Any = None, *, kind: str | None = None) -> None:
        """Send a message on the wire from this agent. Defaults to this call's public args."""
        self.state.post(
            sender=self.agent,
            recipients=recipients,
            content=self.public_args if content is None else content,
            kind=self.tool if kind is None else kind,
            scripted=self.scripted,
        )

    def end_run(self, reason: str) -> None:
        self.state.end(reason)


Handler = Callable[..., Any]  # handler(ctx: ToolContext, **args) -> JSON-serialisable result


@dataclass(frozen=True)
class ToolSpec:
    """One agent tool.

    ``claims`` maps an asserting argument name to the function that returns its
    ground truth; the wrapper logs a Claim whenever the call sets that argument.
    ``internal_fields`` are kept in the log and the Claim log but never forwarded.
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Handler
    internal_fields: tuple[str, ...] = ()
    claims: Mapping[str, TruthFn] = field(default_factory=dict)

    def __post_init__(self) -> None:
        jsonschema.Draft202012Validator.check_schema(dict(self.input_schema))
        props = set(self.input_schema.get("properties", {}))
        missing = (set(self.internal_fields) | set(self.claims)) - props
        if missing:
            raise ValueError(f"{self.name}: fields not in input_schema: {sorted(missing)}")

    def definition(self) -> dict[str, Any]:
        """The tool definition sent to the model (Messages API shape)."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": copy.deepcopy(dict(self.input_schema)),
        }


@dataclass(frozen=True)
class ToolOutcome:
    result: Any
    is_error: bool


def execute_tool(
    state: RunState,
    agent: str,
    spec: ToolSpec,
    args: Mapping[str, Any],
    *,
    scripted: bool = False,
) -> ToolOutcome:
    """Run one tool call through the wrapper. See the module docstring."""
    args = copy.deepcopy(dict(args))
    validator = jsonschema.Draft202012Validator(dict(spec.input_schema))
    errors = sorted(validator.iter_errors(args), key=lambda e: list(e.path))
    if errors:
        message = "; ".join(_describe(e) for e in errors)
        outcome = ToolOutcome({"error": f"invalid arguments: {message}"}, True)
        _record(state, agent, spec.name, args, outcome, scripted)
        return outcome

    public_args = {k: v for k, v in args.items() if k not in spec.internal_fields}
    ctx = ToolContext(state, agent, spec.name, args, public_args, scripted)
    try:
        result = spec.handler(ctx, **args)
        outcome = ToolOutcome(result, False)
    except ToolError as exc:
        outcome = ToolOutcome({"error": str(exc)}, True)

    if not outcome.is_error:
        logger = ClaimLogger(state.world)
        for field_name, truth_fn in spec.claims.items():
            if field_name in args:
                logger.log(
                    state=state,
                    agent=agent,
                    tool=spec.name,
                    field=field_name,
                    claimed=args[field_name],
                    truth=truth_fn(state.world, args, result),
                    scripted=scripted,
                )
    _record(state, agent, spec.name, args, outcome, scripted)
    return outcome


class ToolError(Exception):
    """Raise from a handler to return an error result to the agent (e.g. unknown ticket id)."""


def _record(state: RunState, agent: str, tool: str, args: dict, outcome: ToolOutcome, scripted: bool) -> None:
    state.tool_log.append(
        ToolCallRecord(
            seq=len(state.tool_log) + 1,
            round=state.round,
            day=state.day,
            agent=agent,
            tool=tool,
            args=args,
            result=copy.deepcopy(outcome.result),
            is_error=outcome.is_error,
            scripted=scripted,
        )
    )


def _describe(error: jsonschema.ValidationError) -> str:
    where = "/".join(str(p) for p in error.path)
    return f"{where}: {error.message}" if where else error.message
