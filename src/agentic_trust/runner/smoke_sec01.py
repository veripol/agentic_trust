"""Local smoke test: SEC-DEC-1 at P1, a few runs on the study model, traced to LangSmith.

Run on your own machine, with ANTHROPIC_API_KEY and LANGSMITH_API_KEY in .env:

    python -m agentic_trust.runner.smoke_sec01            # 2 runs, asks before spending
    python -m agentic_trust.runner.smoke_sec01 --yes      # skip the confirmation

It prints the cost estimate first, then for each run: end reason, rounds, day,
tokens, actual cost and the SEC-DEC-1 verdict. Full outputs and transcripts are
written to runs/smoke/<stamp>/ (gitignored). Traces go to the agentic-trust
project, tagged "smoke" and "smoke-<stamp>".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable

from agentic_trust.harness.graph import RunResult, run_episode
from agentic_trust.harness.models import PRICING, AgentModel, ModelSettings
from agentic_trust.runner import tracing
from agentic_trust.runner.estimate import Profile, estimate
from agentic_trust.scenarios.security.sec01.config import SCENARIO, make_config
from agentic_trust.scenarios.security.sec01.prompts import build_prompts
from agentic_trust.scenarios.security.sec01.scorers import SCORERS

GOAL, LEVEL, ARM = "SEC-DEC-1", "P1", "main"

PROFILES = (
    Profile("short", rounds=3, calls_per_turn={"researcher": 3, "vendor": 3}, output_per_call=300),
    Profile("typical", rounds=6, calls_per_turn={"researcher": 3, "vendor": 4}, output_per_call=600),
    Profile("long", rounds=12, calls_per_turn={"researcher": 4, "vendor": 5}, output_per_call=900),
)


def cost_estimate(runs: int) -> dict:
    cfg = make_config(GOAL, LEVEL, ARM)
    defs = {a: SCENARIO.agents[a].definitions() for a in cfg.agents}
    return estimate(build_prompts(cfg), defs, PROFILES, runs=runs, first_agent="researcher",
                    model=cfg.model, max_run_tokens=cfg.max_run_tokens)


def run_smoke(runs: int, model_factory: Callable[[ModelSettings], AgentModel], out_dir: Path, stamp: str,
              log: Callable[[str], None] = print) -> list[dict]:
    """Run the cell's first ``runs`` repeats; return one summary per run. No tracing setup here."""
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for repeat in range(runs):
        cfg = make_config(GOAL, LEVEL, ARM, repeat, tags=("smoke", f"smoke-{stamp}"))
        model = model_factory(ModelSettings(model=cfg.model, effort=cfg.effort))
        started = time.monotonic()
        result: RunResult = run_episode(cfg, SCENARIO, build_prompts(cfg), model)
        verdict = SCORERS[GOAL](result.outputs())
        summary = {
            "run_id": cfg.run_id,
            "end_reason": result.end_reason,
            "rounds": result.rounds,
            "day": result.day,
            "seconds": round(time.monotonic() - started, 1),
            "usage": result.usage.to_dict(),
            "capped_tokens": result.usage.capped_tokens,
            "cost_usd": round(PRICING[cfg.model].cost(result.usage), 4),
            "verdict": {"score": verdict.score, "comment": verdict.comment, "receiver": verdict.receiver},
        }
        summaries.append(summary)
        path = out_dir / f"run-{repeat:02d}.json"
        path.write_text(json.dumps({"summary": summary, "outputs": result.outputs(), "wire": result.wire,
                                    "transcripts": result.transcripts, "prompts": result.prompts},
                                   indent=2, default=str))
        log(f"[{repeat + 1}/{runs}] {cfg.run_id}: end={summary['end_reason']} rounds={summary['rounds']} "
            f"day={summary['day']} requests={summary['usage']['requests']} capped_tokens={summary['capped_tokens']} "
            f"cost=${summary['cost_usd']:.3f} {GOAL}={verdict.score} ({verdict.comment})")
    return summaries


def _trace_links(stamp: str) -> list[str]:
    from langsmith import Client

    client = Client()
    project = os.environ.get("LANGSMITH_PROJECT", tracing.PROJECT)
    runs = client.list_runs(project_name=project, is_root=True,
                            filter=f'has(tags, "smoke-{stamp}")')
    return [client.get_run_url(run=r, project_name=project) for r in runs]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--yes", action="store_true", help="do not ask before spending")
    ap.add_argument("--out", type=Path, default=Path("runs/smoke"))
    args = ap.parse_args(argv)

    st = tracing.configure()
    print(st.describe())
    missing = [k for k in ("ANTHROPIC_API_KEY", "LANGSMITH_API_KEY") if not os.environ.get(k)]
    if missing or not st.enabled:
        print(f"Not running: missing {', '.join(missing) or 'LANGSMITH_TRACING=true'} (see .env.example).")
        return 1

    est = cost_estimate(args.runs)
    print(f"\n{GOAL} at {LEVEL}, {args.runs} run(s), model {make_config(GOAL).model} effort {make_config(GOAL).effort}")
    for name, p in est["profiles"].items():
        print(f"  estimate if runs are {name:<8}: ${p['cost_cell_usd']:.2f} total "
              f"(${p['cost_per_run_usd']:.3f}/run{', token cap would end them' if p['hits_token_cap'] else ''})")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 1

    from agentic_trust.harness.models import make_model

    stamp = time.strftime("%Y%m%d-%H%M%S")
    summaries = run_smoke(args.runs, make_model, args.out / stamp, stamp)
    tracing.flush()

    total = sum(s["cost_usd"] for s in summaries)
    refusals = sum(s["end_reason"] == "refusal" for s in summaries)
    capped = sum(s["end_reason"] == "token_cap" for s in summaries)
    print(f"\nTotal ${total:.3f}. Refusals: {refusals}/{len(summaries)}. Token-cap ends: {capped}/{len(summaries)}. "
          f"Max capped tokens in a run: {max(s['capped_tokens'] for s in summaries)} (cap {make_config(GOAL).max_run_tokens}).")
    print(f"Outputs: {args.out / stamp}")
    try:
        links = _trace_links(stamp)
        print("Traces:" if links else f"Traces: none listed yet; filter project by tag smoke-{stamp}")
        for link in links:
            print(f"  {link}")
    except Exception as exc:  # the runs are done; a listing failure shouldn't hide that
        print(f"Could not list traces ({type(exc).__name__}: {exc}); filter by tag smoke-{stamp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
