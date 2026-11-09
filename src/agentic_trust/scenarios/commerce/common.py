"""Shared pieces of the commerce scenarios (COMMERCE-01, COMMERCE-02).

``ProbeAwareRouter`` builds in the turn-order lesson from HEALTH-01 (HLT-NA-1),
HEALTH-02 (HLT-NA-2R) and SEC-03 (lab log 2026-11-04/05): a scripted probe that lands in
an agent's inbox during that agent's own turn can only be read on its next turn, and
plain round robin would hand the turn to someone else first, who could end the matter
before the probed agent answers. Here, after an agent's turn, if a scripted message
addressed to it is still unread, it takes one more turn in the same round before anyone
else acts. Reading the message clears the condition, so this happens at most once per
probe. A probe aimed at another agent changes nothing: that agent's turn comes as usual.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentic_trust.harness.graph import RouteDecision
from agentic_trust.harness.state import RunState


@dataclass(frozen=True)
class ProbeAwareRouter:
    """Round robin over the agents in this run (``config.agents`` order), plus one extra
    turn for an agent whose own turn delivered it an unread scripted message."""

    def first(self, state: RunState) -> str:
        return state.config.agents[0]

    def next(self, state: RunState, just_ran: str) -> RouteDecision:
        if not state.ended and any(m.scripted for m in state.wire.unread(just_ran)):
            return RouteDecision(just_ran, round_complete=False)
        order = state.config.agents
        i = order.index(just_ran)
        return RouteDecision(order[(i + 1) % len(order)], round_complete=i == len(order) - 1)


def turn_windows(outputs: Any, agent: str) -> list[int] | None:
    """Tool-log positions at which each of ``agent``'s turns started (``after_seq``), from
    ``world.turns_taken``; None for run files saved before turns were recorded."""
    turns = outputs["world"]["truth"].get("turns_taken")
    if turns is None:
        return None
    return [t["after_seq"] for t in turns if t["agent"] == agent]


def calls_in_first_turns(outputs: Any, agent: str, n: int) -> list[dict]:
    """The agent's own successful calls made during its first ``n`` turns."""
    mine = [c for c in outputs["tool_log"] if c["agent"] == agent and not c["scripted"] and not c["is_error"]]
    starts = turn_windows(outputs, agent)
    if starts is None or len(starts) <= n:
        return mine
    limit = starts[n]  # the tool-log length when turn n+1 started
    return [c for c in mine if c["seq"] <= limit]


def turn_after(outputs: Any, agent: str, seq: int) -> bool:
    """Did ``agent`` take a turn that started after tool-log position ``seq``? Falls back to
    'made a tool call after it' for run files without turn records."""
    starts = turn_windows(outputs, agent)
    if starts is None:
        return any(c["agent"] == agent and c["seq"] > seq for c in outputs["tool_log"])
    return any(s >= seq for s in starts)
