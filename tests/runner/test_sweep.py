"""The SEC-01 sweep: plan, resume without paying twice, the ceiling, and the digest. No API calls."""

import json

import pytest

from agentic_trust.harness.models import ModelTurn, ScriptedModel, Usage, scripted_turn
from agentic_trust.runner import cell, sweep
from agentic_trust.runner.budget import Ledger
from agentic_trust.scenarios.security.sec01.config import SHARED_P0_GOALS
from agentic_trust.scenarios.security.sec01.prompts import build_prompts

from ..scenarios.security.sec01.helpers import report, say, triage

COST = Usage(input_tokens=1000, output_tokens=1000, requests=1)  # $0.012 per call
REFUSAL = ModelTurn(content=[], stop_reason="refusal", usage=Usage(input_tokens=1900, requests=1),
                    stop_details={"type": "refusal", "category": "cyber"})


class Models:
    """A model factory that plays an honest run for whichever config the runner just built.
    ``refuse`` lists the run numbers (1-based) that refuse; ``interrupt_at`` raises Ctrl-C there."""

    def __init__(self, monkeypatch, refuse=(), interrupt_at=None):
        self.made = 0
        self.refuse, self.interrupt_at = set(refuse), interrupt_at
        self.current = None
        real = cell.sec01.make_config

        def tracking(*a, **k):
            self.current = real(*a, **k)
            return self.current

        monkeypatch.setattr(cell.sec01, "make_config", tracking)

    def __call__(self, settings):
        self.made += 1
        n, cfg = self.made, self.current
        if n == self.interrupt_at:
            raise KeyboardInterrupt
        p = build_prompts(cfg)
        queues = {p["researcher"]: report(cfg), p["vendor"]: triage(cfg)}

        def model(*, system, tools, messages):
            if n in self.refuse:
                return REFUSAL
            q = queues[system]
            nxt = q.pop(0) if q else say("Nothing further.")
            return nxt if isinstance(nxt, ModelTurn) else scripted_turn(nxt, usage=Usage(**COST.to_dict()))

        return ScriptedModel(model, settings=settings)


SMALL = [sweep.CellSpec(None, "P0", "main"), sweep.CellSpec("SEC-DEC-1", "P1", "main"),
         sweep.CellSpec("SEC-BA-4", "P1", "main")]


def go(tmp_path, models, cells=SMALL, target=2, **kw):
    lines = []
    outcome = sweep.run_sweep(cells, target, root=tmp_path / "runs", ledger=Ledger(tmp_path / "ledger.jsonl"),
                              model_factory=models, log=lines.append, **kw)
    return outcome, lines


def saved(tmp_path):
    return sorted(str(p.relative_to(tmp_path)) for p in (tmp_path / "runs").rglob("run-*.json"))


# --------------------------------------------------------------------------- the plan


def test_plan_covers_every_goal_level_and_arm_with_one_shared_p0():
    cells = sweep.plan()
    names = [c.name for c in cells]
    assert len(cells) == 25 and len(set(names)) == 25 and names[0] == "base-P0-main"
    own_p0 = sorted(c.name for c in cells if c.level == "P0" and c.goal)
    assert own_p0 == ["SEC-BA-3-P0-main", "SEC-BA-4-P0-main", "SEC-DEC-2-P0-control", "SEC-ES-2-P0-control"]
    assert set(SHARED_P0_GOALS) == {"SEC-DEC-1", "SEC-DEC-2", "SEC-LD-2", "SEC-LD-3", "SEC-ES-1", "SEC-ES-2"}
    for goal in ("SEC-DEC-1", "SEC-BA-4", "SEC-ES-2"):
        assert {c.level for c in cells if c.goal == goal and c.arm == "main"} >= {"P1", "P2"}
    goal_order = [c.goal for c in cells[1:]]
    assert goal_order == sorted(goal_order, key=list(cell.sec01.GOALS).index)  # goal by goal


# --------------------------------------------------------------------------- running and resuming


def test_a_sweep_runs_every_cell_then_a_rerun_runs_nothing(tmp_path, monkeypatch):
    models = Models(monkeypatch)
    outcome, lines = go(tmp_path, models)
    assert outcome == "complete" and models.made == 6 and len(saved(tmp_path)) == 6
    assert any("sweep: 6 runs, 0 refused, $" in l and "cell 3/3" in l for l in lines)
    for c in SMALL:
        stats = json.loads((tmp_path / "runs" / c.name / "cell.json").read_text())
        assert stats["completed"] == 2 and stats["reached_target"]

    again = Models(monkeypatch)
    outcome, lines = go(tmp_path, again)
    assert outcome == "complete" and again.made == 0 and lines == []


