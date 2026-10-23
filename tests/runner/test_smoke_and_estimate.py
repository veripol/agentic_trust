"""The cell runner, the budget guard and the cost estimate, without any model or network calls."""

import json

import pytest

from agentic_trust.harness.models import ModelSettings, ModelTurn, ScriptedModel, Usage, scripted_turn
from agentic_trust.runner import cell, smoke_sec01
from agentic_trust.runner.budget import (
    PROJECT_BUDGET_USD, BudgetedModel, BudgetExceeded, Ledger, worst_call_cost,
)
from agentic_trust.runner.estimate import Profile, ceiling_per_run, simulate_run
from agentic_trust.scenarios.security.sec01.config import make_config
from agentic_trust.scenarios.security.sec01.prompts import build_prompts

from ..scenarios.security.sec01.helpers import report, say, triage

TOKENS = Usage(input_tokens=1000, output_tokens=1000, requests=1)  # $0.002 + $0.01 per call


def scripted_factory(usage=TOKENS):
    """A model factory whose model plays an honest SEC-DEC-1 run for each repeat in turn."""
    repeats = iter(range(100))

    def factory(settings):
        cfg = make_config("SEC-DEC-1", "P1", repeat=next(repeats))
        prompts = build_prompts(cfg)
        queues = {prompts["researcher"]: report(cfg), prompts["vendor"]: triage(cfg)}

        def model(*, system, tools, messages):
            q = queues[system]
            nxt = q.pop(0) if q else say("Nothing further.")
            return nxt if isinstance(nxt, ModelTurn) else scripted_turn(nxt, usage=Usage(**usage.to_dict()))

        return ScriptedModel(model, settings=settings)
    return factory


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger.jsonl")


# --------------------------------------------------------------------------- defaults


def test_repeat_defaults():
    assert cell.DEFAULT_REPEATS == 10 and smoke_sec01.SMOKE_REPEATS == 2
    assert PROJECT_BUDGET_USD == 100.0


# --------------------------------------------------------------------------- the cell runner


def test_run_cell_runs_scores_records_spend_and_writes_outputs(tmp_path, ledger):
    lines = []
    summaries = cell.run_cell("SEC-DEC-1", "P1", "main", 2, model_factory=scripted_factory(), ledger=ledger,
                              out_dir=tmp_path / "out", tags=("smoke",), log=lines.append)
    assert [s["run_id"] for s in summaries] == ["sec01/SEC-DEC-1/P1/main/r00", "sec01/SEC-DEC-1/P1/main/r01"]
    assert all(s["verdict"]["score"] is True for s in summaries)
    requests = sum(s["usage"]["requests"] for s in summaries)
    assert len(ledger.entries()) == requests
    assert ledger.total() == pytest.approx(requests * 0.012) == pytest.approx(sum(s["cost_usd"] for s in summaries))
    saved = json.loads((tmp_path / "out" / "run-00.json").read_text())
    assert saved["outputs"]["model"] == "claude-sonnet-5-5" and saved["outputs"]["effort"] == "medium"
    assert "SEC-DEC-1=True" in lines[0]


def test_budget_guard_stops_the_cell_before_crossing_the_ceiling(tmp_path, ledger):
    ledger.record(run_id="earlier", model="claude-sonnet-5-5", usage={}, cost_usd=99.90)
    lines = []
    summaries = cell.run_cell("SEC-DEC-1", "P1", "main", 3, model_factory=scripted_factory(), ledger=ledger,
                              out_dir=tmp_path / "out", log=lines.append)
    assert summaries == [{"run_id": "sec01/SEC-DEC-1/P1/main/r00", "end_reason": "budget_stop",
                          "error": summaries[0]["error"]}]
    assert "project ceiling" in summaries[0]["error"] and "STOPPED" in lines[0]
    assert ledger.total() <= PROJECT_BUDGET_USD


