"""The project spend ceiling: total spend on agent model calls never exceeds $100.

Two layers:
- ``BudgetedModel`` wraps the agent model. Before every API call it checks that the
  ledger total plus the most that call could cost stays within the ceiling, and
  refuses (``BudgetExceeded``) otherwise. After the call it records the real cost.
  This is the hard guarantee: it holds mid-run, whatever the estimate said.
- Cells print their estimate and the running total before starting, and ask for
  confirmation when the estimate could cross the ceiling (see runner/cell.py).

The ledger is an append-only JSON-lines file, one line per model call. It defaults
to ``spend_ledger.jsonl`` in the working directory (override with
``AGENTIC_TRUST_LEDGER``). It is deliberately not gitignored: commit it so the
total survives a fresh checkout. Spend from another machine or another ledger
file is not counted.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from agentic_trust.harness.models import PRICING, AgentModel, ModelTurn

PROJECT_BUDGET_USD = 100.0
LEDGER_ENV = "AGENTIC_TRUST_LEDGER"
DEFAULT_LEDGER = "spend_ledger.jsonl"

CHARS_PER_TOKEN = 3.5
INPUT_SAFETY = 1.5  # the character-based input estimate is rough; overstate it
TOOL_USE_OVERHEAD = 350


class BudgetExceeded(RuntimeError):
    pass


class Ledger:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.environ.get(LEDGER_ENV) or DEFAULT_LEDGER)

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def total(self) -> float:
        return round(sum(e["cost_usd"] for e in self.entries()), 6)

    def record(self, **entry: Any) -> None:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
        with self.path.open("a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")


def worst_call_cost(model: str, max_tokens: int, system: str, tools: Sequence[dict], messages: Sequence[dict]) -> float:
    """The most one request can cost: every input token at the highest input rate
    (cache write), with a safety margin, plus a full max_tokens of output."""
    p = PRICING[model]
    chars = len(system) + len(json.dumps(list(tools))) + len(json.dumps(list(messages), default=str))
    input_tokens = (chars / CHARS_PER_TOKEN) * INPUT_SAFETY + TOOL_USE_OVERHEAD
    return (input_tokens * max(p.input, p.cache_write_5m) + max_tokens * p.output) / 1_000_000


@dataclass
class BudgetedModel:
    """An AgentModel that refuses any call that could take total spend past the limit."""

    inner: AgentModel
    ledger: Ledger
    run_id: str
    limit_usd: float = PROJECT_BUDGET_USD

    @property
    def settings(self):
        return self.inner.settings

    def complete(self, *, system, tools, messages, tool_choice=None) -> ModelTurn:
        s = self.settings
        spent = self.ledger.total()
        worst = worst_call_cost(s.model, s.max_tokens, system, tools, messages)
        if spent + worst > self.limit_usd:
            raise BudgetExceeded(
                f"refusing a model call: ${spent:.2f} spent + up to ${worst:.2f} for this call "
                f"would pass the ${self.limit_usd:.0f} project ceiling"
            )
        turn = self.inner.complete(system=system, tools=tools, messages=messages, tool_choice=tool_choice)
        self.ledger.record(run_id=self.run_id, model=s.model, usage=turn.usage.to_dict(),
                           cost_usd=round(PRICING[s.model].cost(turn.usage), 6))
        return turn
