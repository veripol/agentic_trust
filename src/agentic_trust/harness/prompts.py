"""Prompt assembly: insert the P0/P1/P2 blocks, then fill placeholders.

Follows the build spec's ``build_prompt``: blocks first, then placeholders, so
placeholders inside principle blocks (e.g. ``{DISCLOSURE_DEADLINE_DAYS}``) are
filled too. Two deliberate differences from the spec's sketch, both stricter:

- Placeholders are ``{UPPER_CASE}`` names replaced by pattern, not
  ``str.format``, so literal braces in prompt text are safe.
- A placeholder left unfilled, a missing slot, or a value that is not a plain
  scalar raises. Values come from ``config.values`` only; ground truth
  (``World``) is never an input here and is rejected if passed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .state import AgentAssignment, RunConfig

LEVELS = ("P0", "P1", "P2")
SLOTS = ("{PRESSURE}", "{PRINCIPLE}")
_PLACEHOLDER = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
_SCALARS = (str, int, float)


class PromptError(ValueError):
    pass


@dataclass(frozen=True)
class GoalBlocks:
    """A goal's two prompt blocks."""

    pressure: str
    principle: str


def build_prompt(base: str, blocks: GoalBlocks | None, level: str, values: Mapping[str, Any]) -> str:
    """Build one agent's system prompt.

    ``level`` is P0, P1 or P2. P0 leaves both slots empty, P1 fills the pressure
    slot, P2 fills both. ``blocks=None`` means the agent is not under test and
    gets its base prompt with both slots empty at any level.
    """
    if level not in LEVELS:
        raise PromptError(f"level must be one of {LEVELS}, not {level!r}")
    for slot in SLOTS:
        if base.count(slot) != 1:
            raise PromptError(f"base prompt must contain {slot} exactly once")

    pressure = blocks.pressure if blocks and level in ("P1", "P2") else ""
    principle = blocks.principle if blocks and level == "P2" else ""
    prompt = base.replace("{PRESSURE}", pressure).replace("{PRINCIPLE}", principle)
    prompt = fill_placeholders(prompt, values)
    # Empty slots leave runs of blank lines; collapse them so P0 reads cleanly.
    return re.sub(r"\n{3,}", "\n\n", prompt).strip() + "\n"


def fill_placeholders(text: str, values: Mapping[str, Any]) -> str:
    for name, value in values.items():
        if not isinstance(value, _SCALARS) or isinstance(value, bool):
            raise PromptError(
                f"placeholder {name} must be a str, int or float, not {type(value).__name__}"
            )

    def sub(match: re.Match) -> str:
        name = match.group(1)
        if name not in values:
            raise PromptError(f"no value for placeholder {{{name}}}")
        return str(values[name])

    return _PLACEHOLDER.sub(sub, text)


def build_agent_prompts(
    config: RunConfig,
    base_prompts: Mapping[str, str],
    goal_blocks: Mapping[str, Mapping[str, GoalBlocks]],
) -> dict[str, str]:
    """Build every agent's system prompt for a run.

    ``goal_blocks`` maps agent -> goal ID -> GoalBlocks. Only agents with an
    assignment in ``config`` get blocks; everyone else runs their base prompt
    with both slots empty (build spec rule 1). A 2x2 cell simply assigns two agents.
    """
    prompts = {}
    for agent in config.agents:
        if agent not in base_prompts:
            raise PromptError(f"no base prompt for agent {agent!r}")
        assignment: AgentAssignment = config.assignment(agent)
        blocks = None
        if assignment.goal is not None:
            try:
                blocks = goal_blocks[agent][assignment.goal]
            except KeyError:
                raise PromptError(f"no blocks for goal {assignment.goal} on agent {agent!r}") from None
        prompts[agent] = build_prompt(base_prompts[agent], blocks, assignment.level, config.values)
    return prompts


def prompt_name(use_case: str, agent: str, goal: str | None, level: str) -> str:
    """LangSmith prompt name, e.g. ``sec01-vendor/SEC-LD-3/P1`` (build spec)."""
    return f"{use_case}-{agent}/{goal or 'base'}/{level}"
