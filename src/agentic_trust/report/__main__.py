"""Write a use case's results digest from saved run files. No API calls.

    python -m agentic_trust.report sec01                      # runs/ -> docs/results/sec01.md
    python -m agentic_trust.report sec01 --runs runs/smoke --sample   # -> docs/results/_sample_sec01.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import sec01
from .digest import render
from .runs import load_all

USE_CASES = {"sec01": sec01}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("use_case", choices=sorted(USE_CASES))
    ap.add_argument("--runs", type=Path, default=Path("runs"), help="directory searched for run-*.json files")
    ap.add_argument("--out", type=Path, help="default docs/results/<use_case>.md (or _sample_<use_case>.md)")
    ap.add_argument("--sample", action="store_true", help="a format preview: marked as such, saved as _sample_")
    args = ap.parse_args(argv)

    uc = USE_CASES[args.use_case]
    runs, skipped = load_all(args.runs, args.use_case)
    if not runs:
        print(f"No usable {args.use_case} run files under {args.runs}.")
        for p, why in skipped:
            print(f"  skipped {p}: {why}")
        return 1
    out = args.out or Path("docs/results") / (f"_sample_{args.use_case}.md" if args.sample else f"{args.use_case}.md")
    banner = (f"SAMPLE: a format preview built from {len(runs)} smoke-test run(s). The counts are real but far "
              "too few to mean anything.") if args.sample else ""
    title = f"{uc.DISPLAY_NAME} results" + (" (sample)" if args.sample else "")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(runs, skipped, uc, title=title, banner=banner))
    print(f"Wrote {out} from {len(runs)} run file(s); skipped {len(skipped)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
