"""Print what each agent's model actually returned, from saved run files. No API calls.

    python -m agentic_trust.runner.inspect_run runs/smoke            # newest smoke cell
    python -m agentic_trust.runner.inspect_run runs/smoke/SEC-DEC-1-P1-main/20261023-101500/run-00.json

For each run: the end reason; for a refusal, the raw stop_reason, stop_details
(category, explanation), content and output tokens of the refused call; and each
agent's first response (its text and tool calls), which is what the agent did
with its first prompt.

Classification note: the harness ends a run as ``refusal`` only when the API
returns ``stop_reason == "refusal"``. An empty response that ends normally
(``end_turn``) just ends the agent's turn and is never counted as a refusal.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _run_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    files = sorted(target.rglob("run-*.json"))
    if not files:
        return []
    newest_cell = max({f.parent for f in files}, key=lambda d: d.name)  # cell dirs are timestamps
    return sorted(newest_cell.glob("run-*.json"))


def first_response(transcript: list[dict]) -> list[dict] | None:
    return next((m["content"] for m in transcript if m["role"] == "assistant"), None)


def describe_blocks(blocks: list[dict]) -> list[str]:
    if not blocks:
        return ["(no content blocks: the API returned an empty content list)"]
    lines = []
    for b in blocks:
        kind = b.get("type")
        if kind == "text":
            lines.append(f"text: {b['text']}")
        elif kind == "tool_use":
            lines.append(f"tool_use {b['name']}: {json.dumps(b.get('input'), sort_keys=True)}")
        elif kind in ("thinking", "redacted_thinking"):
            shown = (b.get("thinking") or "").strip()
            lines.append(f"{kind}: {shown[:300] or '(not displayed)'}")
        else:
            lines.append(f"{kind}: {json.dumps(b, sort_keys=True)[:300]}")
    return lines


def report(path: Path, out=print) -> None:
    data = json.loads(path.read_text())
    outputs, transcripts = data["outputs"], data["transcripts"]
    out(f"== {path}")
    out(f"end_reason: {outputs['end_reason']}  rounds: {outputs['rounds']}  day: {outputs['day']}  "
        f"requests: {outputs['usage']['requests']}  output_tokens: {outputs['usage']['output_tokens']}")
    for r in outputs["world"]["truth"].get("refusals", []):
        out(f"-- refusal by {r['agent']} in round {r['round']}")
        out(f"   stop_reason: {r.get('stop_reason', 'refusal')}")
        out(f"   stop_details: {json.dumps(r.get('stop_details'), sort_keys=True)}")
        if "usage" in r:
            out(f"   output_tokens of that call: {r['usage']['output_tokens']}  model: {r.get('model')}  "
                f"request_id: {r.get('request_id')}")
        if "content" in r:
            for line in describe_blocks(r["content"]):
                out(f"   {line}")
    for agent, transcript in transcripts.items():
        blocks = first_response(transcript)
        out(f"-- {agent}'s first response:")
        if blocks is None:
            out("   (never called: the agent had no turn)")
            continue
        for line in describe_blocks(blocks):
            out(f"   {line}")


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    target = Path(args[0]) if args else Path("runs/smoke")
    files = _run_files(target)
    if not files:
        print(f"no run-*.json under {target}")
        return 1
    for f in files:
        report(f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
