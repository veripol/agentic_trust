"""The local smoke script and the cost estimate, without any model or network calls."""

import json

from agentic_trust.harness.models import ModelTurn, ScriptedModel, scripted_turn
from agentic_trust.runner import smoke_sec01
from agentic_trust.runner.estimate import Profile, ceiling_per_run, simulate_run
from agentic_trust.scenarios.security.sec01.config import make_config
from agentic_trust.scenarios.security.sec01.prompts import build_prompts

from ..scenarios.security.sec01.helpers import report, say, triage


def scripted_factory():
    """A model factory whose model plays an honest SEC-DEC-1 run for every repeat."""
    repeats = iter(range(100))

    def factory(settings):
        cfg = make_config("SEC-DEC-1", "P1", repeat=next(repeats))  # one model per run
        prompts = build_prompts(cfg)
        queues = {prompts["researcher"]: report(cfg), prompts["vendor"]: triage(cfg)}

        def model(*, system, tools, messages):
            q = queues[system]
            nxt = q.pop(0) if q else say("Nothing further.")
            return nxt if isinstance(nxt, ModelTurn) else scripted_turn(nxt)

        return ScriptedModel(model, settings=settings)
    return factory


def test_run_smoke_runs_scores_and_writes_outputs(tmp_path):
    lines = []
    summaries = smoke_sec01.run_smoke(2, scripted_factory(), tmp_path, "test", log=lines.append)
    assert [s["run_id"] for s in summaries] == ["sec01/SEC-DEC-1/P1/main/r00", "sec01/SEC-DEC-1/P1/main/r01"]
    assert all(s["verdict"]["score"] is True for s in summaries)
    saved = json.loads((tmp_path / "run-00.json").read_text())
    assert saved["outputs"]["model"] == "claude-sonnet-5-5" and saved["outputs"]["effort"] == "medium"
    assert {"summary", "outputs", "wire", "transcripts", "prompts"} <= set(saved)
    assert "SEC-DEC-1=True" in lines[0]


def test_main_refuses_without_keys(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)  # no .env here
    for var in ("ANTHROPIC_API_KEY", "LANGSMITH_API_KEY", "LANGSMITH_TRACING"):
        monkeypatch.delenv(var, raising=False)
    assert smoke_sec01.main(["--yes"]) == 1
    assert "Not running" in capsys.readouterr().out


def test_estimate_uses_the_real_prompts_and_the_cap():
    est = smoke_sec01.cost_estimate(20)
    assert est["runs"] == 20 and min(est["prefix_tokens"].values()) > 1000
    typical, long = est["profiles"]["typical"], est["profiles"]["long"]
    assert not typical["hits_token_cap"] and long["hits_token_cap"]
    assert long["capped_tokens_per_run"] >= 50_000 > typical["capped_tokens_per_run"]


def test_simulation_caches_the_prefix_and_writes_only_new_tokens():
    p = Profile("one", rounds=1, calls_per_turn={"a": 2}, output_per_call=100, history_per_call=50,
                inbox_per_turn=10, kickoff=0)
    u = simulate_run({"a": 1000}, p, first_agent="a")
    assert (u.cache_creation_input_tokens, u.cache_read_input_tokens, u.output_tokens) == (1060, 1010, 200)


def test_ceiling_is_an_upper_bound():
    assert ceiling_per_run(1700, max_run_tokens=50_000, max_tokens=16_000, max_requests=200) > 3
