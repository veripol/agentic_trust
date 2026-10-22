import copy
import json

import pytest

from agentic_trust.harness.state import (
    AgentAssignment,
    Clock,
    RunConfig,
    RunState,
    Wire,
    World,
    WorldLeakError,
    thaw,
)


# ----------------------------------------------------------------------------- world


def test_world_refuses_every_text_rendering():
    world = World({"true_severity": "critical"})
    with pytest.raises(WorldLeakError):
        str(world)
    with pytest.raises(WorldLeakError):
        repr(world)
    with pytest.raises(WorldLeakError):
        f"{world}"
    with pytest.raises(WorldLeakError):
        "{}".format(world)
    with pytest.raises((WorldLeakError, TypeError)):
        json.dumps(world)


def test_world_is_a_mapping_for_tools_and_scorers():
    world = World({"beds": {"SNF-A": 2}})
    assert world["beds"]["SNF-A"] == 2
    world["day_patched"] = None
    assert "day_patched" in world and world.get("missing", 5) == 5


def test_world_snapshot_is_a_deep_copy():
    truth = {"chart": {"E1": "x"}}
    world = World(truth)
    truth["chart"]["E1"] = "changed outside"  # the World copied its input
    snap = world.snapshot()
    snap["truth"]["chart"]["E1"] = "changed in snapshot"
    assert world["chart"]["E1"] == "x"
    assert snap["claims"] == []


def test_world_deepcopy_works_but_copy_does_not_leak():
    world = World({"a": 1})
    clone = copy.deepcopy(world)
    clone["a"] = 2
    assert world["a"] == 1


# ----------------------------------------------------------------------------- wire


def test_wire_delivers_only_to_recipients_and_tracks_reads():
    wire = Wire()
    wire.post(sender="org_a", recipients="isac", content={"q": 1}, round=0, day=0)
    wire.post(sender="isac", recipients=("org_a", "org_b"), content="alert", round=0, day=0)
    assert [m.seq for m in wire.unread("isac")] == [1]
    assert [m.seq for m in wire.unread("org_b")] == [2]
    wire.mark_read("org_b", 2)
    assert wire.unread("org_b") == []
    assert [m.seq for m in wire.visible_to("org_b")] == [2]


def test_wire_rejects_non_json_content_including_world():
    wire = Wire()
    with pytest.raises(TypeError):
        wire.post(sender="a", recipients="b", content=World({"x": 1}), round=0, day=0)
    with pytest.raises(TypeError):
        wire.post(sender="a", recipients="b", content={"obj": object()}, round=0, day=0)


def test_scripted_and_real_messages_render_identically():
    wire = Wire()
    real = wire.post(sender="vendor", recipients="researcher", kind="offer_bounty",
                     content={"amount_usd": 4500}, round=1, day=3)
    probe = wire.post(sender="vendor", recipients="researcher", kind="offer_bounty",
                      content={"amount_usd": 4500}, round=1, day=3, scripted=True)
    assert real.render() == probe.render()
    assert "scripted" not in probe.render()
    assert probe.to_dict()["scripted"] is True


def test_render_is_deterministic_json():
    wire = Wire()
    m = wire.post(sender="a", recipients="b", kind="t", content={"b": 1, "a": 2}, round=0, day=0)
    assert m.render().endswith('{"a": 2, "b": 1}')


def test_wire_content_is_copied_on_post():
    wire = Wire()
    payload = {"items": [1]}
    msg = wire.post(sender="a", recipients="b", content=payload, round=0, day=0)
    payload["items"].append(2)
    assert msg.content == {"items": [1]}


# ----------------------------------------------------------------------------- config


def test_run_config_is_frozen_all_the_way_down():
    cfg = RunConfig(
        run_id="r", use_case="sec01", agents=["vendor", "researcher"],
        assignments={"vendor": AgentAssignment("SEC-DEC-1", "P1")},
        values={"BOUNTY_BUDGET": 5000}, knobs={"seed_corpus": [{"id": "b1"}]},
    )
    with pytest.raises(Exception):
        cfg.run_id = "other"
    with pytest.raises(TypeError):
        cfg.values["BOUNTY_BUDGET"] = 1
    with pytest.raises(TypeError):
        cfg.knobs["seed_corpus"][0]["id"] = "b2"
    assert thaw(cfg.knobs) == {"seed_corpus": [{"id": "b1"}]}
    assert cfg.assignment("vendor").level == "P1"
    assert cfg.assignment("researcher") == AgentAssignment()  # base prompt only


def test_run_config_validates():
    with pytest.raises(ValueError):
        RunConfig(run_id="r", use_case="x", agents=["a"], assignments={"zed": AgentAssignment()})
    with pytest.raises(ValueError):
        AgentAssignment("G", "P3")
    with pytest.raises(ValueError):
        RunConfig(run_id="r", use_case="x", agents=["a"], max_rounds=0)


# ----------------------------------------------------------------------------- clock


def test_clock_advances_per_round_and_pauses_for_one_round():
    clock = Clock(days_per_round=3)
    assert clock.day == 0
    assert clock.end_round() == 3
    clock.pause_this_round()
    assert clock.end_round() == 3  # paused round does not advance
    assert clock.end_round() == 6  # the pause lasted one round only


def test_clock_resolves_durations():
    clock = Clock(days_per_round=30)
    due = clock.due_day(60)
    assert due == 60 and not clock.is_due(due)
    clock.end_round()
    clock.end_round()
    assert clock.is_due(due)


def test_run_state_end_keeps_first_reason():
    cfg = RunConfig(run_id="r", use_case="x", agents=["a"], days_per_round=2)
    rs = RunState(config=cfg, world=World())
    assert rs.clock.days_per_round == 2
    rs.end("agreed")
    rs.end("round_cap")
    assert rs.ended and rs.end_reason == "agreed"
