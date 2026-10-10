"""Strict-background semantics for conversational roots.

A conversational root (one the delegation checkpoint applies to) that asks for
``background=true`` and cannot get a detached job must be told so, honestly.
It must never have the batch silently run inline: that blocks the chat, spends
the foreground allowance, and clears ``SPAWN_REQUIRED`` for work that was not
handed off.  Callers the checkpoint does not apply to (noneligible roots,
explicitly synchronous callers) keep the historical fallback behaviour.

Real ``delegate_task`` entry, real async registry and durable table in a
scratch ``CLOVER_HOME``, real ``DelegationCheckpoint``.  Only child
construction and the child runner are stubbed.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

import tools.async_delegation as ad
import tools.delegate_tool as dt
from agent import delegation_checkpoint as dc
from tools.delegation_live_log import live_transcript_root


@pytest.fixture(autouse=True)
def _clean_registry():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


_CREDS = {
    "model": "m", "provider": None, "base_url": None, "api_key": None,
    "api_mode": None, "command": None, "args": None,
}


class _FakeChild:
    def __init__(self, index: int):
        self._delegate_role = "leaf"
        self._subagent_id = f"sa-test-{index}"
        self.session_id = f"child-session-{index}"
        self.closed = 0
        self.interrupted = False

    def close(self):
        self.closed += 1

    def get_activity_summary(self):
        return {"api_call_count": 0, "current_tool": None, "last_activity_ts": time.time()}


class _Harness:
    """Records what the stubbed child plumbing was asked to do."""

    def __init__(self):
        self.built: list[_FakeChild] = []
        self.runs: list[str] = []
        self.fail_build_at: int | None = None
        self.hold: threading.Event | None = None


@pytest.fixture(autouse=True)
def _checkpoint_opted_in():
    """Strict background dispatch follows the checkpoint, which ships OFF."""
    import yaml
    from clover_constants import get_clover_home

    (get_clover_home() / "config.yaml").write_text(
        yaml.safe_dump({"delegation": {"checkpoint": {"enabled": True}}}))


def _root(*, eligible: bool = True):
    """A conversational root with a real checkpoint, declared ``delegate``."""
    root = SimpleNamespace(
        valid_tool_names={"todo", "delegate_task"},
        platform="cli",
        _delegate_depth=0,
        _subagent_id=None,
        session_id="root-session",
        _interrupt_requested=False,
        _active_children=[],
        _active_children_lock=None,
        _delegation_checkpoint=dc.DelegationCheckpoint(dc.CheckpointSettings(enabled=True)),
    )
    if not eligible:
        root._delegation_checkpoint_exempt = True
    assert root._delegation_checkpoint.declare("delegate", "Long phase.")
    assert root._delegation_checkpoint.state == dc.SPAWN_REQUIRED
    return root


@pytest.fixture
def harness(monkeypatch):
    h = _Harness()

    def _build(**kw):
        index = kw["task_index"]
        if h.fail_build_at is not None and index == h.fail_build_at:
            raise ValueError("pinned delegation.command is not on PATH")
        child = _FakeChild(index)
        h.built.append(child)
        kw["parent_agent"]._active_children.append(child)
        return child

    def _run(task_index, goal, child, parent_agent, **kw):
        h.runs.append(goal)
        if h.hold is not None:
            h.hold.wait(60)
        ticket = kw.get("checkpoint_ticket")
        if ticket is not None:
            ticket.credit("inline")
        return {
            "task_index": task_index, "status": "completed",
            "summary": f"done: {goal}", "api_calls": 1,
            "duration_seconds": 0.0, "model": "m", "exit_reason": "completed",
        }

    monkeypatch.setattr(dt, "_build_child_agent", _build)
    monkeypatch.setattr(dt, "_run_single_child", _run)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: _CREDS)
    monkeypatch.setattr(dt, "_get_max_async_children", lambda: 1)
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True
    )
    return h


def _delegate(root, **kw):
    kw.setdefault("goal", "Build the reusable explainer and verify every output.")
    kw.setdefault("background", True)
    return json.loads(dt.delegate_task(parent_agent=root, **kw))


def _hold_the_only_async_slot():
    """Occupy the single async slot with a genuinely running job."""
    release = threading.Event()
    result = ad.dispatch_async_delegation_batch(
        goals=["held"], context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=lambda: (release.wait(60), {"results": []})[1],
        max_async_children=1,
    )
    assert result["status"] == "dispatched"
    return release


def _manifest_statuses():
    manifests = list(live_transcript_root().glob("*/manifest.json"))
    assert len(manifests) == 1, manifests
    tasks = json.loads(manifests[0].read_text(encoding="utf-8"))["tasks"]
    return [t.get("status") for t in tasks]


def _assert_rejected(parsed, reason):
    assert parsed["status"] == "rejected", parsed
    assert parsed["mode"] == "background"
    assert parsed["started"] is False
    assert parsed["reason"] == reason
    assert parsed["error"] and parsed["note"]


# ── capacity ───────────────────────────────────────────────────────────────

def test_capacity_rejection_never_runs_inline(harness):
    root = _root()
    release = _hold_the_only_async_slot()
    try:
        parsed = _delegate(root)
    finally:
        release.set()
    _assert_rejected(parsed, "capacity")
    assert harness.runs == []
    # Nothing was started, so the declaration must not be satisfied.
    assert root._delegation_checkpoint.state == dc.SPAWN_REQUIRED


def test_capacity_precheck_builds_no_children(harness):
    root = _root()
    release = _hold_the_only_async_slot()
    try:
        parsed = _delegate(root)
    finally:
        release.set()
    _assert_rejected(parsed, "capacity")
    assert harness.built == []


def test_has_async_capacity_tracks_running_units():
    assert ad.has_async_capacity(1) is True
    release = _hold_the_only_async_slot()
    try:
        assert ad.has_async_capacity(1) is False
        assert ad.has_async_capacity(2) is True
    finally:
        release.set()


def test_capacity_race_after_precheck_is_rejected_and_children_released(
    harness, monkeypatch
):
    """The registry can still refuse after the pre-check passed (another
    session took the slot).  The built children must be released."""
    root = _root()
    monkeypatch.setattr(
        ad, "dispatch_async_delegation_batch",
        lambda **kw: {"status": "rejected", "reason": "capacity", "error": "full"},
    )
    parsed = _delegate(root, tasks=[{"goal": "Part one of the work, in full."},
                                    {"goal": "Part two of the work, in full."}])
    _assert_rejected(parsed, "capacity")
    assert len(harness.built) == 2
    assert [c.closed for c in harness.built] == [1, 1]
    assert root._active_children == []
    assert harness.runs == []
    assert ad.active_count() == 0
    assert _manifest_statuses() == ["rejected", "rejected"]
    assert root._delegation_checkpoint.state == dc.SPAWN_REQUIRED


# ── schedule / persistence ─────────────────────────────────────────────────

def test_schedule_failure_rejected(harness, monkeypatch):
    root = _root()

    class _BrokenExecutor:
        def submit(self, *a, **k):
            raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(ad, "_get_executor", lambda n: _BrokenExecutor())
    parsed = _delegate(root)
    _assert_rejected(parsed, "schedule")
    assert harness.runs == []
    assert ad.active_count() == 0
    assert [c.closed for c in harness.built] == [1]
    assert root._delegation_checkpoint.state == dc.SPAWN_REQUIRED


def test_executor_construction_failure_rejected(harness, monkeypatch):
    root = _root()

    def _boom(n):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(ad, "_get_executor", _boom)
    parsed = _delegate(root)
    _assert_rejected(parsed, "schedule")
    assert harness.runs == []
    assert ad.active_count() == 0


class _PoolThreadStart:
    """Make the real pool's worker-thread start fail on demand.

    ``ThreadPoolExecutor.submit`` enqueues the work item *before* it starts a
    thread, so a start failure raises out of ``submit`` while the item is
    still queued.  Only ``async-delegate`` workers are affected.
    """

    def __init__(self, monkeypatch):
        real_start = threading.Thread.start
        self.failing = False
        outer = self

        def _start(thread):
            if outer.failing and thread.name.startswith("async-delegate"):
                raise RuntimeError("can't start new thread")
            return real_start(thread)

        monkeypatch.setattr(threading.Thread, "start", _start)


def _drain_pool(max_workers: int):
    """Run a sentinel through the real pool so every queued item is consumed."""
    ad._get_executor(max_workers).submit(lambda: None).result(timeout=10)


def _durable_rows() -> int:
    with ad._DB_LOCK, ad._transaction() as conn:
        return conn.execute("SELECT COUNT(*) FROM async_delegations").fetchone()[0]


def _completion_events():
    from tools.process_registry import process_registry

    events = []
    while not process_registry.completion_queue.empty():
        events.append(process_registry.completion_queue.get_nowait())
    return events


def test_thread_start_failure_leaves_no_runnable_rejected_job(harness, monkeypatch):
    """A pool whose first worker cannot start rejects the dispatch.  The work
    item is already queued at that point; once the pool recovers it must be
    inert: the rejected runner never runs, its children were released exactly
    once, and nothing is completed, registered or persisted."""
    _completion_events()
    pool = _PoolThreadStart(monkeypatch)
    root = _root()

    pool.failing = True
    parsed = _delegate(root)
    pool.failing = False
    _assert_rejected(parsed, "schedule")

    _drain_pool(1)

    assert harness.runs == []
    assert [c.closed for c in harness.built] == [1]
    assert _completion_events() == []
    assert ad.active_count() == 0
    assert ad.list_async_delegations() == []
    assert _durable_rows() == 0
    assert root._delegation_checkpoint.state == dc.SPAWN_REQUIRED


def test_pool_growth_failure_cannot_race_an_existing_worker(monkeypatch):
    """With a worker already busy, a failing pool growth rejects the dispatch
    while that worker is free to pick the queued item up the moment its
    current job ends.  The rejected runner must still never run."""
    _completion_events()
    pool = _PoolThreadStart(monkeypatch)
    release = threading.Event()
    ran = []

    held = ad.dispatch_async_delegation_batch(
        goals=["held"], context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=lambda: (release.wait(60), {"results": []})[1],
        max_async_children=2,
    )
    assert held["status"] == "dispatched"
    try:
        pool.failing = True
        rejected = ad.dispatch_async_delegation_batch(
            goals=["rejected"], context=None, toolsets=None, role="leaf",
            model="m", session_key="",
            runner=lambda: ran.append("rejected") or {"results": []},
            max_async_children=2,
        )
        pool.failing = False
        assert rejected["status"] == "rejected"
        assert rejected["reason"] == "schedule"
    finally:
        release.set()

    _drain_pool(2)
    deadline = time.monotonic() + 10
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)

    assert ran == []
    assert {e.get("delegation_id") for e in _completion_events()} <= {
        held["delegation_id"]
    }
    assert ad.active_count() == 0


def test_accepted_dispatch_still_runs_through_the_admission_gate(monkeypatch):
    """The admission decision must not delay or lose a job that was accepted."""
    _completion_events()
    ran = []
    result = ad.dispatch_async_delegation_batch(
        goals=["ok"], context=None, toolsets=None, role="leaf", model="m",
        session_key="",
        runner=lambda: ran.append("ok") or {"results": [
            {"task_index": 0, "status": "completed", "summary": "s"}]},
        max_async_children=1,
    )
    assert result["status"] == "dispatched"
    deadline = time.monotonic() + 10
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ran == ["ok"]
    assert [e.get("delegation_id") for e in _completion_events()] == [
        result["delegation_id"]
    ]


def test_persistence_failure_rejected_no_phantom_record(harness, monkeypatch):
    root = _root()

    def _boom(record):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ad, "_persist_dispatch", _boom)
    parsed = _delegate(root)
    _assert_rejected(parsed, "persistence")
    assert harness.runs == []
    # A phantom running record would consume async capacity forever.
    assert ad.active_count() == 0
    assert ad.has_async_capacity(1) is True
    assert root._delegation_checkpoint.state == dc.SPAWN_REQUIRED


def test_dispatch_persistence_failure_is_a_rejection_for_every_caller(monkeypatch):
    """Strict-independent defect: an unguarded persistence error escaped the
    dispatcher after the record had been inserted."""

    def _boom(record):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ad, "_persist_dispatch", _boom)
    result = ad.dispatch_async_delegation_batch(
        goals=["x"], context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=lambda: {"results": []}, max_async_children=1,
    )
    assert result["status"] == "rejected"
    assert result["reason"] == "persistence"
    assert ad.active_count() == 0


def test_dispatch_rejections_carry_a_reason():
    release = _hold_the_only_async_slot()
    try:
        full = ad.dispatch_async_delegation_batch(
            goals=["x"], context=None, toolsets=None, role="leaf", model="m",
            session_key="", runner=lambda: {"results": []}, max_async_children=1,
        )
    finally:
        release.set()
    assert full["status"] == "rejected"
    assert full["reason"] == "capacity"


# ── delivery unsupported ───────────────────────────────────────────────────

def _no_async_delivery(monkeypatch):
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: False
    )
    monkeypatch.setattr(ad, "_current_origin_session_id", lambda: "")


def test_delivery_unsupported_rejected_in_strict_mode(harness, monkeypatch):
    _no_async_delivery(monkeypatch)
    root = _root()
    parsed = _delegate(root)
    _assert_rejected(parsed, "delivery_unsupported")
    assert harness.runs == []
    # Rejected before any child was built.
    assert harness.built == []
    assert root._delegation_checkpoint.state == dc.SPAWN_REQUIRED


def test_delivery_unsupported_still_sync_for_noneligible_caller(harness, monkeypatch):
    _no_async_delivery(monkeypatch)
    root = _root(eligible=False)
    parsed = _delegate(root)
    assert parsed["results"][0]["status"] == "completed"
    assert "SYNCHRONOUSLY" in parsed["note"]
    assert harness.runs == ["Build the reusable explainer and verify every output."]


# ── construction failure ───────────────────────────────────────────────────

def test_construction_failure_closes_prior_children(harness):
    root = _root(eligible=False)
    harness.fail_build_at = 1
    out = json.loads(dt.delegate_task(
        tasks=[{"goal": "Part one of the work, in full."},
               {"goal": "Part two of the work, in full."}],
        background=True, parent_agent=root,
    ))
    # Noneligible callers keep the bare tool error ...
    assert "error" in out and out.get("status") != "rejected"
    # ... but the child that was already built must not leak.
    assert [c.closed for c in harness.built] == [1]
    assert root._active_children == []


def test_construction_failure_is_a_rejection_for_strict_callers(harness):
    root = _root()
    harness.fail_build_at = 1
    parsed = _delegate(root, tasks=[{"goal": "Part one of the work, in full."},
                                    {"goal": "Part two of the work, in full."}])
    _assert_rejected(parsed, "construction")
    assert [c.closed for c in harness.built] == [1]
    assert root._active_children == []
    assert harness.runs == []
    assert root._delegation_checkpoint.state == dc.SPAWN_REQUIRED


@pytest.mark.parametrize("exc", [RuntimeError("model client init failed"), OSError("too many open files")])
def test_any_construction_failure_is_a_rejection_for_strict_callers(harness, monkeypatch, exc):
    """Not only the explicit-pin ValueError: an earlier child is already built."""
    root = _root()
    built = harness.built

    def _build(**kw):
        if kw["task_index"] == 1:
            raise exc
        child = _FakeChild(kw["task_index"])
        built.append(child)
        kw["parent_agent"]._active_children.append(child)
        return child

    monkeypatch.setattr(dt, "_build_child_agent", _build)
    parsed = _delegate(root, tasks=[{"goal": "Part one of the work, in full."},
                                    {"goal": "Part two of the work, in full."}])
    _assert_rejected(parsed, "construction")
    assert [c.closed for c in built] == [1]
    assert root._active_children == []
    assert harness.runs == []
    assert ad.active_count() == 0
    assert _manifest_statuses() == ["rejected", "rejected"]
    assert root._delegation_checkpoint.state == dc.SPAWN_REQUIRED
    assert root._delegation_checkpoint.phase == dc.PHASE_FOREGROUND


@pytest.mark.parametrize("exc", [RuntimeError("model client init failed"), OSError("too many open files")])
def test_any_construction_failure_closes_prior_children_for_every_caller(harness, monkeypatch, exc):
    """Noneligible callers keep raising, but nothing built may leak."""
    root = _root(eligible=False)
    built = harness.built

    def _build(**kw):
        if kw["task_index"] == 1:
            raise exc
        child = _FakeChild(kw["task_index"])
        built.append(child)
        kw["parent_agent"]._active_children.append(child)
        return child

    monkeypatch.setattr(dt, "_build_child_agent", _build)
    with pytest.raises(type(exc)):
        dt.delegate_task(
            tasks=[{"goal": "Part one of the work, in full."},
                   {"goal": "Part two of the work, in full."}],
            background=True, parent_agent=root,
        )
    assert [c.closed for c in built] == [1]
    assert root._active_children == []


def test_failure_while_wiring_a_built_child_releases_that_child_too(harness, monkeypatch):
    root = _root()
    wired = {"n": 0}

    def _wrap(inner, writer):
        wired["n"] += 1
        if wired["n"] == 2:
            raise OSError("cannot open live transcript")
        return inner

    monkeypatch.setattr("tools.delegation_live_log.wrap_progress_callback", _wrap)
    parsed = _delegate(root, tasks=[{"goal": "Part one of the work, in full."},
                                    {"goal": "Part two of the work, in full."}])
    _assert_rejected(parsed, "construction")
    # The second child was built but never made it into the roster.
    assert [c.closed for c in harness.built] == [1, 1]
    assert root._active_children == []
    assert harness.runs == []


# ── what must not change ───────────────────────────────────────────────────

def test_capacity_fallback_unchanged_for_noneligible_caller(harness):
    root = _root(eligible=False)
    release = _hold_the_only_async_slot()
    try:
        parsed = _delegate(root)
    finally:
        release.set()
    assert parsed["results"][0]["status"] == "completed"
    assert "SYNCHRONOUSLY" in parsed["note"]
    assert len(harness.runs) == 1


def test_explicit_synchronous_call_is_unchanged(harness):
    root = _root()
    parsed = _delegate(root, background=False)
    assert parsed["results"][0]["status"] == "completed"
    assert harness.runs == ["Build the reusable explainer and verify every output."]
    assert root._delegation_checkpoint.state == dc.DELEGATED_STARTED


def test_accepted_background_dispatch_hands_the_request_off(harness):
    root = _root()
    harness.hold = threading.Event()
    try:
        parsed = _delegate(root)
        assert parsed["status"] == "dispatched"
        assert parsed["mode"] == "background"
        assert root._delegation_checkpoint.phase == dc.PHASE_HANDED_OFF
        assert ad.active_count() == 1
    finally:
        harness.hold.set()


# ── ancillary failure after the worker was accepted ────────────────────────

def test_monitor_failure_after_submit_still_hands_the_request_off(harness, monkeypatch):
    """The detached job is already running once ``submit`` succeeded.  A
    failure in the stale-monitor thread must not turn that accepted job into a
    raised error: the root would stay open with a job it never handed off."""
    from tools.process_registry import process_registry

    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()

    def _no_thread():
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(ad, "_ensure_stale_monitor", _no_thread)
    root = _root()
    harness.hold = threading.Event()
    try:
        parsed = _delegate(root)
        assert parsed["status"] == "dispatched"
        delegation_id = parsed["delegation_id"]
        cp = root._delegation_checkpoint
        assert cp.phase == dc.PHASE_HANDED_OFF
        assert dc.completion_directive(root).reason == "delegation_handoff"
        assert dc.admit(root, "write_file", {}, "w1").block_code == dc.HANDOFF_ACTIVE
        assert dc.admit(root, "delegate_task", {"goal": "again"}, "d2").block_code == dc.SPAWN_CLOSED
        # The job survived: running, not interrupted, not duplicated inline.
        assert ad.get_durable_delegation(delegation_id)["state"] == "running"
        assert all(not c.interrupted for c in harness.built)
        assert len(harness.built) == 1
    finally:
        harness.hold.set()

    deadline = time.monotonic() + 10
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    events = []
    while not process_registry.completion_queue.empty():
        events.append(process_registry.completion_queue.get_nowait())
    assert [e.get("delegation_id") for e in events] == [delegation_id]
    assert harness.runs == ["Build the reusable explainer and verify every output."]
