"""Run a use case's whole sweep: every goal at P0, P1 and P2, every arm, then the digest.

Run on your own machine, with ANTHROPIC_API_KEY and LANGSMITH_API_KEY in .env:

    python -m agentic_trust.runner.sweep --plan          # SEC-01 (the default): plan and cost; no keys, no spend
    python -m agentic_trust.runner.sweep                 # run SEC-01 (asks once), then write its digest
    python -m agentic_trust.runner.sweep sec03 --plan    # any use case in agentic_trust.registry, by name

Cells (build spec rules 1-2):
- one shared P0 cell, both agents on their base prompts, scored by the digest for
  every goal without a probe or arm change (config.SHARED_P0_GOALS);
- P0 cells of their own for probe goals (SEC-BA-3, SEC-BA-4) and for control arms;
- P1 and P2 cells for every goal and arm.
Each cell targets ``--repeats`` completed runs (default 10); refused runs are re-run,
up to 2x per cell. Cells run goal by goal, so a stop at the spend ceiling leaves whole
goals finished rather than every goal half-done.

Spend: ``--budget USD`` caps what this invocation may spend (e.g. ``--budget 20``); the
project's $100 total still applies on top. Every model call is checked first against the
most it could cost, so a stop never overshoots, and the sweep ends cleanly with a PARTIAL
digest. Rerun with a fresh ``--budget`` to continue.

Resumable: each cell has a fixed directory under runs/sweep/<use case>/. A rerun counts the
run files already saved there and continues from the next slot; complete cells are
skipped. A run interrupted mid-way saved no file and is re-run (its spend up to the
interruption is already in the ledger). Raw runs stay in runs/ (gitignored); the digest
is written to docs/results/<use case>.md, which git tracks. Use cases never share run
directories or digests.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agentic_trust import registry
from agentic_trust.harness.models import AgentModel, ModelSettings
from agentic_trust.report.digest import render
from agentic_trust.report.runs import load_all
from agentic_trust.runner import cell, preflight, tracing
from agentic_trust.runner.budget import PROJECT_BUDGET_USD, Ledger
DEFAULT_USE_CASE = "sec01"
ROOT = Path("runs/sweep/sec01")  # the default use case's paths; others: runs/sweep/<name>
DIGEST = Path("docs/results/sec01.md")
RUN_LOG = cell.RUN_LOG  # gitignored; the sweep never writes docs/lab_log.md (lab log 2026-11-05)
LEVELS = ("P0", "P1", "P2")


@dataclass(frozen=True)
class CellSpec:
    goal: str | None  # None = the shared P0 cell
    level: str
    arm: str

    @property
    def name(self) -> str:
        return f"{self.goal or 'base'}-{self.level}-{self.arm}"


def _uc(uc):
    return uc if uc is not None else registry.get(DEFAULT_USE_CASE)


def root_for(name: str) -> Path:
    return Path("runs/sweep") / name


def digest_for(name: str) -> Path:
    return Path("docs/results") / f"{name}.md"


def plan(uc=None, goals: list[str] | None = None) -> list[CellSpec]:
    """Every cell of a use case's sweep, in run order: the shared P0 cell, then goal by goal.
    ``goals`` keeps only those goals' cells (and the shared P0 cell if any of them use it)."""
    cfg = _uc(uc).config
    if goals is not None:
        unknown = set(goals) - set(cfg.GOALS)
        if unknown:
            raise ValueError(f"not goals of {cfg.USE_CASE}: {sorted(unknown)}")
    wanted = [g for g in cfg.GOALS if goals is None or g in goals]
    shared_needed = any(g in cfg.SHARED_P0_GOALS for g in wanted)
    cells = [CellSpec(None, "P0", "main")] if shared_needed else []
    for goal in wanted:
        arms = ["main"] + sorted(a for g, a in cfg.ARMS if g == goal and a != "main")
        for arm in arms:
            shared = arm == "main" and goal in cfg.SHARED_P0_GOALS
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


