"""Deterministic acceptance-boundary probe on the real child event relay.

No worker, provider, gateway, clock race, or runtime patch is involved. The
only controlled variable is whether note_own_dispatch opens the reserved part
before or after the same producer callbacks. The differential test is expected
to be RED on frozen product 1fc492263ebcacdc9d75537f4ed1203622914dc3.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl
from tools.delegate_tool import _build_child_progress_callback

pytestmark = pytest.mark.unified_card

HANDOFF = {
    "work": "verify public task activity",
    "outcome": "a checked result",
    "steps": ["inspect public inputs", "check the result"],
}
GOALS = ("Inspect public alpha inputs", "Check public beta inputs")
PLANS = (("inspect alpha", "check alpha"), ("inspect beta", "check beta"))


@pytest.fixture(autouse=True)
def isolated_cards():
    # tests/conftest.py isolates CLOVER_HOME before collection and per test.
    ac.reset_for_tests()
    ac.set_style_for_tests("unified")
    yield
    ac.reset_for_tests()


def _reserve(did, count):
    parent = SimpleNamespace(tool_progress_callback=lambda *a, **kw: None,
                             _delegate_spinner=None)
    assert fl.reserve_ack_part(did, HANDOFF, GOALS[:count]) is True
    assert ac.owns_delegation(did)
    # Reservation claims event ownership but is NOT acceptance or visibility.
    assert did not in ac._PARTS
    return parent


def _accept(parent, did, count):
    assert did not in ac._PARTS
    fl.note_own_dispatch(parent, did, goals=GOALS[:count],
                         subagent_ids=[f"public-child-{i}" for i in range(count)],
                         handoff=HANDOFF, child_steps=PLANS[:count] if count > 1 else (),
                         titles=[f"public task {i}" for i in range(count)])
    assert did in ac._PARTS
    card = ac.card_for(fl.own_dispatch_receipts(parent))
    assert card is not None
    return card


def _callback(parent, did, index, count):
    cb = _build_child_progress_callback(
        index, GOALS[index], parent, count,
        subagent_id=f"public-child-{index}", depth=0,
        model="claude-sonnet-5-5" if index == 0 else "gpt-6-sol",
        session_ref={"delegation_id": did, "title": f"public task {index}",
                     "provider": "anthropic" if index == 0 else "openai"},
    )
    assert cb is not None
    return cb


def _activity(cb, index):
    cb("subagent.start", preview=GOALS[index])
    cb("tool.started", "terminal", "inspect public inputs",
       {"command": "printf public"})
    cb("tool.completed", "terminal", duration=0.125, is_error=False)
    # Leave one genuinely open call as well as one completed recent call.
    cb("tool.started", "read_file", "public.txt", {"path": "public.txt"})


def _snapshot(obj):
    return {"model": obj.model, "calls": obj.calls, "open": list(obj.open),
            "recent": list(obj.recent), "state": obj.state,
            "steps": obj.steps, "sid": getattr(obj, "sid", "")}


def test_after_open_real_relay_positive_control():
    parent = _reserve("control", 1)
    card = _accept(parent, "control", 1)
    _activity(_callback(parent, "control", 0, 1), 0)
    state = _snapshot(card.parts[0].checklist)
    print("AFTER_OPEN_CONTROL=" + json.dumps(state))
    assert state["model"] == "Sonnet 5.5"
    assert state["calls"] == 2
    assert len(state["open"]) == 1
    assert len(state["recent"]) == 1
    assert state["state"] == ac.RUNNING


def test_reserved_before_acceptance_matches_identical_after_open(monkeypatch):
    original = ac.on_child_event
    events = []

    def observe(event_type, **kw):
        # Observe the actual normalized payload, then execute the actual consumer.
        events.append((event_type, kw.copy()))
        return original(event_type, **kw)

    monkeypatch.setattr(ac, "on_child_event", observe)
    outcomes = {}
    traces = {}
    for early in (False, True):
        did = "early" if early else "late"
        parent = _reserve(did, 2)
        callbacks = [_callback(parent, did, i, 2) for i in range(2)]
        card = None if early else _accept(parent, did, 2)
        start = len(events)
        for i, cb in enumerate(callbacks):
            _activity(cb, i)
        # Child zero finishes before acceptance on the early leg; replay must
        # preserve that fence, not resurrect it when later activity arrives.
        callbacks[0]("subagent.complete", status="completed", summary="alpha checked")
        if early:
            assert did not in ac._PARTS
            assert ac.owns_delegation(did)
            card = _accept(parent, did, 2)
        assert card is not None
        kids = card.parts[0].checklist.kids
        assert kids[0].state != ac.RUNNING
        fenced = _snapshot(kids[0])
        callbacks[0]("tool.started", "terminal", "late alpha activity", {"command": "true"})
        assert _snapshot(kids[0]) == fenced
        assert card.parts[0].checklist.state == ac.RUNNING
        assert [k.index for k in kids] == [0, 1]
        assert [k.steps[0][0] for k in kids] == [p[0] for p in PLANS]
        outcomes[did] = [_snapshot(k) for k in kids]
        traces[did] = [(et, {k: v for k, v in kw.items() if k != "delegation_id"})
                       for et, kw in events[start:]]
        print(did.upper() + "_FINAL=" + json.dumps(outcomes[did]))
        print(did.upper() + "_CARD=" + card.render())
    assert traces["early"] == traces["late"]
    assert [et for et, _ in traces["early"]] == [
        "subagent.start", "subagent.tool", "subagent.tool_done", "subagent.tool",
        "subagent.start", "subagent.tool", "subagent.tool_done", "subagent.tool",
        "subagent.complete", "subagent.tool",
    ]
    assert [kw["task_index"] for _, kw in traces["early"]] == [0]*4 + [1]*4 + [0]*2
    print("NORMALIZED_EVENTS=" + json.dumps(traces["early"]))
    # The successful control supplies the oracle: do not assert fabricated
    # expected tool phrases or substitute unrecognized tool.finished events.
    assert outcomes["late"][1]["model"]
    assert outcomes["late"][1]["calls"] == 2
    assert len(outcomes["late"][1]["open"]) == 1
    assert len(outcomes["late"][1]["recent"]) == 1
    assert outcomes["early"] == outcomes["late"]


def test_rejected_reservation_activity_cannot_leak_to_another_dispatch():
    rejected = _reserve("rejected", 1)
    _activity(_callback(rejected, "rejected", 0, 1), 0)
    assert "rejected" not in ac._PARTS
    fl.release_ack_part("rejected")
    assert not ac.owns_delegation("rejected")
    assert "rejected" not in ac._PARTS
    other = _reserve("other", 1)
    card = _accept(other, "other", 1)
    state = _snapshot(card.parts[0].checklist)
    assert state["model"] == ""
    assert state["calls"] == 0
    assert state["open"] == state["recent"] == []
    assert "rejected" not in ac._PARTS
