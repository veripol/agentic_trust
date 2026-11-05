"""Run one cell: a goal at a level and arm, for a number of repeats, within the budget.

Run on your own machine, with ANTHROPIC_API_KEY and LANGSMITH_API_KEY in .env:

    python -m agentic_trust.runner.cell SEC-DEC-1 P1             # 10 repeats (the default)
    python -m agentic_trust.runner.cell SEC-ES-2 P2 --arm control --repeats 10
    python -m agentic_trust.runner.cell HLT-NA-2 P1              # any use case: found by goal ID

Before spending, it prints the cell's estimated cost, the project spend so far
(from the ledger) and what remains of the $100 ceiling. It asks for confirmation
when the estimate could take the total past the ceiling; --yes never skips that.
Every model call goes through the budget guard (runner/budget.py), so the
ceiling holds mid-run too. Outputs go to runs/<cell>/<stamp>/ (gitignored);
traces to the agentic-trust project.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable

from agentic_trust.harness.graph import run_episode
from agentic_trust.harness.models import PRICING, AgentModel, ModelSettings
from agentic_trust.runner import preflight, tracing
from agentic_trust.runner.budget import PROJECT_BUDGET_USD, BudgetedModel, BudgetExceeded, Ledger, worst_call_cost
from agentic_trust.runner.estimate import estimate
from agentic_trust.scenarios.security.sec01 import config as sec01  # the default use case's config

DEFAULT_REPEATS = 10  # results at 10 runs per cell are reported as directional


def _uc(uc):
    from agentic_trust import registry

    return uc if uc is not None else registry.get("sec01")


def cell_estimate(goal: str | None, level: str, arm: str, repeats: int, uc=None) -> dict:
    uc = _uc(uc)
    cfg = uc.config.make_config(goal, level, arm)
    defs = {a: uc.config.SCENARIO.agents[a].definitions() for a in cfg.agents}
    prompts = uc.prompts.build_prompts(cfg)
    profiles = [p for p in uc.profiles]
    # Agents not in this run (e.g. a seeded arm that leaves one out) drop out of the estimate.
    profiles = [type(p)(p.name, p.rounds, {a: n for a, n in p.calls_per_turn.items() if a in cfg.agents},
                        p.output_per_call, p.history_per_call, p.inbox_per_turn, p.kickoff) for p in profiles]
    first = uc.first_agent if uc.first_agent in cfg.agents else cfg.agents[0]
    return estimate(prompts, defs, profiles, runs=repeats, first_agent=first,
                    model=cfg.model, max_run_tokens=cfg.max_run_tokens)


def budget_check(est: dict, ledger: Ledger, limit: float = PROJECT_BUDGET_USD) -> tuple[float, float, bool]:
    """(spent, high estimate, needs confirmation): confirm when the long-run estimate could cross the ceiling."""
    spent = ledger.total()
    high = max(p["cost_cell_usd"] for p in est["profiles"].values())
    return spent, high, spent + high > limit


def saved_summaries(out_dir: Path) -> list[dict]:
    """Summaries of the runs already saved in a cell directory, in the order they ran."""
    found = []
    for f in out_dir.glob("run-*.json") if out_dir.exists() else []:
        s = json.loads(f.read_text())["summary"]
        found.append(s)
    return sorted(found, key=lambda s: (s.get("repeat", 0), s.get("attempt", 0)))


def resume_point(summaries: list[dict]) -> tuple[int, int]:
    """(next slot, its retry number) after the runs already saved: slots fill in order,
    and a slot is done once it has a run that was not refused."""
    done = {s["repeat"] for s in summaries if s["end_reason"] != "refusal"}
    repeat = len(done)
    attempt = sum(1 for s in summaries if s["repeat"] == repeat and s["end_reason"] == "refusal")
    return repeat, attempt


def run_cell(
    goal: str | None, level: str, arm: str, target: int, *,
    model_factory: Callable[[ModelSettings], AgentModel],
    ledger: Ledger,
    out_dir: Path,
    tags: tuple[str, ...] = (),
    log: Callable[[str], None] = print,
    limit_usd: float = PROJECT_BUDGET_USD,
    max_attempts: int | None = None,
    resume: bool = False,
    on_run: Callable[[dict], None] | None = None,
    reserve_per_run: float = 0.0,
    uc=None,
) -> list[dict]:
    """Run until ``target`` runs finish without a refusal, or ``max_attempts`` (default
    2 x target) attempts are used. A refused run is unscored and re-run under a new run
    id; every attempt, refused or not, is saved and returned. Stops early if the budget
    guard refuses a call.

    ``goal=None`` is the shared P0 cell: base prompts for both agents, scored per goal in
    the digest (build spec rule 2). ``resume=True`` continues from the runs already saved
    in ``out_dir`` instead of starting over; those count toward the target and the cap.
    ``on_run`` is called with each new run's summary. ``reserve_per_run`` is checked before
    each run starts: if the ledger total plus that reserve would pass ``limit_usd``, the cell
    stops without starting the run, so no run is cut off half-way by the per-call guard
    (which still backs this up on every call)."""
    uc = _uc(uc)
    out_dir.mkdir(parents=True, exist_ok=True)
    max_attempts = 2 * target if max_attempts is None else max_attempts
    name = goal or "base"
    summaries: list[dict] = saved_summaries(out_dir) if resume else []
    repeat, attempt = resume_point(summaries) if resume else (0, 0)  # the slot being filled, and its retry number
    while repeat < target and len(summaries) < max_attempts:
        run_id = f"{uc.config.USE_CASE}/{name}/{level}/{arm}/r{repeat:02d}" + (f"/a{attempt}" if attempt else "")
        cfg = uc.config.make_config(goal, level, arm, repeat, tags=tags, run_id=run_id)
        model = BudgetedModel(model_factory(ModelSettings(model=cfg.model, effort=cfg.effort)), ledger, cfg.run_id, limit_usd)
        label = f"[{len(summaries) + 1}, slot {repeat + 1}/{target}]"
        spent = ledger.total()
        if reserve_per_run:
            # Never reserve less than the per-call guard holds back for a call or two, or a
            # run that started could still be cut off by the guard half-way through.
            settings = ModelSettings(model=cfg.model, effort=cfg.effort)
            reserve_per_run = max(reserve_per_run, 2 * worst_call_cost(cfg.model, settings.max_tokens, "", [], []))
        if reserve_per_run and spent + reserve_per_run > limit_usd:
            msg = (f"not starting {cfg.run_id}: ${spent:.2f} spent + ${reserve_per_run:.2f} reserved for one run "
                   f"would pass the ${limit_usd:.2f} limit")
            log(f"{label} STOPPED: {msg}")
            stop = {"run_id": cfg.run_id, "repeat": repeat, "attempt": attempt, "end_reason": "budget_stop", "error": msg}
            summaries.append(stop)
            if on_run:
                on_run(stop)
            break
        started = time.monotonic()
        try:
            result = run_episode(cfg, uc.config.SCENARIO, uc.prompts.build_prompts(cfg), model)
        except BudgetExceeded as exc:
            log(f"{label} {cfg.run_id}: STOPPED by the budget guard: {exc}")
            stop = {"run_id": cfg.run_id, "repeat": repeat, "attempt": attempt,
                    "end_reason": "budget_stop", "error": str(exc)}
            summaries.append(stop)
            if on_run:
                on_run(stop)
            break
        verdict = uc.scorers.SCORERS[goal](result.outputs()) if goal else _SHARED
        refusals = result.world["truth"].get("refusals", [])
        summary = {
            "run_id": cfg.run_id,
            "repeat": repeat,
            "attempt": attempt,
            "end_reason": result.end_reason,
            "rounds": result.rounds,
            "day": result.day,
            "seconds": round(time.monotonic() - started, 1),
            "usage": result.usage.to_dict(),
            "capped_tokens": result.usage.capped_tokens,
            "cost_usd": round(PRICING[cfg.model].cost(result.usage), 4),
            "refusal_categories": [(r.get("stop_details") or {}).get("category") for r in refusals],
            "verdict": {"score": verdict.score, "comment": verdict.comment, "receiver": verdict.receiver,
                        "needs_review": verdict.needs_review},
        }
        summaries.append(summary)
        fname = f"run-{repeat:02d}" + (f"-a{attempt}" if attempt else "") + ".json"
        (out_dir / fname).write_text(json.dumps({
            "config": {"run_id": cfg.run_id, "use_case": cfg.use_case, "goal": name, "level": level, "arm": arm,
                       "repeat": repeat, "attempt": attempt, "tags": list(cfg.tags),
                       "model": cfg.model, "effort": cfg.effort},
            "summary": summary, "outputs": result.outputs(), "wire": result.wire,
            "transcripts": result.transcripts, "prompts": result.prompts}, indent=2, default=str))
        for r in refusals:
            d = r.get("stop_details") or {}
            log(f"    refusal: {r['agent']} round {r['round']}, category={d.get('category')!r}, "
                f"output_tokens={r['usage']['output_tokens']}, request_id={r.get('request_id')}")
        log(f"{label} {cfg.run_id}: end={summary['end_reason']} rounds={summary['rounds']} "
            f"day={summary['day']} requests={summary['usage']['requests']} capped_tokens={summary['capped_tokens']} "
            f"cost=${summary['cost_usd']:.3f} {name}={verdict.score} ({verdict.comment})")
        if on_run:
            on_run(summary)
        if result.end_reason == "refusal":
            attempt += 1  # re-run this slot
        else:
            repeat, attempt = repeat + 1, 0
    stats = cell_stats(summaries, target)
    (out_dir / "cell.json").write_text(json.dumps(
        {"goal": name, "level": level, "arm": arm, "max_attempts": max_attempts, **stats,
         "runs": [s["run_id"] for s in summaries]}, indent=2))
    return summaries


class _Shared:
    """Verdict stand-in for shared P0 runs, which the digest scores once per goal."""

    score = None
    comment = "shared P0 run: scored per goal in the digest"
    receiver: dict = {}
    needs_review = False


_SHARED = _Shared()


def cell_stats(summaries: list[dict], target: int) -> dict:
    """Attempts, refusals and the refusal rate for a cell, from its run summaries."""
    ran = [s for s in summaries if s["end_reason"] != "budget_stop"]
    refused = [s for s in ran if s["end_reason"] == "refusal"]
    categories: dict[str, int] = {}
    for s in refused:
        for c in s.get("refusal_categories") or [None]:
            categories[str(c)] = categories.get(str(c), 0) + 1
    return {
        "target": target,
        "attempts": len(ran),
        "completed": len(ran) - len(refused),
        "refused": len(refused),
        "refusal_rate": round(len(refused) / len(ran), 3) if ran else None,
        "refusal_categories": categories,
        "reached_target": len(ran) - len(refused) >= target,
        "budget_stop": any(s["end_reason"] == "budget_stop" for s in summaries),
    }


# Per-cell refusal-rate lines go here, not to docs/lab_log.md: the tracked lab log is for
# human-written entries, and sweep writes to it caused pull conflicts (lab log 2026-11-05).
# runs/ is gitignored.
RUN_LOG = Path("runs/run_log.md")


def append_run_log(line: str, path: Path = RUN_LOG) -> Path:
    """Append one cell's line to the (gitignored) run log, creating it if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a") as f:
        if new:
            f.write("# Run log\n\nOne entry per cell, written by the cell runner and the sweep. "
                    "Gitignored; human entries go in docs/lab_log.md.\n")
        f.write(line)
    return path


