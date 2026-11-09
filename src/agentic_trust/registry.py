"""Every use case by name: the cell runner, the sweep and the report look scenarios up here.

A use case is four modules plus its cost profiles:

- ``config``: USE_CASE, AGENTS, GOALS (goal -> tested agent label), ARMS, PROBES,
  SHARED_P0_GOALS, SCENARIO and make_config(goal, level, arm, repeat, *, tags, run_id);
- ``prompts``: build_prompts(config);
- ``scorers``: SCORERS (goal -> scorer);
- ``report``: DISPLAY_NAME, GOAL_LABELS, END_TEXT and call_sentence(tool call), plus
  GOALS, SCORERS and SHARED_P0_GOALS re-exported for the digest.

Attributes are read from the modules at call time, so a new use case is a new entry
here and a new scenario folder; nothing else changes, and no use case's runs or
results touch another's (runs/sweep/<name>/, docs/results/<name>.md).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import ModuleType

from agentic_trust.runner.estimate import Profile


@dataclass(frozen=True)
class UseCase:
    name: str
    package: str  # e.g. "agentic_trust.scenarios.security.sec01"
    report_module: str  # e.g. "agentic_trust.report.sec01"
    first_agent: str  # the agent that gets the kickoff (for the cost estimate)
    profiles: tuple[Profile, ...]

    def _mod(self, name: str) -> ModuleType:
        return importlib.import_module(name)

    @property
    def config(self) -> ModuleType:
        return self._mod(f"{self.package}.config")

    @property
    def prompts(self) -> ModuleType:
        return self._mod(f"{self.package}.prompts")

    @property
    def scorers(self) -> ModuleType:
        return self._mod(f"{self.package}.scorers")

    @property
    def report(self) -> ModuleType:
        return self._mod(self.report_module)


def _profiles(calls_short: dict, calls_typical: dict, calls_long: dict, long_rounds: int) -> tuple[Profile, ...]:
    return (
        Profile("short", rounds=3, calls_per_turn=calls_short, output_per_call=300),
        Profile("typical", rounds=6, calls_per_turn=calls_typical, output_per_call=600),
        Profile("long", rounds=long_rounds, calls_per_turn=calls_long, output_per_call=900),
    )


USE_CASES: dict[str, UseCase] = {
    "sec01": UseCase(
        "sec01", "agentic_trust.scenarios.security.sec01", "agentic_trust.report.sec01", "researcher",
        _profiles({"researcher": 3, "vendor": 3}, {"researcher": 3, "vendor": 4},
                  {"researcher": 4, "vendor": 5}, long_rounds=10),
    ),
    "sec03": UseCase(
        "sec03", "agentic_trust.scenarios.security.sec03", "agentic_trust.report.sec03", "org_a",
        _profiles({"org_a": 2, "isac": 2, "org_b": 2}, {"org_a": 3, "isac": 3, "org_b": 3},
                  {"org_a": 4, "isac": 4, "org_b": 4}, long_rounds=8),
    ),
    "health01": UseCase(
        "health01", "agentic_trust.scenarios.health.health01", "agentic_trust.report.health01", "provider",
        _profiles({"provider": 3, "payer": 3}, {"provider": 3, "payer": 4},
                  {"provider": 4, "payer": 5}, long_rounds=6),
    ),
    "health02": UseCase(
        "health02", "agentic_trust.scenarios.health.health02", "agentic_trust.report.health02", "hospital",
        _profiles({"hospital": 3, "snf_a": 2, "snf_b": 2, "payer": 2},
                  {"hospital": 4, "snf_a": 3, "snf_b": 3, "payer": 3},
                  {"hospital": 5, "snf_a": 4, "snf_b": 4, "payer": 4}, long_rounds=6),
    ),
    "commerce01": UseCase(
        "commerce01", "agentic_trust.scenarios.commerce.commerce01", "agentic_trust.report.commerce01",
        "post_purchase",
        _profiles({"post_purchase": 3, "returns": 2}, {"post_purchase": 4, "returns": 3},
                  {"post_purchase": 5, "returns": 4}, long_rounds=8),
    ),
    "commerce02": UseCase(
        "commerce02", "agentic_trust.scenarios.commerce.commerce02", "agentic_trust.report.commerce02", "criteria",
        _profiles({"criteria": 2, "price": 2, "purchase": 2, "store": 3},
                  {"criteria": 2, "price": 2, "purchase": 3, "store": 4},
                  {"criteria": 3, "price": 3, "purchase": 4, "store": 5}, long_rounds=6),
    ),
}


def get(name: str) -> UseCase:
    try:
        return USE_CASES[name]
    except KeyError:
        raise KeyError(f"unknown use case {name!r}; known: {', '.join(USE_CASES)}") from None


def available() -> dict[str, UseCase]:
    """Use cases whose scenario modules exist."""
    out = {}
    for name, uc in USE_CASES.items():
        try:
            uc.config
        except ModuleNotFoundError:
            continue
        out[name] = uc
    return out


def use_case_of(goal: str) -> UseCase:
    """The use case a goal ID belongs to (goal IDs are unique across use cases)."""
    for uc in USE_CASES.values():
        try:
            if goal in uc.config.GOALS:
                return uc
        except ModuleNotFoundError:
            continue
    raise KeyError(f"no use case has goal {goal!r}")
