"""Loading saved run files for reporting, and checking they hold what the digest needs.

A run file is written by runner/cell.py: config, summary, outputs (end_reason,
tool_log, world truth and claims, day, rounds, usage), wire, transcripts, prompts.
Files written before the ``config`` block existed are still read: goal, level,
arm, repeat and attempt are parsed from the run id. A file missing anything the
digest uses is skipped and listed, never guessed at.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUIRED_OUTPUTS = ("end_reason", "tool_log", "world", "day", "rounds", "usage")
_RUN_ID = re.compile(r"^(?P<use_case>[^/]+)/(?P<goal>[^/]+)/(?P<level>P[012])/(?P<arm>[^/]+)/r(?P<repeat>\d+)(?:/a(?P<attempt>\d+))?$")


@dataclass(frozen=True)
class RunRecord:
    path: Path
    run_id: str
    use_case: str
    goal: str
    level: str
    arm: str
    repeat: int
    attempt: int
    outputs: dict[str, Any]
    transcripts: dict[str, Any]

    @property
    def short_id(self) -> str:
        return f"{self.level}/r{self.repeat:02d}" + (f"/a{self.attempt}" if self.attempt else "")

    @property
    def end_reason(self) -> str:
        return self.outputs["end_reason"]


def problems(data: dict[str, Any]) -> list[str]:
    """What a run file lacks for the digest (empty when it is complete)."""
    missing = []
    outputs = data.get("outputs")
    if not isinstance(outputs, dict):
        return ["no outputs"]
    missing += [f"outputs.{k}" for k in REQUIRED_OUTPUTS if k not in outputs]
    world = outputs.get("world") or {}
    missing += [f"outputs.world.{k}" for k in ("truth", "claims") if k not in world]
    if "transcripts" not in data:
        missing.append("transcripts")
    run_id = (data.get("config") or {}).get("run_id") or (data.get("summary") or {}).get("run_id")
    if not run_id or (not data.get("config") and not _RUN_ID.match(run_id)):
        missing.append("config (and no parseable run_id)")
    return missing


def load(path: Path) -> RunRecord:
    data = json.loads(path.read_text())
    gaps = problems(data)
    if gaps:
        raise ValueError(f"{path}: missing {', '.join(gaps)}")
    cfg = data.get("config")
    if cfg:
        fields = {k: cfg[k] for k in ("run_id", "use_case", "goal", "level", "arm", "repeat", "attempt")}
    else:
        run_id = data["summary"]["run_id"]
        m = _RUN_ID.match(run_id).groupdict()
        fields = {"run_id": run_id, "use_case": m["use_case"], "goal": m["goal"], "level": m["level"],
                  "arm": m["arm"], "repeat": int(m["repeat"]), "attempt": int(m["attempt"] or 0)}
    return RunRecord(path=path, outputs=data["outputs"], transcripts=data["transcripts"], **fields)


def load_all(root: Path, use_case: str) -> tuple[list[RunRecord], list[tuple[Path, str]]]:
    """Every run file under ``root`` for ``use_case``; plus (path, reason) for files skipped."""
    runs, skipped = [], []
    for path in sorted(root.rglob("run-*.json")):
        try:
            rec = load(path)
        except (ValueError, json.JSONDecodeError, KeyError) as exc:
            skipped.append((path, str(exc)))
            continue
        if rec.use_case == use_case:
            runs.append(rec)
    return runs, skipped
