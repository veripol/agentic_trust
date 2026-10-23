"""Local smoke test: SEC-DEC-1 at P1 for 2 runs, traced to LangSmith, within the budget.

Run on your own machine, with ANTHROPIC_API_KEY and LANGSMITH_API_KEY in .env:

    python -m agentic_trust.runner.smoke_sec01

It is a 2-run cell (runner/cell.py) that always asks before spending. It prints
the estimate and the project spend so far, then for each run: end reason,
rounds, day, tokens, actual cost and the SEC-DEC-1 verdict, then trace links.
Outputs go to runs/smoke/ (gitignored); every call is recorded in the spend ledger.
"""

from __future__ import annotations

import sys

from agentic_trust.runner import cell

SMOKE_REPEATS = 2


def main(argv: list[str] | None = None) -> int:
    return cell.main(["SEC-DEC-1", "P1", "--repeats", str(SMOKE_REPEATS), *(argv or [])],
                     always_confirm=True, label="smoke")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
