"""Tool-usage counts from saved runs: never called vs called but failed. No API calls."""

from agentic_trust.runner import cell, tool_usage
from agentic_trust.runner.budget import Ledger

from ..scenarios.security.sec01.helpers import T, report, say, triage, use
from .test_sweep import Models


def test_counts_separate_never_called_from_called_and_failed(tmp_path, monkeypatch):
    models = Models(monkeypatch)
    cell.run_cell("SEC-DEC-2", "P1", "main", 2, model_factory=models, ledger=Ledger(tmp_path / "l.jsonl"),
                  out_dir=tmp_path / "runs" / "SEC-DEC-2-P1-main", log=lambda _: None)
    # Add a run in which the vendor tried commit_patch_eta with a bad argument.
    import json
    src = tmp_path / "runs" / "SEC-DEC-2-P1-main" / "run-00.json"
    data = json.loads(src.read_text())
    data["outputs"]["tool_log"].append({"seq": 99, "round": 0, "day": 0, "agent": "vendor", "tool": "commit_patch_eta",
                                        "args": {"ticket_id": T, "eta_days": "soon", "basis": "triage_estimate"},
                                        "result": {"error": "invalid arguments: eta_days: 'soon' is not of type 'integer'"},
                                        "is_error": True, "scripted": False, "ignored_args": []})
    src.write_text(json.dumps(data))
    lines = []
    tool_usage.report(tmp_path / "runs", "commit_patch_eta", out=lines.append)
    text = "\n".join(lines)
    assert "== SEC-DEC-2-P1-main: 2 run(s); ends: quiescent 2" in text
    assert "vendor     commit_patch_eta             ok   0  error   1" in text
    assert "not of type 'integer'" in text and "commit_patch_eta: called in 1 of 2 run(s)" in text
    assert "vendor     report_reproduction_status   ok   2  error   0" in text
