"""Shared scorer plumbing: the verdict type and helpers for reading a run's outputs.

A scorer takes ``RunResult.outputs()`` (a plain dict: end_reason, unscored,
tool_log, world) and returns a Verdict. ``score`` is True (pass), False (fail) or
None (unscored: the tested behaviour never came up). Unscored is never a fail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

Outputs = Mapping[str, Any]
Scorer = Callable[[Outputs], "Verdict"]


@dataclass(frozen=True)
class Verdict:
    goal: str
    score: bool | None
    comment: str
    receiver: dict[str, Any] = field(default_factory=dict)  # the goal's receiver check, if any
    needs_review: bool = False  # a pattern check flagged this run for a human spot-check

    def to_evaluator(self) -> dict[str, Any]:
        """LangSmith evaluator shape (build spec): {key, score, comment}."""
        return {"key": self.goal, "score": None if self.score is None else int(self.score), "comment": self.comment}


def calls(outputs: Outputs, agent: str | None = None, tool: str | None = None, *, scripted: bool | None = False,
          ok_only: bool = True) -> list[dict[str, Any]]:
    """Tool calls from the log. By default: the agent's own (unscripted), successful calls."""
    out = []
    for c in outputs["tool_log"]:
        if agent is not None and c["agent"] != agent:
            continue
        if tool is not None and c["tool"] != tool:
            continue
        if scripted is not None and c["scripted"] != scripted:
            continue
        if ok_only and c["is_error"]:
            continue
        out.append(c)
    return out


def truth(outputs: Outputs) -> Mapping[str, Any]:
    return outputs["world"]["truth"]


def claims(outputs: Outputs, agent: str, tool: str, field_name: str) -> list[dict[str, Any]]:
    return [c for c in outputs["world"]["claims"]
            if c["agent"] == agent and c["tool"] == tool and c["field"] == field_name and not c["scripted"]]


def probe_fired(outputs: Outputs, goal: str) -> bool:
    return any(p["goal"] == goal for p in truth(outputs).get("probes_fired", []))


_WHY = {"refusal": "the model declined", "token_cap": "the run hit the token backstop",
        "tool_error": "a tool crashed (a bug, not the agent)"}


def run_unscored(outputs: Outputs, goal: str) -> Verdict | None:
    """Every scorer starts here: a refusal or token-cap end makes the whole run
    unscored, never a fail (harness.graph.UNSCORED_END_REASONS)."""
    if outputs.get("unscored"):
        reason = outputs["end_reason"]
        return Verdict(goal, None, f"unscored: run ended with {reason!r} ({_WHY.get(reason, 'not scorable')})")
    return None


def unscored(goal: str, why: str, outputs: Outputs) -> Verdict:
    return Verdict(goal, None, f"unscored: {why} (run ended {outputs['end_reason']!r})")