def estimate_remaining(statuses: list[CellStatus], uc=None) -> dict[str, float]:
    """Estimated cost of the runs still to do, per run profile (short/typical/long)."""
    totals: dict[str, float] = {}
    for st in statuses:
        if st.remaining:
            est = cell.cell_estimate(st.spec.goal, st.spec.level, st.spec.arm, st.remaining, _uc(uc))
            for name, p in est["profiles"].items():
                totals[name] = round(totals.get(name, 0.0) + p["cost_cell_usd"], 2)
    return totals


def long_run_cost(spec: CellSpec, uc=None) -> float:
    """Estimated cost of one long run in this cell: what the sweep reserves before starting a run."""
    return cell.cell_estimate(spec.goal, spec.level, spec.arm, 1, _uc(uc))["profiles"]["long"]["cost_per_run_usd"]


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
    reserve_per_run: float | None = None,
    uc=None,
) -> str:
    """Run every incomplete cell in order. Returns "complete" or "budget_stop".

    Before each run starts, the sweep reserves the estimated cost of a long run for that
    cell (``reserve_per_run`` overrides it) and stops cleanly if that would pass
    ``limit_usd``. Every model call is also checked against the limit by the budget guard."""
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
                f"ledger ${ledger.total():.2f} of ${limit_usd:.2f}, cell {i}/{len(todo)}")

        reserve = reserve_per_run if reserve_per_run is not None else long_run_cost(spec, uc)
        summaries = cell.run_cell(spec.goal, spec.level, spec.arm, target, model_factory=model_factory,
                                  ledger=ledger, out_dir=root / spec.name, tags=("sweep", f"sweep-{spec.name}"),
                                  log=lambda _: None, limit_usd=limit_usd, resume=True, on_run=on_run,
                                  reserve_per_run=reserve, uc=_uc(uc))
        stats = cell.cell_stats(summaries, target)
        if lab_log is not None:
            cell.append_run_log(cell.lab_log_line(spec.goal or "shared-P0", spec.level, spec.arm, stats,
                                                  root / spec.name, time.strftime("%Y-%m-%d %H:%M")), lab_log)
        tracing.flush_quietly()
        if stats["budget_stop"]:
            log(f"Stopped at the ${limit_usd:.2f} spend limit during {spec.name}.")
            return "budget_stop"
    return "complete"


def write_digest(root: Path, out: Path, cells: list[CellSpec], target: int, uc=None) -> Path:
    uc = _uc(uc)
    report_uc = uc.report
    runs, skipped = load_all(root, uc.name)
    statuses = [status(c, root, target) for c in cells]
    done = sum(s.complete for s in statuses)
    banner = "" if done == len(cells) else (
        f"PARTIAL: {done} of {len(cells)} cells complete. Goals without all three levels have no comparison yet.")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(runs, skipped, report_uc, title=f"{report_uc.DISPLAY_NAME} results", banner=banner))
    return out


def spend_limit(spent: float, budget: float | None) -> float:
    """The ledger total this invocation may reach: what is spent so far plus ``budget``,
    never above the project ceiling."""
    return PROJECT_BUDGET_USD if budget is None else min(PROJECT_BUDGET_USD, spent + budget)