def test_budgeted_model_checks_before_and_records_after(ledger):
    inner = ScriptedModel([scripted_turn(say("hi"), usage=Usage(**TOKENS.to_dict()))],
                          settings=ModelSettings())
    model = BudgetedModel(inner, ledger, "run-1", limit_usd=1.0)
    model.complete(system="S", tools=[], messages=[{"role": "user", "content": "x"}])
    [entry] = ledger.entries()
    assert entry["run_id"] == "run-1" and entry["cost_usd"] == pytest.approx(0.012)
    assert model.settings is inner.settings  # the harness's model/effort check still sees the real settings

    ledger.record(run_id="x", model="m", usage={}, cost_usd=0.9)
    with pytest.raises(BudgetExceeded):  # 0.912 + a full 16k-token response would pass $1
        model.complete(system="S", tools=[], messages=[])
    assert len(inner.requests) == 1  # the refused call never reached the model


def test_worst_call_cost_covers_a_full_response():
    assert worst_call_cost("claude-sonnet-5-5", 16_000, "", [], []) >= 0.16


def test_budget_check_asks_only_when_the_estimate_could_cross(ledger):
    est = {"profiles": {"typical": {"cost_cell_usd": 3.0}, "long": {"cost_cell_usd": 12.0}}}
    assert cell.budget_check(est, ledger) == (0.0, 12.0, False)
    ledger.record(run_id="x", model="m", usage={}, cost_usd=90.0)
    assert cell.budget_check(est, ledger)[2] is True


# --------------------------------------------------------------------------- the CLI


@pytest.fixture
def keys(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env; the default ledger lands in tmp_path
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("AGENTIC_TRUST_LEDGER", raising=False)
    return tmp_path


def test_main_refuses_without_keys(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    for var in ("ANTHROPIC_API_KEY", "LANGSMITH_API_KEY", "LANGSMITH_TRACING"):
        monkeypatch.delenv(var, raising=False)
    assert smoke_sec01.main(["--yes"]) == 1
    assert "Not running" in capsys.readouterr().out


def test_main_refuses_once_the_ceiling_is_reached(keys, capsys):
    Ledger().record(run_id="x", model="m", usage={}, cost_usd=100.0)
    assert cell.main(["SEC-DEC-1", "P1", "--yes"]) == 1
    assert "ceiling is already reached" in capsys.readouterr().out


def test_crossing_the_ceiling_asks_even_with_yes(keys, monkeypatch, capsys):
    Ledger().record(run_id="x", model="m", usage={}, cost_usd=98.0)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "n")
    assert cell.main(["SEC-DEC-1", "P1", "--yes"]) == 1
    assert asked and "anyway" in asked[0]
    out = capsys.readouterr().out
    assert "WARNING" in out and "spend so far $98.00 of $100" in out


def test_smoke_always_asks_and_prints_the_estimate(keys, monkeypatch, capsys):
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "n")
    assert smoke_sec01.main(["--yes"]) == 1  # --yes does not skip the smoke test's question
    out = capsys.readouterr().out
    assert asked == ["Proceed? [y/N] "] and "2 run(s)" in out and "estimate if runs are typical" in out


# --------------------------------------------------------------------------- the estimate


def test_estimate_uses_the_real_prompts_and_no_profile_hits_the_cap():
    est = cell.cell_estimate("SEC-DEC-1", "P1", "main", cell.DEFAULT_REPEATS)
    assert est["runs"] == 10 and min(est["prefix_tokens"].values()) > 1000
    assert not any(p["hits_token_cap"] for p in est["profiles"].values())
    assert max(p["capped_tokens_per_run"] for p in est["profiles"].values()) < 150_000


def test_simulation_caches_the_prefix_and_writes_only_new_tokens():
    p = Profile("one", rounds=1, calls_per_turn={"a": 2}, output_per_call=100, history_per_call=50,
                inbox_per_turn=10, kickoff=0)
    u = simulate_run({"a": 1000}, p, first_agent="a")
    assert (u.cache_creation_input_tokens, u.cache_read_input_tokens, u.output_tokens) == (1060, 1010, 200)


def test_ceiling_is_an_upper_bound():
    assert ceiling_per_run(1700, max_run_tokens=150_000, max_tokens=16_000, max_requests=200) > 8
