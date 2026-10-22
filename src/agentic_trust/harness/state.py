"""The three state objects of a run: world, wire and config.

- ``World`` holds ground truth and every logged Claim. Only scorers read it.
  It refuses to be turned into text, so it cannot be interpolated into a prompt.
- ``Wire`` holds the messages agents actually exchange, each addressed to the
  agents allowed to see it.
- ``RunConfig`` holds the knobs, probes, seeds and placeholder values. It is
  frozen at run start.

``Clock`` is the simulation day counter (docs/clock.md): it starts at 0, each
completed round advances it by ``days_per_round``, and a round can be paused
(an RFI pauses the clock for its round).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

# --------------------------------------------------------------------------- world


class WorldLeakError(RuntimeError):
    """Raised when code tries to render ground truth as text."""


class World:
    """Ground truth plus the Claim log. Scorer-only; never rendered into a prompt.

    Behaves like a mutable mapping for tools and scorers. Any attempt to turn the
    whole object into a string (``str``, ``repr``, ``format``, f-strings, JSON)
    raises ``WorldLeakError``, so an accidental ``f"{world}"`` in prompt code
    fails loudly instead of leaking. Use ``snapshot()`` to export it for scoring.
    """

    __slots__ = ("_data", "claims")

    def __init__(self, truth: Mapping[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = copy.deepcopy(dict(truth or {}))
        self.claims: list[Any] = []  # Claim objects; see harness.claim

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def setdefault(self, key: str, default: Any) -> Any:
        return self._data.setdefault(key, default)

    def keys(self):
        return self._data.keys()

    def snapshot(self) -> dict[str, Any]:
        """A deep copy of ground truth and claims, for scorers and run outputs."""
        return {
            "truth": copy.deepcopy(self._data),
            "claims": [c.to_dict() if hasattr(c, "to_dict") else c for c in self.claims],
        }

    def _leak(self, *_: Any) -> str:
        raise WorldLeakError("world holds ground truth and must never be rendered as text")

    __str__ = _leak
    __repr__ = _leak
    __format__ = _leak

    def __reduce__(self):
        # Blocks pickling/copy shortcuts that could carry it into a serialised prompt.
        raise WorldLeakError("world cannot be serialised; use World.snapshot()")

    def __deepcopy__(self, memo: dict) -> "World":
        clone = World(self._data)
        clone.claims = copy.deepcopy(self.claims, memo)
        return clone


# --------------------------------------------------------------------------- wire


@dataclass(frozen=True)
class WireMessage:
    """One message on the wire.

    ``scripted`` marks harness-injected probes. It is kept for the trace and for
    scorers, and is never part of what the recipient sees (see ``render``).
    """

    seq: int
    round: int
    day: int
    sender: str
    recipients: tuple[str, ...]
    kind: str  # "message", or the name of the tool whose call produced it
    content: Any  # JSON-serialisable; internal fields already removed
    scripted: bool = False

    def render(self) -> str:
        """What the recipient reads. Identical for real and scripted messages."""
        if isinstance(self.content, str):
            body = self.content
        else:
            body = json.dumps(self.content, sort_keys=True, ensure_ascii=False)
        if self.kind == "result":  # a tool result that arrives later (e.g. a delayed assessment)
            return f"[Day {self.day}] Result from {self.sender}:\n{body}"
        via = "" if self.kind == "message" else f" via {self.kind}"
        return f"[Day {self.day}] From {self.sender}{via}:\n{body}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "round": self.round,
            "day": self.day,
            "sender": self.sender,
            "recipients": list(self.recipients),
            "kind": self.kind,
            "content": copy.deepcopy(self.content),
            "scripted": self.scripted,
        }


class Wire:
    """Everything agents exchange. Each agent reads only messages addressed to it."""

    def __init__(self) -> None:
        self.messages: list[WireMessage] = []
        self._cursor: dict[str, int] = {}  # agent -> last seq it has read

    def post(
        self,
        *,
        sender: str,
        recipients: str | tuple[str, ...] | list[str],
        content: Any,
        kind: str = "message",
        round: int,
        day: int,
        scripted: bool = False,
    ) -> WireMessage:
        if isinstance(recipients, str):
            recipients = (recipients,)
        _ensure_wire_safe(content)
        msg = WireMessage(
            seq=len(self.messages) + 1,
            round=round,
            day=day,
            sender=sender,
            recipients=tuple(recipients),
            kind=kind,
            content=copy.deepcopy(content),
            scripted=scripted,
        )
        self.messages.append(msg)
        return msg

    def unread(self, agent: str) -> list[WireMessage]:
        """Messages addressed to ``agent`` that it has not read yet. Does not mark them read."""
        since = self._cursor.get(agent, 0)
        return [m for m in self.messages if m.seq > since and agent in m.recipients]

    def mark_read(self, agent: str, upto_seq: int) -> None:
        self._cursor[agent] = max(self._cursor.get(agent, 0), upto_seq)

    def visible_to(self, agent: str) -> list[WireMessage]:
        return [m for m in self.messages if agent in m.recipients]


def _ensure_wire_safe(content: Any) -> None:
    """Wire content must be plain JSON data; ground truth objects can't ride along."""
    try:
        json.dumps(content)
    except (TypeError, ValueError, WorldLeakError) as exc:
        raise TypeError(f"wire content must be JSON-serialisable: {exc}") from exc


