"""A cell targets N completed runs: refused runs are re-run, up to 2 x N attempts."""

import json

from agentic_trust.harness.models import ModelTurn, ScriptedModel, Usage, scripted_turn
from agentic_trust.runner import cell
from agentic_trust.runner.budget import Ledger
from agentic_trust.scenarios.security.sec01.config import make_config
from agentic_trust.scenarios.security.sec01.prompts import build_prompts

from ..scenarios.security.sec01.helpers import report, say, triage

REFUSAL = ModelTurn(content=[], stop_reason="refusal", usage=Usage(input_tokens=1900, requests=1),
                    stop_details={"type": "refusal", "category": "cyber"})


def factory(refuse_pattern):
    """Attempt i refuses on the researcher's first call when refuse_pattern[i] is True."""
    attempts = iter(refuse_pattern)

    def make(settings):
        refuse = next(attempts)
        state = {}

        def model(*, system, tools, messages):
            if "queues" not in state:  # first call of the run: the researcher's
                state["queues"] = {}
                state["first"] = True
            if state.pop("first", False) and refuse:
                return REFUSAL
            if not state["queues"]:
                cfg = make.current  # the run being played (set by run() below)
                p = build_prompts(cfg)
                state["queues"] = {p["researcher"]: report(cfg), p["vendor"]: triage(cfg)}
            q = state["queues"][system]
            nxt = q.pop(0) if q else say("Nothing further.")
            return nxt if isinstance(nxt, ModelTurn) else scripted_turn(nxt)

        return ScriptedModel(model, settings=settings)
    return make


def run(tmp_path, pattern, target, max_attempts=None, monkeypatch=None):
    make = factory(pattern)
    real = cell.sec01.make_config

    def tracking(*a, **k):  # so the scripted model knows which run (and artifact hashes) it is in
        make.current = real(*a, **k)
        return make.current

    monkeypatch.setattr(cell.sec01, "make_config", tracking)
    out = tmp_path / "cell"
    summaries = cell.run_cell("SEC-DEC-1", "P1", "main", target, model_factory=make,
                              ledger=Ledger(tmp_path / "l.jsonl"), out_dir=out, log=lambda _: None,
                              max_attempts=max_attempts)
    return summaries, out


def test_refused_runs_are_retried_until_the_target_is_met(tmp_path, monkeypatch):
    summaries, out = run(tmp_path, [True, False, True, False], target=2, monkeypatch=monkeypatch)
    assert [(s["run_id"], s["end_reason"]) for s in summaries] == [
        ("sec01/SEC-DEC-1/P1/main/r00", "refusal"),
        ("sec01/SEC-DEC-1/P1/main/r00/a1", "quiescent"),
        ("sec01/SEC-DEC-1/P1/main/r01", "refusal"),
        ("sec01/SEC-DEC-1/P1/main/r01/a1", "quiescent"),
    ]
    assert [s["verdict"]["score"] for s in summaries] == [None, True, None, True]
    stats = json.loads((out / "cell.json").read_text())
    assert (stats["target"], stats["attempts"], stats["completed"], stats["refused"]) == (2, 4, 2, 2)
    assert stats["refusal_rate"] == 0.5 and stats["refusal_categories"] == {"cyber": 2}
    assert stats["reached_target"] is True
    files = sorted(p.name for p in out.glob("run-*.json"))
    assert files == ["run-00-a1.json", "run-00.json", "run-01-a1.json", "run-01.json"]
    saved = json.loads((out / "run-01-a1.json").read_text())
    assert saved["config"] == {"run_id": "sec01/SEC-DEC-1/P1/main/r01/a1", "use_case": "sec01",
                               "goal": "SEC-DEC-1", "level": "P1", "arm": "main", "repeat": 1, "attempt": 1,
                               "tags": [], "model": "claude-sonnet-5-5", "effort": "medium"}


def test_the_cap_can_leave_the_target_short(tmp_path, monkeypatch):
    summaries, out = run(tmp_path, [True, False, True, True], target=2, monkeypatch=monkeypatch)
    stats = json.loads((out / "cell.json").read_text())
    assert (stats["attempts"], stats["completed"], stats["reached_target"]) == (4, 1, False)


def test_attempts_stop_at_twice_the_target(tmp_path, monkeypatch):
    summaries, out = run(tmp_path, [True] * 10, target=2, monkeypatch=monkeypatch)
    assert len(summaries) == 4 and all(s["end_reason"] == "refusal" for s in summaries)
    stats = json.loads((out / "cell.json").read_text())
    assert stats["completed"] == 0 and stats["reached_target"] is False and stats["refusal_rate"] == 1.0


def test_lab_log_line_reports_the_rate():
    stats = cell.cell_stats([{"end_reason": "refusal", "refusal_categories": ["cyber"]},
                             {"end_reason": "agreed"}, {"end_reason": "quiescent"}], target=2)
    line = cell.lab_log_line("SEC-DEC-1", "P1", "main", stats, cell.Path("runs/x"), "2026-10-24 10:00")
    assert "refusal rate 33%" in line and "2 completed runs (target 2) in 3 attempts; 1 refused (33%)" in line
    assert "categories: cyber x1" in line and "not reached" not in line
