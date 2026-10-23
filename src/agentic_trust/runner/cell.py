"""Run one cell: a goal at a level and arm, for a number of repeats, within the budget.

Run on your own machine, with ANTHROPIC_API_KEY and LANGSMITH_API_KEY in .env:

    python -m agentic_trust.runner.cell SEC-DEC-1 P1             # 10 repeats (the default)
    python -m agentic_trust.runner.cell SEC-ES-2 P2 --arm control --repeats 10

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
from agentic_trust.runner.budget import PROJECT_BUDGET_USD, BudgetedModel, BudgetExceeded, Ledger
from agentic_trust.runner.estimate import Profile, estimate
from agentic_trust.scenarios.security.sec01 import config as sec01
from agentic_trust.scenarios.security.sec01.prompts import build_prompts
from agentic_trust.scenarios.security.sec01.scorers import SCORERS

DEFAULT_REPEATS = 10  # results at 10 runs per cell are reported as directional

# How SEC-01 runs may unfold, for the estimate (runner/estimate.py).
PROFILES = (
    Profile("short", rounds=3, calls_per_turn={"researcher": 3, "vendor": 3}, output_per_call=300),
    Profile("typical", rounds=6, calls_per_turn={"researcher": 3, "vendor": 4}, output_per_call=600),
    Profile("long", rounds=10, calls_per_turn={"researcher": 4, "vendor": 5}, output_per_call=900),
)


def cell_estimate(goal: str, level: str, arm: str, repeats: int) -> dict:
    cfg = sec01.make_config(goal, level, arm)
    defs = {a: sec01.SCENARIO.agents[a].definitions() for a in cfg.agents}
    return estimate(build_prompts(cfg), defs, PROFILES, runs=repeats, first_agent="researcher",
                    model=cfg.model, max_run_tokens=cfg.max_run_tokens)


def budget_check(est: dict, ledger: Ledger, limit: float = PROJECT_BUDGET_USD) -> tuple[float, float, bool]:
    """(spent, high estimate, needs confirmation): confirm when the long-run estimate could cross the ceiling."""
    spent = ledger.total()
    high = max(p["cost_cell_usd"] for p in est["profiles"].values())
    return spent, high, spent + high > limit


def run_cell(
    goal: str, level: str, arm: str, target: int, *,
    model_factory: Callable[[ModelSettings], AgentModel],
    ledger: Ledger,
    out_dir: Path,
    tags: tuple[str, ...] = (),
    log: Callable[[str], None] = print,
    limit_usd: float = PROJECT_BUDGET_USD,
    max_attempts: int | None = None,
) -> list[dict]:
    """Run until ``target`` runs finish without a refusal, or ``max_attempts`` (default
    2 x target) attempts are used. A refused run is unscored and re-run under a new run
    id; every attempt, refused or not, is saved and returned. Stops early if the budget
    guard refuses a call."""
    out_dir.mkdir(parents=True, exist_ok=True)
    max_attempts = 2 * target if max_attempts is None else max_attempts
    summaries: list[dict] = []
    repeat, attempt = 0, 0  # the slot being filled, and its retry number
    while repeat < target and len(summaries) < max_attempts:
        run_id = f"{sec01.USE_CASE}/{goal}/{level}/{arm}/r{repeat:02d}" + (f"/a{attempt}" if attempt else "")
        cfg = sec01.make_config(goal, level, arm, repeat, tags=tags, run_id=run_id)
        model = BudgetedModel(model_factory(ModelSettings(model=cfg.model, effort=cfg.effort)), ledger, cfg.run_id, limit_usd)
        label = f"[{len(summaries) + 1}, slot {repeat + 1}/{target}]"
        started = time.monotonic()
        try:
            result = run_episode(cfg, sec01.SCENARIO, build_prompts(cfg), model)
        except BudgetExceeded as exc:
            log(f"{label} {cfg.run_id}: STOPPED by the budget guard: {exc}")
            summaries.append({"run_id": cfg.run_id, "repeat": repeat, "attempt": attempt,
                              "end_reason": "budget_stop", "error": str(exc)})
            break
        verdict = SCORERS[goal](result.outputs())
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
        name = f"run-{repeat:02d}" + (f"-a{attempt}" if attempt else "") + ".json"
        (out_dir / name).write_text(json.dumps({
            "config": {"run_id": cfg.run_id, "use_case": cfg.use_case, "goal": goal, "level": level, "arm": arm,
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
            f"cost=${summary['cost_usd']:.3f} {goal}={verdict.score} ({verdict.comment})")
        if result.end_reason == "refusal":
            attempt += 1  # re-run this slot
        else:
            repeat, attempt = repeat + 1, 0
    stats = cell_stats(summaries, target)
    (out_dir / "cell.json").write_text(json.dumps(
        {"goal": goal, "level": level, "arm": arm, **stats, "runs": [s["run_id"] for s in summaries]}, indent=2))
    return summaries


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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("goal", choices=sorted(sec01.GOALS))
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

    ledger = Ledger()
    est = cell_estimate(args.goal, args.level, args.arm, args.repeats)
    spent, high, crosses = budget_check(est, ledger)
    cfg = sec01.make_config(args.goal, args.level, args.arm)
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
                         ledger=ledger, out_dir=out, tags=(label, tag))
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
    lab_log = Path("docs/lab_log.md")
    if lab_log.exists() and done:
        with lab_log.open("a") as f:
            f.write(lab_log_line(args.goal, args.level, args.arm, stats, out, time.strftime("%Y-%m-%d %H:%M")))
        print(f"Refusal rate appended to {lab_log}")
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
