"""Count tool calls per cell from saved run files, to tell "never called" from "called
but failed". No API calls.

    python -m agentic_trust.runner.tool_usage runs/sweep/sec01
    python -m agentic_trust.runner.tool_usage runs/sweep/sec01 --tool commit_patch_eta

For each cell: runs and their end reasons, then each agent's calls per tool as
ok/error. With --tool, every failed call to that tool is listed with its error, and
every run is listed with whether that tool was called at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def tally(root: Path) -> dict[str, dict]:
    cells: dict[str, dict] = defaultdict(lambda: {"runs": 0, "ends": Counter(), "calls": Counter(), "files": []})
    for path in sorted(root.rglob("run-*.json")):
        data = json.loads(path.read_text())
        o = data["outputs"]
        c = cells[path.parent.name]
        c["runs"] += 1
        c["ends"][o["end_reason"]] += 1
        c["files"].append((path, o))
        for call in o["tool_log"]:
            if call.get("scripted"):
                continue
            c["calls"][(call["agent"], call["tool"], "error" if call["is_error"] else "ok")] += 1
    return dict(cells)


def report(root: Path, tool: str | None = None, out=print) -> None:
    for name, c in tally(root).items():
        ends = ", ".join(f"{k} {v}" for k, v in c["ends"].most_common())
        out(f"== {name}: {c['runs']} run(s); ends: {ends}")
        tools = sorted({(a, t) for a, t, _ in c["calls"]})
        for agent, t in tools:
            ok, err = c["calls"][(agent, t, "ok")], c["calls"][(agent, t, "error")]
            out(f"   {agent:<10} {t:<28} ok {ok:>3}  error {err:>3}")
        if tool:
            called = 0
            for path, o in c["files"]:
                mine = [x for x in o["tool_log"] if x["tool"] == tool and not x.get("scripted")]
                called += bool(mine)
                for x in mine:
                    if x["is_error"]:
                        out(f"   {tool} error in {path.name}: {x['result']}")
            out(f"   {tool}: called in {called} of {c['runs']} run(s)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, nargs="?", default=Path("runs/sweep/sec01"))
    ap.add_argument("--tool", help="list every run's use of this tool, and its errors")
    args = ap.parse_args(argv)
    if not any(args.root.rglob("run-*.json")):
        print(f"no run files under {args.root}")
        return 1
    report(args.root, args.tool)
    return 0


if __name__ == "__main__":
    sys.exit(main())
