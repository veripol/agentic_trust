"""Reading saved runs back: the raw refusal and each agent's first response. No API calls."""

import json

from agentic_trust.harness.models import ModelTurn, ScriptedModel, Usage, scripted_turn
from agentic_trust.runner import cell, inspect_run
from agentic_trust.runner.budget import Ledger
from agentic_trust.scenarios.security.sec01.config import make_config
from agentic_trust.scenarios.security.sec01.prompts import build_prompts

from ..scenarios.security.sec01.helpers import report, say, triage

API_REFUSAL = ModelTurn(
    content=[], stop_reason="refusal", usage=Usage(input_tokens=1900, output_tokens=0, requests=1),
    stop_details={"type": "refusal", "category": "cyber", "explanation": "declined"},
    model="claude-sonnet-5-5", request_id="req_123",
)


def factory(researcher_first):
    repeats = iter(range(100))

    def make(settings):
        cfg = make_config("SEC-DEC-1", "P1", repeat=next(repeats))
        prompts = build_prompts(cfg)
        opening = [researcher_first] if researcher_first is not None else []
        queues = {prompts["researcher"]: opening + report(cfg), prompts["vendor"]: triage(cfg)}

        def model(*, system, tools, messages):
            q = queues[system]
            nxt = q.pop(0) if q else say("Nothing further.")
            return nxt if isinstance(nxt, ModelTurn) else scripted_turn(nxt)

        return ScriptedModel(model, settings=settings)
    return make


def test_a_refusal_is_saved_raw_and_logged_with_its_category(tmp_path):
    lines = []
    cell.run_cell("SEC-DEC-1", "P1", "main", 1, model_factory=factory(API_REFUSAL),
                  ledger=Ledger(tmp_path / "l.jsonl"), out_dir=tmp_path / "cell" / "20261023-120000", log=lines.append)
    assert "category='cyber'" in lines[0] and "output_tokens=0" in lines[0] and "req_123" in lines[0]
    saved = json.loads((tmp_path / "cell" / "20261023-120000" / "run-00.json").read_text())
    [r] = saved["outputs"]["world"]["truth"]["refusals"]
    assert r["stop_reason"] == "refusal" and r["content"] == [] and r["usage"]["output_tokens"] == 0

    out = []
    inspect_run.report(tmp_path / "cell" / "20261023-120000" / "run-00.json", out=out.append)
    text = "\n".join(out)
    assert "end_reason: refusal" in text
    assert '"category": "cyber"' in text and "request_id: req_123" in text
    assert "(no content blocks: the API returned an empty content list)" in text
    assert "vendor's first response:\n   (never called" in text


def test_first_responses_show_text_and_tool_calls(tmp_path):
    cell.run_cell("SEC-DEC-1", "P1", "main", 1, model_factory=factory(None),
                  ledger=Ledger(tmp_path / "l.jsonl"), out_dir=tmp_path / "c" / "1", log=lambda _: None)
    out = []
    inspect_run.report(tmp_path / "c" / "1" / "run-00.json", out=out.append)
    text = "\n".join(out)
    assert "researcher's first response:\n   tool_use prepare_evidence: {\"redaction_level\": 1}" in text
    assert "vendor's first response:\n   tool_use reproduce_issue" in text


def test_old_run_files_without_raw_refusal_content_still_read(tmp_path):
    path = tmp_path / "run-00.json"
    path.write_text(json.dumps({
        "outputs": {"end_reason": "refusal", "rounds": 0, "day": 0,
                    "usage": {"requests": 1, "output_tokens": 0},
                    "world": {"truth": {"refusals": [{"agent": "researcher", "round": 0,
                                                       "stop_details": {"category": "cyber"}}]}}},
        "transcripts": {"researcher": [{"role": "user", "content": "kickoff"},
                                       {"role": "assistant", "content": []}], "vendor": []},
    }))
    out = []
    inspect_run.report(path, out=out.append)
    text = "\n".join(out)
    assert '"category": "cyber"' in text and "(no content blocks" in text


def test_main_picks_the_newest_cell(tmp_path, capsys):
    for stamp in ("20261022-090000", "20261023-090000"):
        d = tmp_path / "smoke" / "SEC-DEC-1-P1-main" / stamp
        d.mkdir(parents=True)
        (d / "run-00.json").write_text(json.dumps({
            "outputs": {"end_reason": stamp, "rounds": 0, "day": 0, "usage": {"requests": 0, "output_tokens": 0},
                        "world": {"truth": {}}}, "transcripts": {}}))
    assert inspect_run.main([str(tmp_path / "smoke")]) == 0
    out = capsys.readouterr().out
    assert "20261023-090000" in out and "end_reason: 20261022" not in out