# --------------------------------------------------------------------------- config


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def thaw(value: Any) -> Any:
    """Turn a frozen config value back into plain dicts and lists."""
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class AgentAssignment:
    """Which goal's blocks an agent gets, at which level. ``goal=None`` = base prompt only."""

    goal: str | None = None
    level: str = "P0"

    def __post_init__(self) -> None:
        if self.level not in ("P0", "P1", "P2"):
            raise ValueError(f"level must be P0, P1 or P2, not {self.level!r}")


@dataclass(frozen=True)
class RunConfig:
    """Everything that defines one run. Frozen at run start.

    ``assignments`` maps agent -> AgentAssignment. Agents not listed run their
    base prompt with both slots empty (build spec, "Rules for building a run").
    """

    run_id: str
    use_case: str
    agents: tuple[str, ...]
    assignments: Mapping[str, AgentAssignment] = field(default_factory=dict)
    arm: str = "main"
    repeat: int = 0
    values: Mapping[str, Any] = field(default_factory=dict)  # prompt placeholder values
    knobs: Mapping[str, Any] = field(default_factory=dict)  # scenario config keys
    probes: tuple[Any, ...] = ()  # harness.graph.Probe objects
    initial_messages: Mapping[str, str] = field(default_factory=dict)  # agent -> kickoff text
    days_per_round: int = 1
    max_rounds: int = 12
    max_steps_per_turn: int = 8
    max_run_tokens: int | None = None  # input+output tokens across the run; None = no cap
    recursion_limit: int = 25  # LangGraph supersteps = agent turns
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "agents", tuple(self.agents))
        object.__setattr__(self, "assignments", _freeze(dict(self.assignments)))
        object.__setattr__(self, "values", _freeze(dict(self.values)))
        object.__setattr__(self, "knobs", _freeze(dict(self.knobs)))
        object.__setattr__(self, "probes", tuple(self.probes))
        object.__setattr__(self, "initial_messages", _freeze(dict(self.initial_messages)))
        object.__setattr__(self, "tags", tuple(self.tags))
        unknown = set(self.assignments) - set(self.agents)
        if unknown:
            raise ValueError(f"assignments name agents not in this run: {sorted(unknown)}")
        if self.days_per_round < 0 or self.max_rounds < 1 or self.max_steps_per_turn < 1:
            raise ValueError("days_per_round >= 0, max_rounds >= 1 and max_steps_per_turn >= 1")

    def assignment(self, agent: str) -> AgentAssignment:
        return self.assignments.get(agent, AgentAssignment())


# --------------------------------------------------------------------------- clock


class Clock:
    """Simulation day counter (docs/clock.md).

    The day starts at 0. Each completed round advances it by ``days_per_round``,
    unless that round was paused. Duration-based tool calls resolve against it:
    ``due_day(n)`` is n days from now and ``is_due(d)`` says whether day d has come.
    """

    def __init__(self, days_per_round: int) -> None:
        self.days_per_round = days_per_round
        self.day = 0
        self._paused_this_round = False

    def pause_this_round(self) -> None:
        self._paused_this_round = True

    @property
    def paused(self) -> bool:
        return self._paused_this_round

    def end_round(self) -> int:
        """Called by the harness when a round completes. Returns the new day."""
        if not self._paused_this_round:
            self.day += self.days_per_round
        self._paused_this_round = False
        return self.day

    def due_day(self, after_days: int) -> int:
        return self.day + after_days

    def is_due(self, day: int) -> bool:
        return self.day >= day


# --------------------------------------------------------------------------- run state


@dataclass
class ToolCallRecord:
    """One tool call as the harness saw it, with full arguments (including internal fields)."""

    seq: int
    round: int
    day: int
    agent: str
    tool: str
    args: dict[str, Any]
    result: Any
    is_error: bool = False
    scripted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "round": self.round,
            "day": self.day,
            "agent": self.agent,
            "tool": self.tool,
            "args": copy.deepcopy(self.args),
            "result": copy.deepcopy(self.result),
            "is_error": self.is_error,
            "scripted": self.scripted,
        }


@dataclass
class RunState:
    """All mutable state for one run. Agents only ever see what reaches them via the wire."""

    config: RunConfig
    world: World
    wire: Wire = field(default_factory=Wire)
    clock: Clock | None = None
    round: int = 0
    tool_log: list[ToolCallRecord] = field(default_factory=list)
    ids: dict[str, str] = field(default_factory=dict)  # shared run ids, e.g. ticket_id
    turns: dict[str, int] = field(default_factory=dict)  # agent -> turns taken
    ended: bool = False
    end_reason: str | None = None

    def __post_init__(self) -> None:
        if self.clock is None:
            self.clock = Clock(self.config.days_per_round)

    @property
    def day(self) -> int:
        return self.clock.day

    def end(self, reason: str) -> None:
        if not self.ended:
            self.ended = True
            self.end_reason = reason

    def post(self, *, sender: str, recipients, content: Any, kind: str = "message", scripted: bool = False) -> WireMessage:
        return self.wire.post(
            sender=sender,
            recipients=recipients,
            content=content,
            kind=kind,
            round=self.round,
            day=self.day,
            scripted=scripted,
        )