def print_plan(statuses: list[CellStatus], est: dict[str, float], ledger: Ledger, target: int,
               limit: float = PROJECT_BUDGET_USD, name: str = DEFAULT_USE_CASE) -> None:
    print(f"{name} sweep: {len(statuses)} cells, target {target} completed runs each "
          f"(refused runs re-run, up to {2 * target} attempts a cell)")
    for st in statuses:
        state = "complete" if st.complete else (f"{st.completed}/{target} done" if st.attempts else "to run")
        print(f"  {st.spec.name:<22} {state:<12} {st.remaining:>3} run(s) to go")
    left = sum(s.remaining for s in statuses)
    spent = ledger.total()
    print(f"\n{left} runs to go. Estimated cost of those runs:")
    for name, usd in est.items():
        print(f"  if runs are {name:<8}: ${usd:.2f}")
    print(f"Project spend so far ${spent:.2f} of ${PROJECT_BUDGET_USD:.0f} (ledger {ledger.path}). "
          f"This sweep stops at a ledger total of ${limit:.2f}: up to ${max(limit - spent, 0):.2f} new spend. "
          "Refused attempts cost little (no output) and are not in the estimate.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("use_case", nargs="?", default=DEFAULT_USE_CASE, choices=sorted(registry.USE_CASES),
                    help=f"which use case to sweep (default {DEFAULT_USE_CASE})")
    ap.add_argument("--repeats", type=int, default=cell.DEFAULT_REPEATS, help="completed runs per cell")
    ap.add_argument("--root", type=Path, help="where run files are saved (default runs/sweep/<use case>, gitignored)")
    ap.add_argument("--digest", type=Path, help="where the digest is written (default docs/results/<use case>.md)")
    ap.add_argument("--plan", action="store_true", help="print the plan and estimate, then exit; no keys needed")
    ap.add_argument("--report-only", action="store_true", help="write the digest from saved runs, run nothing")
    ap.add_argument("--goals", help="comma-separated goal IDs to run (default: every goal); "
                                     "the same cell directories are used, so other goals can run later")
    ap.add_argument("--budget", type=float, metavar="USD",
                    help=f"most this invocation may spend (default: up to the ${PROJECT_BUDGET_USD:.0f} project ceiling)")
    args = ap.parse_args(argv)
    if args.budget is not None and args.budget <= 0:
        ap.error("--budget must be more than 0")

    uc = registry.get(args.use_case)
    args.root = args.root or root_for(uc.name)
    args.digest = args.digest or digest_for(uc.name)
    goals = [g.strip() for g in args.goals.split(",")] if args.goals else None
    try:
        cells = plan(uc, goals)
    except ValueError as exc:
        ap.error(str(exc))
    ledger = Ledger()
    statuses = [status(c, args.root, args.repeats) for c in cells]
    est = estimate_remaining(statuses, uc)
    limit = spend_limit(ledger.total(), args.budget)
    if args.plan:
        print_plan(statuses, est, ledger, args.repeats, limit, uc.name)
        return 0
    if args.report_only:
        print(f"Wrote {write_digest(args.root, args.digest, cells, args.repeats, uc)}")
        return 0

    if not preflight.check():
        return 1
    st = tracing.configure()
    print(st.describe())
    missing = [k for k in ("ANTHROPIC_API_KEY", "LANGSMITH_API_KEY") if not os.environ.get(k)]
    if missing or not st.enabled:
        print(f"Not running: missing {', '.join(missing) or 'LANGSMITH_TRACING=true'} (see .env.example).")
        return 1

    print_plan(statuses, est, ledger, args.repeats, limit, uc.name)
    if all(s.complete for s in statuses):
        print(f"\nEvery cell is complete. Wrote {write_digest(args.root, args.digest, cells, args.repeats, uc)}")
        return 0
    spent = ledger.total()
    if spent >= limit:
        print(f"Not running: the ledger total ${spent:.2f} already reaches this sweep's limit ${limit:.2f}.")
        return 1
    if spent + max(est.values()) > limit:
        print(f"\nWARNING: the higher estimates would take spend past ${limit:.2f}. The budget guard stops the sweep "
              "before any call that would cross it; cells run goal by goal, so finished goals stay finished. "
              "Rerun later with a new --budget to resume.")
    if input("\nStart the sweep? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 1

    from agentic_trust.harness.models import make_model

    try:
        outcome = run_sweep(cells, args.repeats, root=args.root, ledger=ledger, model_factory=make_model,
                            lab_log=RUN_LOG, limit_usd=limit, uc=uc)
    except KeyboardInterrupt:
        tracing.flush_quietly()
        print("\nStopped. Every finished run is saved; rerun the same command to resume where it left off.")
        return 130
    out = write_digest(args.root, args.digest, cells, args.repeats, uc)
    print(f"\nSweep {outcome}. Ledger total now ${ledger.total():.2f} (this sweep's limit ${limit:.2f}; "
          f"project ceiling ${PROJECT_BUDGET_USD:.0f}). "
          f"Digest: {out} (commit it). Raw runs: {args.root} (gitignored).")
    return 0 if outcome == "complete" else 2


if __name__ == "__main__":
    sys.exit(main())
