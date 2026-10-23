"""Pre-run cost estimates from the real prompts and tool definitions. No API calls.

Token counts are estimated from characters (no model or token-counting call is
made). A run is simulated request by request with the harness's caching: the
fixed prefix (tool definitions + system prompt) and the history are written to
the cache once and read back on later requests; each request writes only what
was appended since the previous one.

Assumptions (all overridable):
- ``CHARS_PER_TOKEN`` = 3.5 for English prose and JSON schemas (rough; real
  counts may differ by ~15%).
- ``TOOL_USE_OVERHEAD`` = 350 tokens: the tool-use system text the API adds
  when tools are present.
- Thinking from earlier turns is not billed again as input; the thinking a call
  produces is billed as output (it counts toward ``output_per_call``).
- Every agent acts every round (no skipped turns), which overstates short runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Sequence

from agentic_trust.harness.models import PRICING, Usage

CHARS_PER_TOKEN = 3.5
TOOL_USE_OVERHEAD = 350


@dataclass(frozen=True)
class Profile:
    """How a run unfolds, for the estimate."""

    name: str
    rounds: int
    calls_per_turn: Mapping[str, int]  # agent -> model calls per turn
    output_per_call: int  # output tokens per call, thinking included
    history_per_call: int = 180  # tool_use block + tool result appended per call
    inbox_per_turn: int = 150  # wire messages read at the start of a turn
    kickoff: int = 60  # the first user message (researcher only)


def tokens(text: str) -> int:
    return round(len(text) / CHARS_PER_TOKEN)


def prefix_tokens(system: str, tool_defs: Sequence[dict]) -> int:
    return tokens(system) + tokens(json.dumps(list(tool_defs))) + (TOOL_USE_OVERHEAD if tool_defs else 0)


def simulate_run(prefixes: Mapping[str, int], profile: Profile, first_agent: str,
                 max_run_tokens: int | None = None) -> Usage:
    """Usage for one run. With ``max_run_tokens``, stops where the harness's token
    backstop would end the run (checked after each model call, as the harness does)."""
    usage = Usage()
    history = {a: 0 for a in prefixes}
    last_input = {a: 0 for a in prefixes}  # what the previous request cached
    for _ in range(profile.rounds):
        for agent in prefixes:
            history[agent] += profile.inbox_per_turn
            if agent == first_agent and last_input[agent] == 0:
                history[agent] += profile.kickoff
            for _ in range(profile.calls_per_turn[agent]):
                total_in = prefixes[agent] + history[agent]
                usage.add(Usage(
                    output_tokens=profile.output_per_call,
                    cache_read_input_tokens=last_input[agent],
                    cache_creation_input_tokens=total_in - last_input[agent],
                    requests=1,
                ))
                last_input[agent] = total_in
                history[agent] += profile.history_per_call
                if max_run_tokens is not None and usage.capped_tokens >= max_run_tokens:
                    return usage
    return usage


def estimate(
    prompts: Mapping[str, str],
    tool_defs: Mapping[str, Sequence[dict]],
    profiles: Sequence[Profile],
    *,
    runs: int,
    first_agent: str,
    model: str = "claude-sonnet-5-5",
    max_run_tokens: int | None = None,
) -> dict:
    pricing = PRICING[model]
    prefixes = {a: prefix_tokens(prompts[a], tool_defs[a]) for a in prompts}
    out = {"prefix_tokens": prefixes, "runs": runs, "profiles": {}}
    for p in profiles:
        full = simulate_run(prefixes, p, first_agent)
        u = simulate_run(prefixes, p, first_agent, max_run_tokens)
        per_run = pricing.cost(u)
        out["profiles"][p.name] = {
            "hits_token_cap": u.requests < full.requests,
            "capped_tokens_uncapped_run": full.capped_tokens,
            "requests_per_run": u.requests,
            "usage_per_run": u.to_dict(),
            "capped_tokens_per_run": u.capped_tokens,
            "cost_per_run_usd": round(per_run, 4),
            "cost_cell_usd": round(per_run * runs, 2),
        }
    return out


def ceiling_per_run(prefix: int, *, max_run_tokens: int, max_tokens: int, max_requests: int,
                    model: str = "claude-sonnet-5-5") -> float:
    """Hard upper bound for one run from the caps alone: capped tokens stop at
    max_run_tokens (plus one response's overshoot, all priced as output), and each of
    at most max_requests requests reads at most the whole capped context from cache."""
    p = PRICING[model]
    capped = max_run_tokens + max_tokens
    return (capped * p.output + max_requests * (prefix + capped) * p.cache_read) / 1_000_000