def test_an_interrupted_sweep_resumes_without_paying_twice(tmp_path, monkeypatch):
    first = Models(monkeypatch, interrupt_at=4)  # Ctrl-C as the 4th run starts
    with pytest.raises(KeyboardInterrupt):
        go(tmp_path, first)
    assert len(saved(tmp_path)) == 3  # base-P0 x2 and the first SEC-DEC-1 run
    spent = Ledger(tmp_path / "ledger.jsonl").total()

    second = Models(monkeypatch)
    outcome, _ = go(tmp_path, second)
    assert outcome == "complete" and second.made == 3 and len(saved(tmp_path)) == 6
    run_ids = [json.loads((tmp_path / p).read_text())["config"]["run_id"] for p in saved(tmp_path)]
    assert len(run_ids) == len(set(run_ids)) == 6  # no slot was run twice
    costs = sum(json.loads((tmp_path / p).read_text())["summary"]["cost_usd"] for p in saved(tmp_path))
    assert spent > 0 and Ledger(tmp_path / "ledger.jsonl").total() == pytest.approx(costs)  # every run paid once


def test_refusals_resume_mid_slot_and_an_exhausted_cell_is_complete(tmp_path, monkeypatch):
    cells = [sweep.CellSpec("SEC-DEC-1", "P1", "main")]
    first = Models(monkeypatch, refuse={1, 2, 3, 4})  # every attempt of a 2-run cell refuses
    go(tmp_path, cells=cells, models=first)
    st = sweep.status(cells[0], tmp_path / "runs", 2)
    assert (st.completed, st.attempts, st.complete) == (0, 4, True)  # cap reached: not retried forever
    again = Models(monkeypatch)
    go(tmp_path, cells=cells, models=again)
    assert again.made == 0


def test_resume_continues_the_same_slot_after_a_refusal(tmp_path, monkeypatch):
    cells = [sweep.CellSpec("SEC-DEC-1", "P1", "main")]
    first = Models(monkeypatch, refuse={1}, interrupt_at=3)  # r00 refused, r00/a1 done, then Ctrl-C
    with pytest.raises(KeyboardInterrupt):
        go(tmp_path, cells=cells, models=first)
    assert cell.resume_point(cell.saved_summaries(tmp_path / "runs" / cells[0].name)) == (1, 0)
    go(tmp_path, cells=cells, models=Models(monkeypatch))
    ids = sorted(json.loads((tmp_path / p).read_text())["config"]["run_id"] for p in saved(tmp_path))
    assert ids == ["sec01/SEC-DEC-1/P1/main/r00", "sec01/SEC-DEC-1/P1/main/r00/a1", "sec01/SEC-DEC-1/P1/main/r01"]


