"""Run the whole SEC-01 sweep: every goal at P0, P1 and P2, every arm, then the digest.

Run on your own machine, with ANTHROPIC_API_KEY and LANGSMITH_API_KEY in .env:

    python -m agentic_trust.runner.sweep --plan     # the plan and its cost estimate; no keys, no spend
    python -m agentic_trust.runner.sweep            # run it (asks once), then write the digest

Cells (build spec rules 1-2):
- one shared P0 cell, both agents on their base prompts, scored by the digest for
  every goal without a probe or arm change (config.SHARED_P0_GOALS);
- P0 cells of their own for probe goals (SEC-BA-3, SEC-BA-4) and for control arms;
- P1 and P2 cells for every goal and arm.
Each cell targets ``--repeats`` completed runs (default 10); refused runs are re-run,
up to 2x per cell. Cells run goal by goal, so a stop at the spend ceiling leaves whole
goals finished rather than every goal half-done.

Resumable: each cell has a fixed directory under runs/sweep/sec01/. A rerun counts the
run files already saved there and continues from the next slot; complete cells are
skipped. A run interrupted mid-way saved no file and is re-run (its spend up to the
interruption is already in the ledger). Raw runs stay in runs/ (gitignored); the digest
is written to docs/results/sec01.md, which git tracks.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agentic_trust.harness.models import AgentModel, ModelSettings
from agentic_trust.report import sec01 as report_uc
from agentic_trust.report.digest import render
from agentic_trust.report.runs import load_all
from agentic_trust.runner import cell, preflight, tracing
from agentic_trust.runner.budget import PROJECT_BUDGET_USD, Ledger
from agentic_trust.scenarios.security.sec01 import config as sec01

ROOT = Path("runs/sweep/sec01")
DIGEST = Path("docs/results/sec01.md")
LAB_LOG = Path("docs/lab_log.md")
LEVELS = ("P0", "P1", "P2")


@dataclass(frozen=True)
class CellSpec:
    goal: str | None  # None = the shared P0 cell
    level: str
    arm: str

    @property
    def name(self) -> str:
        return f"{self.goal or 'base'}-{self.level}-{self.arm}"


def plan() -> list[CellSpec]:
    """Every cell of the SEC-01 sweep, in run order: the shared P0 cell, then goal by goal."""
    cells = [CellSpec(None, "P0", "main")]
    for goal in sec01.GOALS:
        arms = ["main"] + sorted(a for g, a in sec01.ARMS if g == goal and a != "main")
        for arm in arms:
            shared = arm == "main" and goal in sec01.SHARED_P0_GOALS
            cells += [CellSpec(goal, lv, arm) for lv in LEVELS if not (lv == "P0" and shared)]
    return cells


@dataclass
class CellStatus:
    spec: CellSpec
    completed: int
    attempts: int
    target: int

    @property
    def max_attempts(self) -> int:
        return 2 * self.target

    @property
    def complete(self) -> bool:
        return self.completed >= self.target or self.attempts >= self.max_attempts

    @property
    def remaining(self) -> int:
        return 0 if self.complete else self.target - self.completed


def status(spec: CellSpec, root: Path, target: int) -> CellStatus:
    saved = cell.saved_summaries(root / spec.name)
    done = sum(1 for s in saved if s["end_reason"] != "refusal")
    return CellStatus(spec, done, len(saved), target)


def estimate_remaining(statuses: list[CellStatus]) -> dict[str, float]:
    """Estimated cost of the runs still to do, per run profile (short/typical/long)."""
    totals: dict[str, float] = {}
    for st in statuses:
        if st.remaining:
            est = cell.cell_estimate(st.spec.goal, st.spec.level, st.spec.arm, st.remaining)
            for name, p in est["profiles"].items():
                totals[name] = round(totals.get(name, 0.0) + p["cost_cell_usd"], 2)
    return totals


@dataclass
class Tally:
    runs: int = 0
    refused: int = 0
    spent: float = 0.0


def run_sweep(
    cells: list[CellSpec], target: int, *,
    root: Path, ledger: Ledger,
    model_factory: Callable[[ModelSettings], AgentModel],
    log: Callable[[str], None] = print,
    lab_log: Path | None = None,
    limit_usd: float = PROJECT_BUDGET_USD,
) -> str:
    """Run every incomplete cell in order. Returns "complete" or "budget_stop"."""
    statuses = [status(c, root, target) for c in cells]
    todo = [s for s in statuses if not s.complete]
    tally = Tally()
    for i, st in enumerate(todo, 1):
        spec = st.spec
        log(f"== cell {i}/{len(todo)}: {spec.name} ({st.completed}/{target} done, "
            f"{st.attempts} attempt(s) saved)")

        def on_run(summary: dict, spec=spec, i=i) -> None:
            if summary["end_reason"] == "budget_stop":
                return
            tally.runs += 1
            tally.refused += summary["end_reason"] == "refusal"
            tally.spent += summary["cost_usd"]
            v = summary["verdict"]["score"]
            log(f"   {summary['run_id']}: {summary['end_reason']}, verdict {v}, ${summary['cost_usd']:.3f} | "
                f"sweep: {tally.runs} runs, {tally.refused} refused, ${tally.spent:.2f} this sweep, "
                f"ledger ${ledger.total():.2f} of ${limit_usd:.0f}, cell {i}/{len(todo)}")

        summaries = cell.run_cell(spec.goal, spec.level, spec.arm, target, model_factory=model_factory,
                                  ledger=ledger, out_dir=root / spec.name, tags=("sweep", f"sweep-{spec.name}"),
                                  log=lambda _: None, limit_usd=limit_usd, resume=True, on_run=on_run)
        stats = cell.cell_stats(summaries, target)
        if lab_log is not None and lab_log.exists():
            with lab_log.open("a") as f:
                f.write(cell.lab_log_line(spec.goal or "shared-P0", spec.level, spec.arm, stats,
                                          root / spec.name, time.strftime("%Y-%m-%d %H:%M")))
        tracing.flush_quietly()
        if stats["budget_stop"]:
            log(f"Stopped at the ${limit_usd:.0f} ceiling during {spec.name}.")
            return "budget_stop"
    return "complete"


def write_digest(root: Path, out: Path, cells: list[CellSpec], target: int) -> Path:
    runs, skipped = load_all(root, "sec01")
    statuses = [status(c, root, target) for c in cells]
    done = sum(s.complete for s in statuses)
    banner = "" if done == len(cells) else (
        f"PARTIAL: {done} of {len(cells)} cells complete. Goals without all three levels have no comparison yet.")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(runs, skipped, report_uc, title=f"{report_uc.DISPLAY_NAME} results", banner=banner))
    return out


def print_plan(statuses: list[CellStatus], est: dict[str, float], ledger: Ledger, target: int) -> None:
    print(f"SEC-01 sweep: {len(statuses)} cells, target {target} completed runs each "
          f"(refused runs re-run, up to {2 * target} attempts a cell)")
    for st in statuses:
        state = "complete" if st.complete else (f"{st.completed}/{target} done" if st.attempts else "to run")
        print(f"  {st.spec.name:<22} {state:<12} {st.remaining:>3} run(s) to go")
    left = sum(s.remaining for s in statuses)
    spent = ledger.total()
    print(f"\n{left} runs to go. Estimated cost of those runs:")
    for name, usd in est.items():
        print(f"  if runs are {name:<8}: ${usd:.2f}")
    print(f"Project spend so far ${spent:.2f} of ${PROJECT_BUDGET_USD:.0f} (ledger {ledger.path}); "
          f"remaining ${PROJECT_BUDGET_USD - spent:.2f}. Refused attempts cost little (no output) and "
          "are not in the estimate.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeats", type=int, default=cell.DEFAULT_REPEATS, help="completed runs per cell")
    ap.add_argument("--root", type=Path, default=ROOT, help="where run files are saved (gitignored)")
    ap.add_argument("--digest", type=Path, default=DIGEST, help="where the digest is written (git-tracked)")
    ap.add_argument("--plan", action="store_true", help="print the plan and estimate, then exit; no keys needed")
    ap.add_argument("--report-only", action="store_true", help="write the digest from saved runs, run nothing")
    args = ap.parse_args(argv)

    cells = plan()
    ledger = Ledger()
    statuses = [status(c, args.root, args.repeats) for c in cells]
    est = estimate_remaining(statuses)
    if args.plan:
        print_plan(statuses, est, ledger, args.repeats)
        return 0
    if args.report_only:
        print(f"Wrote {write_digest(args.root, args.digest, cells, args.repeats)}")
        return 0

    if not preflight.check():
        return 1
    st = tracing.configure()
    print(st.describe())
    missing = [k for k in ("ANTHROPIC_API_KEY", "LANGSMITH_API_KEY") if not os.environ.get(k)]
    if missing or not st.enabled:
        print(f"Not running: missing {', '.join(missing) or 'LANGSMITH_TRACING=true'} (see .env.example).")
        return 1

    print_plan(statuses, est, ledger, args.repeats)
    if all(s.complete for s in statuses):
        print(f"\nEvery cell is complete. Wrote {write_digest(args.root, args.digest, cells, args.repeats)}")
        return 0
    spent = ledger.total()
    if spent >= PROJECT_BUDGET_USD:
        print("Not running: the project ceiling is already reached.")
        return 1
    if spent + max(est.values()) > PROJECT_BUDGET_USD:
        print(f"\nWARNING: the higher estimates would take total spend past ${PROJECT_BUDGET_USD:.0f}. The budget "
              "guard stops the sweep before any call that would cross it; cells run goal by goal, so finished "
              "goals stay finished. Rerun later to resume (after raising the ceiling, if you choose to).")
    if input("\nStart the sweep? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 1

    from agentic_trust.harness.models import make_model

    try:
        outcome = run_sweep(cells, args.repeats, root=args.root, ledger=ledger, model_factory=make_model,
                            lab_log=LAB_LOG)
    except KeyboardInterrupt:
        tracing.flush_quietly()
        print("\nStopped. Every finished run is saved; rerun the same command to resume where it left off.")
        return 130
    out = write_digest(args.root, args.digest, cells, args.repeats)
    print(f"\nSweep {outcome}. Project spend now ${ledger.total():.2f} of ${PROJECT_BUDGET_USD:.0f}. "
          f"Digest: {out} (commit it). Raw runs: {args.root} (gitignored).")
    return 0 if outcome == "complete" else 2


if __name__ == "__main__":
    sys.exit(main())
