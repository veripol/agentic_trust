"""Why SEC-03 ISAC goals come back unscored: a per-run flow summary from saved run files.
No API calls.

    python -m agentic_trust.runner.sec03_flow runs/sweep/sec03            # every cell
    python -m agentic_trust.runner.sec03_flow runs/sweep/sec03 --goals SEC-BA-2 SEC-DEC-3

For each run of an ISAC goal it prints: the end reason; the round Org A recorded findings
in; Org B's submissions (round, level, TLP); the ISAC's relays and broadcasts; whether the
SEC-BA-2 probe fired and whether the ISAC took a turn after it; whether the file has turn
records (runs saved before 2026-11-04 don't); and the recomputed verdict.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from agentic_trust.scenarios.security.sec03.scorers import SCORERS

ISAC_GOALS = ("SEC-DEC-3", "SEC-BA-1", "SEC-BA-2")


def summarise(path: Path) -> dict:
    data = json.loads(path.read_text())
    out = data["outputs"]
    log, w = out["tool_log"], out["world"]["truth"]
    goal = (data.get("config") or {}).get("goal") or path.parent.name.split("-P")[0]
    arm = (data.get("config") or {}).get("arm") or path.parent.name.rsplit("-", 1)[-1]
    findings = [c["round"] for c in log if c["agent"] == "org_a" and c["tool"] == "record_findings" and not c["is_error"]]
    first_org_a_turn = min((c["round"] for c in log if c["agent"] == "org_a"), default=None)
    subs = [(c["round"], c["args"].get("bundle_id"), c["args"].get("tlp")) for c in log
            if c["agent"] == "org_b" and c["tool"] == "submit_indicators" and not c["is_error"]]
    isac = Counter(c["tool"] for c in log if c["agent"] == "isac")
    fired = [p for p in w.get("probes_fired", []) if p["goal"] == "SEC-BA-2"]
    turns = w.get("turns_taken")
    isac_turn_after = None
    if fired and turns is not None:
        isac_turn_after = any(t["agent"] == "isac" and t["round"] > fired[0]["round"] for t in turns)
    goals = [goal] if goal in SCORERS else list(ISAC_GOALS)
    return {
        "file": str(path), "goal": goal, "arm": arm, "end": out["end_reason"],
        "findings_round": findings[0] if findings else None,
        "findings_in_first_turn": bool(findings) and findings[0] == first_org_a_turn,
        "org_b_submissions": subs, "isac_calls": dict(isac),
        "probe_fired": bool(fired), "isac_turn_after_probe": isac_turn_after,
        "has_turn_records": turns is not None,
        "verdicts": {g: (v.score, v.comment) for g in goals if g in ISAC_GOALS for v in [SCORERS[g](out)]},
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", type=Path)
    ap.add_argument("--goals", nargs="*", default=list(ISAC_GOALS))
    args = ap.parse_args(argv)
    rows = []
    for f in sorted(args.root.rglob("run-*.json")):
        try:
            s = summarise(f)
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            print(f"skip {f}: {type(e).__name__}: {e}")
            continue
        if s["goal"] in args.goals or (s["goal"] not in SCORERS and set(args.goals) & set(ISAC_GOALS)):
            rows.append(s)
    for s in rows:
        v = "; ".join(f"{g}={sc}" for g, (sc, _) in s["verdicts"].items())
        print(f"{s['goal']:9} {s['arm']:7} end={s['end']:17} findings_round={s['findings_round']} "
              f"first_turn={s['findings_in_first_turn']} org_b_subs={s['org_b_submissions']} "
              f"isac={s['isac_calls']} probe={s['probe_fired']} isac_turn_after={s['isac_turn_after_probe']} "
              f"turn_records={s['has_turn_records']} | {v}")
    if rows:
        print(f"\n{len(rows)} runs. Org A recorded findings in its first turn: "
              f"{sum(s['findings_in_first_turn'] for s in rows)}. No Org B submission (main arms): "
              f"{sum(not s['org_b_submissions'] for s in rows if s['arm'] == 'main' and s['goal'] != 'SEC-BA-2')}. "
              f"Files without turn records: {sum(not s['has_turn_records'] for s in rows)}.")


if __name__ == "__main__":
    main()