def lab_log_line(goal: str, level: str, arm: str, stats: dict, out_dir: Path, when: str) -> str:
    cats = ", ".join(f"{c} x{n}" for c, n in sorted(stats["refusal_categories"].items())) or "none"
    rate = "n/a" if stats["refusal_rate"] is None else f"{stats['refusal_rate']:.0%}"
    short = "" if stats["reached_target"] else f" **Target of {stats['target']} not reached.**"
    return (f"\n### {when}: cell {goal} {level} arm={arm}: refusal rate {rate}\n\n"
            f"- {stats['completed']} completed runs (target {stats['target']}) in {stats['attempts']} attempts; "
            f"{stats['refused']} refused ({rate}); categories: {cats}.{short} Outputs: `{out_dir}`.\n")


def _trace_links(tag: str) -> list[str]:
    from langsmith import Client

    client = Client()
    project = os.environ.get("LANGSMITH_PROJECT", tracing.PROJECT)
    runs = client.list_runs(project_name=project, is_root=True, filter=f'has(tags, "{tag}")')
    return [client.get_run_url(run=r, project_name=project) for r in runs]


def main(argv: list[str] | None = None, *, always_confirm: bool = False, label: str = "cell") -> int:
    from agentic_trust import registry

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    all_goals = sorted(g for u in registry.available().values() for g in u.config.GOALS)
    ap.add_argument("goal", choices=all_goals)
    ap.add_argument("level", choices=["P0", "P1", "P2"])
    ap.add_argument("--arm", default="main")
    ap.add_argument("--repeats", type=int, default=DEFAULT_REPEATS,
                    help="target number of completed (non-refused) runs; refused runs are retried up to 2x this")
    ap.add_argument("--yes", action="store_true", help="skip the routine confirmation (never the budget one)")
    ap.add_argument("--out", type=Path, default=Path("runs"))
    args = ap.parse_args(argv)

    if not preflight.check():
        return 1
    st = tracing.configure()
    print(st.describe())
    missing = [k for k in ("ANTHROPIC_API_KEY", "LANGSMITH_API_KEY") if not os.environ.get(k)]
    if missing or not st.enabled:
        print(f"Not running: missing {', '.join(missing) or 'LANGSMITH_TRACING=true'} (see .env.example).")
        return 1

    uc = registry.use_case_of(args.goal)
    ledger = Ledger()
    est = cell_estimate(args.goal, args.level, args.arm, args.repeats, uc)
    spent, high, crosses = budget_check(est, ledger)
    cfg = uc.config.make_config(args.goal, args.level, args.arm)
    print(f"\n{label}: {args.goal} {args.level} arm={args.arm}, {args.repeats} run(s), {cfg.model} effort={cfg.effort}")
    for name, p in est["profiles"].items():
        print(f"  estimate if runs are {name:<8}: ${p['cost_cell_usd']:.2f} (${p['cost_per_run_usd']:.3f}/run"
              f"{', token cap would end them' if p['hits_token_cap'] else ''})")
    print(f"  project spend so far ${spent:.2f} of ${PROJECT_BUDGET_USD:.0f} (ledger {ledger.path}); "
          f"remaining ${PROJECT_BUDGET_USD - spent:.2f}")
    if spent >= PROJECT_BUDGET_USD:
        print("Not running: the project ceiling is already reached.")
        return 1
    if crosses:
        print(f"WARNING: up to ${high:.2f} more could take total spend past ${PROJECT_BUDGET_USD:.0f}. "
              "The budget guard will stop the cell before any call that would cross it.")
        if input("Run anyway, up to the ceiling? [y/N] ").strip().lower() != "y":
            print("Cancelled.")
            return 1
    elif (always_confirm or not args.yes) and input("Proceed? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 1

    from agentic_trust.harness.models import make_model

    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = f"{label}-{stamp}"
    out = args.out / label / f"{args.goal}-{args.level}-{args.arm}" / stamp
    summaries = run_cell(args.goal, args.level, args.arm, args.repeats, model_factory=make_model,
                         ledger=ledger, out_dir=out, tags=(label, tag), uc=uc)
    tracing.flush()

    stats = cell_stats(summaries, args.repeats)
    done = [s for s in summaries if s["end_reason"] != "budget_stop"]
    cost = sum(s["cost_usd"] for s in done)
    print(f"\nCell cost ${cost:.3f}; project spend now ${ledger.total():.2f} of ${PROJECT_BUDGET_USD:.0f}.")
    if done:
        rate = "n/a" if stats["refusal_rate"] is None else f"{stats['refusal_rate']:.0%}"
        print(f"  {stats['completed']} completed of target {stats['target']} in {stats['attempts']} attempts; "
              f"refusal rate {rate} {stats['refusal_categories'] or ''}")
        for reason in ("token_cap", "quiescent"):
            print(f"  {reason}: {sum(s['end_reason'] == reason for s in done)}/{len(done)}", end="")
        print(f"\n  max capped tokens in a run: {max(s['capped_tokens'] for s in done)} (cap {cfg.max_run_tokens})")
        scores = [s["verdict"]["score"] for s in done if s["end_reason"] != "refusal"]
        print(f"  {args.goal}: pass {scores.count(True)}, fail {scores.count(False)}, unscored {scores.count(None)}")
        if not stats["reached_target"]:
            print(f"  WARNING: only {stats['completed']} of {stats['target']} target runs completed.")
    if done:
        log = append_run_log(lab_log_line(args.goal, args.level, args.arm, stats, out, time.strftime("%Y-%m-%d %H:%M")))
        print(f"Refusal rate appended to {log}")
    print(f"Outputs: {out}")
    try:
        links = _trace_links(tag)
        print("Traces:" if links else f"Traces: none listed yet; filter the project by tag {tag}")
        for link in links:
            print(f"  {link}")
    except Exception as exc:  # the runs are done; a listing failure shouldn't hide that
        print(f"Could not list traces ({type(exc).__name__}: {exc}); filter by tag {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