def test_the_ceiling_stops_the_sweep_and_the_digest_says_partial(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.record(run_id="earlier", model="m", usage={}, cost_usd=99.80)
    outcome, lines = go(tmp_path, Models(monkeypatch))
    assert outcome == "budget_stop" and ledger.total() <= 100.0
    assert any("Stopped at the $100.00 spend limit" in l for l in lines)
    out = sweep.write_digest(tmp_path / "runs", tmp_path / "docs" / "results" / "sec01.md", SMALL, 2)
    assert "> PARTIAL: 0 of 3 cells complete." in out.read_text()


# --------------------------------------------------------------------------- the digest


def test_the_digest_scores_the_shared_p0_cell_for_each_shared_goal(tmp_path, monkeypatch):
    go(tmp_path, Models(monkeypatch))
    md = sweep.write_digest(tmp_path / "runs", tmp_path / "docs" / "results" / "sec01.md", SMALL, 2).read_text()
    assert "PARTIAL" not in md
    row = next(l for l in md.splitlines() if l.startswith("| SEC-DEC-1 |"))
    assert [c.strip() for c in row.strip("|").split("|")][3:5] == ["2/2", "2/2"]  # P0 from the shared cell
    shared = [l for l in md.split("## Sources")[1].splitlines() if "shared P0 (6 goals)" in l]
    assert len(shared) == 2 and "SEC-DEC-1 pass" in shared[0]
    ba4 = next(l for l in md.splitlines() if l.startswith("| SEC-BA-4 |"))
    assert [c.strip() for c in ba4.strip("|").split("|")][3] == "—"  # BA-4 has its own P0, not run here


def test_cli_plan_and_report_only_need_no_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for var in ("ANTHROPIC_API_KEY", "LANGSMITH_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert sweep.main(["--plan"]) == 0
    out = capsys.readouterr().out
    assert "25 cells" in out and "250 runs to go" in out and "if runs are typical" in out
    go(tmp_path, Models(monkeypatch))
    assert sweep.main(["--report-only", "--root", "runs", "--digest", "docs/results/sec01.md"]) == 0
    assert (tmp_path / "docs" / "results" / "sec01.md").read_text().startswith("# SEC-01")


def test_cli_refuses_without_keys_before_asking(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for var in ("ANTHROPIC_API_KEY", "LANGSMITH_API_KEY", "LANGSMITH_TRACING"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("asked")))
    assert sweep.main([]) == 1 and "Not running" in capsys.readouterr().out


def test_resume_after_a_refusal_keeps_the_refused_file_and_retries_that_slot(tmp_path, monkeypatch):
    cells = [sweep.CellSpec("SEC-DEC-1", "P1", "main")]
    first = Models(monkeypatch, refuse={1}, interrupt_at=2)  # r00 refused, Ctrl-C before its retry
    with pytest.raises(KeyboardInterrupt):
        go(tmp_path, cells=cells, models=first)
    assert cell.resume_point(cell.saved_summaries(tmp_path / "runs" / cells[0].name)) == (0, 1)
    go(tmp_path, cells=cells, models=Models(monkeypatch))
    runs = {json.loads((tmp_path / p).read_text())["config"]["run_id"]: json.loads((tmp_path / p).read_text())
            for p in saved(tmp_path)}
    assert sorted(runs) == ["sec01/SEC-DEC-1/P1/main/r00", "sec01/SEC-DEC-1/P1/main/r00/a1",
                            "sec01/SEC-DEC-1/P1/main/r01"]
    assert runs["sec01/SEC-DEC-1/P1/main/r00"]["summary"]["end_reason"] == "refusal"  # not overwritten
    stats = json.loads((tmp_path / "runs" / cells[0].name / "cell.json").read_text())
    assert (stats["attempts"], stats["refused"], stats["completed"]) == (3, 1, 2)


# --------------------------------------------------------------------------- --budget


def test_spend_limit_is_spent_plus_budget_within_the_project_ceiling():
    assert sweep.spend_limit(3.0, 20) == 23.0
    assert sweep.spend_limit(95.0, 20) == 100.0  # the project ceiling still holds
    assert sweep.spend_limit(3.0, None) == 100.0


def test_a_budget_stops_the_sweep_without_overshooting_and_a_rerun_finishes_without_repaying(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.record(run_id="smoke", model="m", usage={}, cost_usd=1.00)
    # Each scripted run costs about $0.08. The reserve before each run is raised to twice the
    # per-call guard's worst case (~$0.33), so runs 1-4 start and run 5 does not.
    budget = 0.60
    outcome, lines = go(tmp_path, Models(monkeypatch), limit_usd=sweep.spend_limit(ledger.total(), budget),
                        reserve_per_run=0.10)
    new_spend = ledger.total() - 1.00
    assert outcome == "budget_stop" and 0 < new_spend <= budget
    assert any("Stopped at the $1.60 spend limit" in l for l in lines)
    assert not any("budget guard" in l for l in lines)  # stopped between runs, not by refusing a call mid-run
    done_before = {c.name for c in SMALL if sweep.status(c, tmp_path / "runs", 2).complete}
    files_before = set(saved(tmp_path))
    assert done_before and len(done_before) < len(SMALL)  # some cells finished, not all

    again = Models(monkeypatch)
    outcome, _ = go(tmp_path, again, limit_usd=sweep.spend_limit(ledger.total(), 20), reserve_per_run=0.10)
    assert outcome == "complete" and files_before <= set(saved(tmp_path)) and len(saved(tmp_path)) == 6
    assert again.made == 6 - len(files_before)  # only the runs not already saved were paid for
    costs = sum(json.loads((tmp_path / p).read_text())["summary"]["cost_usd"] for p in saved(tmp_path))
    assert ledger.total() == pytest.approx(1.00 + costs)  # every paid call belongs to a saved run: nothing paid twice


def test_plan_shows_the_budget_limit(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Ledger().record(run_id="smoke", model="m", usage={}, cost_usd=1.25)
    assert sweep.main(["--plan", "--budget", "20"]) == 0
    assert "This sweep stops at a ledger total of $21.25: up to $20.00 new spend." in capsys.readouterr().out
    with pytest.raises(SystemExit):
        sweep.main(["--plan", "--budget", "0"])


def test_without_the_reserve_the_call_guard_still_holds_the_limit(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    outcome, _ = go(tmp_path, Models(monkeypatch), limit_usd=0.40, reserve_per_run=0.0)
    assert outcome == "budget_stop" and ledger.total() <= 0.40  # cut off mid-run, but never over the limit
