"""Incident probes: causal handback is not acceptance verification.

Real lifecycle, dispatch receipts and SQLite owners in a temporary CLOVER_HOME.
No provider is called. These are status/authority probes, not live model UX proof.
Originally RED incident probes; assertions remain preserved after the fix.
Positive authoritative acceptance is tested at the real gateway owner seam.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from agent import foreground_time_limit as fl, work_lifecycle as wl, workstreams as ws
from cron import notepad as np
from tools import async_delegation as ad


@pytest.fixture
def incident(monkeypatch, tmp_path):
    from clover_state import SessionDB

    settings = ws.ProactiveSettings(enabled=True, mode="active", threads_max_items=32)
    monkeypatch.setattr(ws, "load_settings", lambda: settings)
    scope = ws.make_scope(str(tmp_path / "project"), root="local")
    db = SessionDB()
    db.create_session("parent", "cli")
    agent = SimpleNamespace(platform="cli", session_id="parent", _session_db=db,
                            _workstream_scope=scope)
    yield SimpleNamespace(agent=agent, scope=scope, settings=settings, db=db)
    db.close()


def dispatch(h, did, goal, turn):
    ad._persist_dispatch({"delegation_id": did, "dispatched_at": time.time(),
                          "goal": goal, "session_key": "parent", "origin_session_id": "parent"})
    fl.note_own_dispatch(h.agent, did, goals=(goal,), request_identity={"turn_id": turn})


def complete(did, *, status="completed", consumed=True, **result):
    ad._persist_completion({"delegation_id": did, "status": status},
                           {"status": status, **result})
    if consumed:
        assert ad.mark_completion_delivered(did)


def handback(h, *, first_result=None):
    wl.begin_turn(h.agent, "", None, None)
    dispatch(h, "deleg_292cc8e4", "Preserve direct answers during delegation", "original")
    wl.end_turn(h.agent)
    original = np.list_workstreams(h.scope, kinds=["obligation"])[0]
    complete("deleg_292cc8e4", summary="Prompt guidance only", **(first_result or {}))
    wl.begin_turn(h.agent, "", "internal", ["deleg_292cc8e4"], may_note=False)
    dispatch(h, "deleg_126703c7", "Implement and check the runtime answer preservation fix", "handback")
    wl.end_turn(h.agent)
    # This is the runtime's causal relation, not a goal-text similarity guess.
    assert [r.delegation_id for r in h.agent._wl_handback_dispatches["deleg_126703c7"]] == [
        "deleg_292cc8e4"]
    return original


def test_consumed_status_means_result_received_not_verified(incident):
    h = incident
    handback(h)
    complete("deleg_126703c7", summary="Everything verified and published")
    wl.begin_turn(h.agent, "", "internal", ["deleg_126703c7"], may_note=False)
    assert wl.report(h.agent).status == "result_received"


def test_consumed_handback_does_not_verify_acceptance(incident):
    """The exact false split: original stays open while integration is 'verified'."""
    h = incident
    original = handback(h)
    complete("deleg_126703c7", summary="Runtime fix done; all tests pass")
    wl.begin_turn(h.agent, "", "internal", ["deleg_126703c7"], may_note=False)
    wl.end_turn(h.agent)

    current = np.get_workstream(h.scope, original["workstream_id"])
    assert current is not None and current["status"] == "open"
    integration = next(r for r in np.list_workstreams(h.scope)
                       if "runtime answer" in r["title"])
    assert integration["status"] == "open", "consumption is not parent acceptance verification"
    assert not any(e["status"] == "verified" for e in np.list_evidence(
        h.scope, integration["workstream_id"]))


def test_handback_turn_refreshes_original_owner_failure(incident):
    """New handback dispatch must not hide a newly committed original failure."""
    h = incident
    original = handback(h, first_result={"incomplete": True})
    evidence = np.list_evidence(h.scope, original["workstream_id"])
    assert [(e["source_id"], e["status"]) for e in evidence] == [
        ("deleg_292cc8e4", "failed")]
    current = np.get_workstream(h.scope, original["workstream_id"])
    assert current is not None and "failed" in current["summary"]


def test_resolved_outcome_replaces_stale_current_summary(incident):
    """A typed verified outcome must not retain 'unverified' as current fact.

    Explicit typed input tests the policy/store seam only. It is NOT a claim
    that production has an authoritative parent-verification producer.
    """
    h = incident
    pending = ws.TypedOutcome("Check candidate artifact", "unverified", "project_check", "check-1")
    original = ws.record_outcomes(h.scope, [pending], h.settings)[0]
    verified = ws.TypedOutcome("Check candidate artifact", "verified", "project_check", "check-1",
                               summary="Candidate built and tested; not deployed")
    ws.record_outcomes(h.scope, [verified], h.settings)
    current = np.get_workstream(h.scope, original["workstream_id"])
    assert current is not None and current["status"] == "resolved"
    assert current["summary"] == verified.summary


@pytest.mark.parametrize("bad", [
    {"status": "failed"}, {"incomplete": True}, {"truncated": True},
    {"exit_reason": "max_iterations"}, {"status": "cancelled"},
])
def test_bad_or_stopped_handback_never_closes_original(incident, bad):
    h = incident
    original = handback(h, first_result={"incomplete": True})
    complete("deleg_126703c7", **bad)
    wl.begin_turn(h.agent, "", "internal", ["deleg_126703c7"], may_note=False)
    wl.end_turn(h.agent)
    current = np.get_workstream(h.scope, original["workstream_id"])
    assert current is not None and current["status"] == "open"
    assert "Preserve direct answers" in ws.thread_context(h.scope, h.settings)


@pytest.mark.parametrize("consumed", [False, True])
def test_replayed_completion_does_not_duplicate_records(incident, consumed):
    h = incident
    handback(h)
    complete("deleg_126703c7", consumed=consumed)
    snapshots = []
    for _ in range(3):
        wl.begin_turn(h.agent, "", "internal", ["deleg_126703c7"], may_note=False)
        wl.end_turn(h.agent)
        snapshots.append(np.list_workstreams(h.scope))
    assert snapshots[0] == snapshots[1] == snapshots[2]
    if not consumed:
        integration = next(r for r in snapshots[0] if "runtime answer" in r["title"])
        assert integration["status"] == "open"


def test_unrelated_rollout_stays_open(incident):
    h = incident
    rollout = ws.record_outcomes(h.scope, [ws.TypedOutcome(
        "Review, deploy and release the answer preservation fix", "not_run", "rollout", "rollout-1",
        summary="Candidate built/tested only; review, deployment and release remain",
    )], h.settings)[0]
    handback(h)
    complete("deleg_126703c7")
    wl.begin_turn(h.agent, "", "internal", ["deleg_126703c7"], may_note=False)
    wl.end_turn(h.agent)
    assert np.get_workstream(h.scope, rollout["workstream_id"]) == rollout


@pytest.mark.parametrize("field,value", [
    ("profile", "/other-profile"), ("project", "/other-project"),
    ("audience", "private:other"), ("root", "other-root"),
])
def test_completion_does_not_write_another_scope(incident, field, value):
    from dataclasses import replace

    h = incident
    foreign = replace(h.scope, **{field: value})
    row = ws.record_outcomes(foreign, [ws.TypedOutcome(
        "Preserve direct answers during delegation", "unverified", "delegation", "foreign-helper",
    )], h.settings)[0]
    handback(h)
    complete("deleg_126703c7")
    wl.begin_turn(h.agent, "", "internal", ["deleg_126703c7"], may_note=False)
    wl.end_turn(h.agent)
    assert np.get_workstream(foreign, row["workstream_id"]) == row


def test_another_instance_does_not_adopt_completion_lineage(incident):
    h = incident
    handback(h)
    before = np.list_workstreams(h.scope)
    complete("deleg_126703c7")
    other = SimpleNamespace(platform="cli", session_id="other-session", _workstream_scope=h.scope)
    wl.begin_turn(other, "", "internal", ["deleg_126703c7"], may_note=False)
    wl.end_turn(other)
    assert np.list_workstreams(h.scope) == before


def test_parent_stop_does_not_upgrade_consumption_to_verification(incident):
    h = incident
    handback(h)
    complete("deleg_126703c7")
    wl.begin_turn(h.agent, "", "internal", ["deleg_126703c7"], may_note=False)
    h.agent._interrupt_requested = True
    # turn_finalizer calls end_turn even on the interrupted path.
    wl.end_turn(h.agent)
    integration = next(r for r in np.list_workstreams(h.scope) if "runtime answer" in r["title"])
    assert integration["status"] == "open"


def test_same_goal_in_another_session_is_not_matching_ownership(incident):
    h = incident
    goal = "Check candidate artifact"
    original = ws.record_outcomes(h.scope, [ws.TypedOutcome(
        goal, "unverified", "delegation", "original-job", session_id="parent",
    )], h.settings)[0]
    # Policy seam only: same wording is not causal lineage or matching scope
    # of acceptance. A different job/session must not resolve the first job.
    ws.record_outcomes(h.scope, [ws.TypedOutcome(
        goal, "verified", "delegation", "unrelated-job", session_id="other-session",
    )], h.settings)
    current = np.get_workstream(h.scope, original["workstream_id"])
    assert current is not None and current["status"] == "open"


def test_changed_failed_evidence_is_not_overwritten_by_helper_success(incident):
    h = incident
    original = handback(h, first_result={"incomplete": True})
    np.add_evidence(h.scope, original["workstream_id"], source_kind="project_check",
                    source_id="independent-failed-check", status="failed",
                    summary="Prompt guidance fails answer preservation")
    complete("deleg_126703c7", summary="Everything fixed and installed")
    wl.begin_turn(h.agent, "", "internal", ["deleg_126703c7"], may_note=False)
    wl.end_turn(h.agent)
    current = np.get_workstream(h.scope, original["workstream_id"])
    assert current is not None and current["status"] == "open"
    assert any(e["source_id"] == "independent-failed-check" and e["status"] == "failed"
               for e in np.list_evidence(h.scope, original["workstream_id"]))
