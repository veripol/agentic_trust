"""Sweep and cell runs never write the tracked docs/lab_log.md (lab log 2026-11-05)."""

import subprocess
from pathlib import Path

from agentic_trust.runner import cell, sweep

ROOT = Path(__file__).resolve().parents[2]


def test_run_log_is_gitignored_and_separate_from_the_lab_log():
    assert cell.RUN_LOG == sweep.RUN_LOG == Path("runs/run_log.md")
    ignored = subprocess.run(["git", "check-ignore", "-q", str(cell.RUN_LOG)], cwd=ROOT)
    assert ignored.returncode == 0
    for module in (cell, sweep):
        src = Path(module.__file__).read_text()
        assert 'Path("docs/lab_log.md")' not in src


def test_append_run_log_creates_the_file_with_a_header(tmp_path):
    log = tmp_path / "runs" / "run_log.md"
    cell.append_run_log("### a\n", log)
    cell.append_run_log("### b\n", log)
    text = log.read_text()
    assert text.startswith("# Run log") and text.count("### ") == 2
