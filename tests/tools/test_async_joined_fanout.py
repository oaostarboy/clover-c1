"""Joined fan-out delivery (C1.5 B1): parallel helpers report back together.

Real async registry and durable ledger in a scratch ``CLOVER_HOME``. Only the
child work itself (the runner callable) is fake. ``fanout_delivery="each"``
is today's behaviour and is covered both here and by the existing
``test_async_batch_child_delivery`` suite.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from tools import async_delegation as ad
from tools.process_registry import format_process_notification, process_registry

BATCH = "deleg_b1b1b1b1"
DEAD_PID = 99999999


@pytest.fixture(autouse=True)
def reg(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    ad._reset_for_tests(drain_timeout=10)
    original = process_registry.completion_queue
    process_registry.completion_queue = queue.Queue()
    try:
        yield process_registry.completion_queue
    finally:
        # Runners released at the end of the test finish (and publish) first,
        # against this test's queue and records, never the next test's.
        ad._reset_for_tests(drain_timeout=10)
        process_registry.completion_queue = original


def _drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def _wait_event(q, timeout=10.0):
    return q.get(timeout=timeout)


def _result(index, status="completed", summary=None, error=None):
    return {
        "task_index": index, "status": status,
        "summary": summary if summary is not None else (
            f"result {index}" if status == "completed" else None),
        "error": error, "api_calls": 2, "duration_seconds": 1.5,
        "exit_reason": status,
    }


def _publish(index, **kw):
    ad.publish_batch_child_completion(_result(index, **kw), delegation_id=BATCH)


GOALS = ["audit alpha", "audit beta", "audit gamma"]
IDENTITY = {"turn_id": "turn-7", "request_id": "req-7", "generation": 3}


def _dispatch(runner, *, mode="joined", goals=GOALS, **kw):
    handle = ad.dispatch_async_delegation_batch(
        goals=goals, context="shared brief", toolsets=None, role="leaf",
        model="m", session_key="agent:main:telegram:dm:100",
        parent_session_id="sess-origin", runner=runner, delegation_id=BATCH,
        fanout_delivery=mode, request_identity=IDENTITY, **kw,
    )
    assert handle["status"] == "dispatched", handle
    return handle


def _state(delegation_id):
    return ad.get_durable_delegation(delegation_id)["delivery_state"]


def _task_json():
    with ad._DB_LOCK, ad._transaction() as conn:
        raw = conn.execute(
            "SELECT task_json FROM async_delegations WHERE delegation_id=?", (BATCH,)
        ).fetchone()[0]
    return json.loads(raw)


def _mark_owner_dead_and_forget_memory():
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET owner_pid=? WHERE delegation_id=?",
            (DEAD_PID, BATCH),
        )
    with ad._records_lock:
        ad._records.clear()


# ── one join, ordered, failed child included ───────────────────────────────

def test_joined_children_do_not_wake_and_one_join_carries_all_in_task_order(reg):
    def runner():
        _publish(2, summary="gamma findings")
        _publish(0, summary="alpha findings")
        _publish(1, status="error", error="beta worker crashed")
        return {"results": [
            _result(0, summary="alpha findings"),
            _result(1, status="error", error="beta worker crashed"),
            _result(2, summary="gamma findings"),
        ]}

    _dispatch(runner)
    join = _wait_event(reg)

    assert join["delegation_id"] == BATCH
    assert join["is_batch"] is True
    assert join["fanout_delivery"] == "joined"
    assert [r["task_index"] for r in join["results"]] == [0, 1, 2]
    assert [r["status"] for r in join["results"]] == ["completed", "error", "completed"]
    assert join["incomplete_indexes"] == []
    assert reg.empty(), "child completions must not re-enter the root on their own"

    # Durable receipts are kept, parked behind the join.
    for i in range(3):
        row = ad.get_durable_delegation(f"{BATCH}:child:{i}")
        assert row is not None and row["delivery_state"] == "joined"

    text = format_process_notification(join)
    first, second, third = (text.index(f"TASK {i}/3") for i in (1, 2, 3))
    assert first < second < third
    assert "alpha findings" in text and "gamma findings" in text
    assert "beta worker crashed" in text


def test_each_mode_is_todays_per_child_delivery(reg):
    def runner():
        _publish(0, summary="alpha findings")
        _publish(1, status="error", error="beta worker crashed")
        _publish(2, summary="gamma findings")
        return {"results": [
            _result(0, summary="alpha findings"),
            _result(1, status="error", error="beta worker crashed"),
            _result(2, summary="gamma findings"),
        ]}

    _dispatch(runner, mode="each")
    seen = [_wait_event(reg) for _ in range(3)]

    assert [e["delegation_id"] for e in seen] == [f"{BATCH}:child:{i}" for i in range(3)]
    assert all(e.get("batch_delegation_id") == BATCH for e in seen)
    assert all("fanout_delivery" not in e or e["fanout_delivery"] != "joined" for e in seen)
    assert all(_state(e["delegation_id"]) == "pending" for e in seen)
    deadline = time.monotonic() + 10
    while _state(BATCH) != "delivered" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _state(BATCH) == "delivered"  # silent ack, no second batch summary
    assert reg.empty()


# ── identity persisted at dispatch ─────────────────────────────────────────

def test_mode_and_request_identity_are_persisted_at_dispatch_and_stamped(reg):
    gate = threading.Event()

    def runner():
        gate.wait(10)
        return {"results": [_result(i) for i in range(3)]}

    _dispatch(runner)
    try:
        task = _task_json()
        assert task["fanout_delivery"] == "joined"
        assert task["request_turn_id"] == "turn-7"
        assert task["request_id"] == "req-7"
        assert task["request_generation"] == 3
    finally:
        gate.set()
    join = _wait_event(reg)
    assert join["request_turn_id"] == "turn-7"
    assert join["request_id"] == "req-7"
    assert join["request_generation"] == 3
    assert join["parent_session_id"] == "sess-origin"


# ── exactly-once delivery, duplicates, claims ─────────────────────────────

def test_duplicate_child_publish_and_replayed_completion_yield_one_join(reg):
    def runner():
        _publish(0, summary="alpha findings")
        _publish(0, summary="alpha findings")  # duplicate completion
        _publish(1, summary="beta findings")
        _publish(2, summary="gamma findings")
        _publish(2, summary="gamma findings")
        return {"results": [_result(i, summary=f"{n} findings")
                            for i, n in enumerate(("alpha", "beta", "gamma"))]}

    _dispatch(runner)
    join = _wait_event(reg)
    assert [r["task_index"] for r in join["results"]] == [0, 1, 2]
    assert reg.empty()
    with ad._DB_LOCK, ad._transaction() as conn:
        kids = ad._committed_child_ids(conn, BATCH)
    assert kids == {f"{BATCH}:child:{i}" for i in range(3)}


def test_join_claim_is_exactly_once_and_cascades_to_child_receipts(reg):
    def runner():
        for i in range(3):
            _publish(i)
        return {"results": [_result(i) for i in range(3)]}

    _dispatch(runner)
    join = _wait_event(reg)
    receipts = [f"{BATCH}:child:{i}" for i in range(3)]

    assert ad.owned_receipts_ready(receipts) == ()  # nothing handed over yet
    claim = ad.claim_event_delivery(join, "gateway-a")
    assert claim
    assert ad.claim_event_delivery(join, "gateway-b") is None  # exactly once
    assert set(ad.owned_receipts_ready(receipts)) == set(receipts)

    ad.release_event_delivery(join, claim)
    assert ad.owned_receipts_ready(receipts) == ()
    assert _state(BATCH) == "pending"

    claim = ad.claim_event_delivery(join, "gateway-b")
    assert claim
    ad.complete_event_delivery(join, claim)
    assert _state(BATCH) == "delivered"
    assert all(_state(r) == "delivered" for r in receipts)
    assert set(ad.owned_receipts_ready(receipts)) == set(receipts)

    replay = queue.Queue()
    assert ad.restore_undelivered_completions(replay) == 0
    assert ad.claim_event_delivery(join, "gateway-c") is None


def test_dropped_join_settles_child_receipts_without_replaying_them(reg):
    def runner():
        for i in range(3):
            _publish(i)
        return {"results": [_result(i) for i in range(3)]}

    _dispatch(runner)
    join = _wait_event(reg)
    claim = ad.claim_event_delivery(join, "gateway-a")
    assert ad.drop_completion_delivery(BATCH, claim)
    assert _state(BATCH) == "dropped"
    for i in range(3):
        assert _state(f"{BATCH}:child:{i}") == "inboxed"
    replay = queue.Queue()
    assert ad.restore_undelivered_completions(replay) == 0


# ── restart / owner death ─────────────────────────────────────────────────

def test_owner_death_before_join_recovers_one_join_from_the_durable_child_set(
    reg, monkeypatch
):
    gate = threading.Event()
    parked = threading.Event()
    finalized = threading.Event()
    original_finalize = ad._finalize_batch

    def spy(*a, **k):
        try:
            return original_finalize(*a, **k)
        finally:
            finalized.set()

    monkeypatch.setattr(ad, "_finalize_batch", spy)

    def runner():
        _publish(0, summary="alpha findings")
        _publish(2, summary="gamma findings")
        parked.set()
        gate.wait(30)  # child 1 never publishes: this process "dies" here
        return {"results": []}

    try:
        _dispatch(runner)
        assert parked.wait(10)
        _mark_owner_dead_and_forget_memory()
        assert reg.empty()

        replay = queue.Queue()
        assert ad.restore_undelivered_completions(replay) == 1
        join = replay.get_nowait()
        assert join["delegation_id"] == BATCH
        assert join["fanout_delivery"] == "joined"
        assert [r["task_index"] for r in join["results"]] == [0, 1, 2]
        assert [r["status"] for r in join["results"]] == ["completed", "incomplete", "completed"]
        assert join["incomplete_indexes"] == [1]
        assert join["results"][0]["summary"] == "alpha findings"  # N-1 good results kept
        assert join["results"][2]["summary"] == "gamma findings"
        assert join["request_turn_id"] == "turn-7"
        assert join["parent_session_id"] == "sess-origin"
        text = format_process_notification(join)
        assert "alpha findings" in text and "gamma findings" in text
        assert "incomplete" in text.lower()

        # A second restart before delivery replays the SAME join, never a new one.
        again = queue.Queue()
        assert ad.restore_undelivered_completions(again) == 1
        assert again.get_nowait()["delegation_id"] == BATCH
        # Children are parked behind the join, never replayed on their own.
        for i in (0, 2):
            assert _state(f"{BATCH}:child:{i}") == "joined"
    finally:
        gate.set()
    assert finalized.wait(10)
    assert reg.empty(), "the zombie runner returning must not publish a second join"


def test_owner_death_with_every_child_committed_still_delivers_the_join(reg):
    gate = threading.Event()
    parked = threading.Event()

    def runner():
        for i in range(3):
            _publish(i, summary=f"result {i}")
        parked.set()
        gate.wait(30)
        return {"results": []}

    try:
        _dispatch(runner)
        assert parked.wait(10)
        _mark_owner_dead_and_forget_memory()
        replay = queue.Queue()
        assert ad.restore_undelivered_completions(replay) == 1
        join = replay.get_nowait()
        assert join["delegation_id"] == BATCH
        assert [r["status"] for r in join["results"]] == ["completed"] * 3
        assert join["incomplete_indexes"] == []
        assert _state(BATCH) == "pending"  # not silently acknowledged
    finally:
        gate.set()


# ── stall recovery + late child ───────────────────────────────────────────

def test_stalled_batch_joins_with_incomplete_slot_then_late_child_is_one_bound_result(
    reg, monkeypatch
):
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.05)
    monkeypatch.setattr(ad, "_STALE_IDLE_SECONDS", 0.1)
    monkeypatch.setattr(ad, "_STALL_GRACE_SECONDS", 0.1)
    gate = threading.Event()
    finalized = threading.Event()
    original_finalize = ad._finalize_batch

    def spy(*a, **k):
        try:
            return original_finalize(*a, **k)
        finally:
            finalized.set()

    monkeypatch.setattr(ad, "_finalize_batch", spy)

    def runner():
        _publish(0, summary="alpha findings")
        _publish(2, summary="gamma findings")
        assert gate.wait(30)
        _publish(1, summary="beta findings, finally")  # lands after the join
        _publish(1, summary="beta findings, finally")  # and replays
        return {"results": [_result(i) for i in range(3)]}

    try:
        _dispatch(runner, progress_fn=lambda: (("frozen",), False))
        join = _wait_event(reg, timeout=15)
        assert join["delegation_id"] == BATCH
        assert join["status"] == "stalled"
        assert [r["status"] for r in join["results"]] == ["completed", "incomplete", "completed"]
        assert join["incomplete_indexes"] == [1]
        before = ad.get_durable_delegation(BATCH)
    finally:
        gate.set()

    late = _wait_event(reg, timeout=15)
    assert finalized.wait(10)
    assert late["delegation_id"] == f"{BATCH}:child:1"
    assert late["batch_delegation_id"] == BATCH
    assert late["late_result"] is True
    assert late["summary"] == "beta findings, finally"
    assert late["request_turn_id"] == "turn-7"
    assert reg.empty(), "never a second join and never a duplicate late result"
    assert ad.get_durable_delegation(BATCH)["result"] == before["result"]
    assert _state(f"{BATCH}:child:1") == "pending"

    text = format_process_notification(late)
    assert BATCH in text
    assert "late" in text.lower()
    assert "beta findings, finally" in text

    claim = ad.claim_event_delivery(late, "gateway-a")
    assert claim
    ad.complete_event_delivery(late, claim)
    assert ad.claim_event_delivery(late, "gateway-b") is None


def test_late_child_after_memory_pruned_still_binds_to_the_durable_batch(reg):
    gate = threading.Event()
    parked = threading.Event()

    def runner():
        _publish(0, summary="alpha findings")
        parked.set()
        gate.wait(30)
        return {"results": []}

    try:
        _dispatch(runner, goals=["audit alpha", "audit beta"])
        assert parked.wait(10)
        _mark_owner_dead_and_forget_memory()
        replay = queue.Queue()
        assert ad.restore_undelivered_completions(replay) == 1
        join = replay.get_nowait()
        assert join["incomplete_indexes"] == [1]
        claim = ad.claim_event_delivery(join, "gateway-a")
        ad.complete_event_delivery(join, claim)
        # A different process (or a pruned record) sees the straggler later.
        _publish(1, summary="beta findings, finally")
    finally:
        gate.set()
    late = reg.get_nowait()
    assert late["late_result"] is True
    assert late["batch_delegation_id"] == BATCH
    assert reg.empty()


# ── cancellation ──────────────────────────────────────────────────────────

def test_cancelled_children_join_as_cancelled_not_as_a_new_task(reg):
    from gateway.completion_ownership import completion_cancelled_by_user

    def runner():
        for i in range(3):
            _publish(i, status="interrupted", error="Operation interrupted")
        return {"results": [
            _result(i, status="interrupted", error="Operation interrupted")
            for i in range(3)
        ]}

    _dispatch(runner)
    join = _wait_event(reg)
    assert [r["status"] for r in join["results"]] == ["interrupted"] * 3
    assert completion_cancelled_by_user(join, "route_session_closed") is True
    assert reg.empty()


def test_partly_finished_batch_is_not_treated_as_cancelled(reg):
    from gateway.completion_ownership import completion_cancelled_by_user

    def runner():
        _publish(0, summary="alpha findings")
        _publish(1, status="interrupted", error="Operation interrupted")
        _publish(2, status="interrupted", error="Operation interrupted")
        return {"results": [
            _result(0, summary="alpha findings"),
            _result(1, status="interrupted", error="Operation interrupted"),
            _result(2, status="interrupted", error="Operation interrupted"),
        ]}

    _dispatch(runner)
    join = _wait_event(reg)
    assert completion_cancelled_by_user(join, "route_session_closed") is False


# ── a child whose own publish failed is still in the join ─────────────────

def test_result_whose_child_publish_failed_is_rescued_from_the_batch_aggregate(reg):
    def runner():
        _publish(0, summary="alpha findings")
        _publish(2, summary="gamma findings")
        # child 1's publish raised in the worker; only the aggregate has it
        return {"results": [
            _result(0, summary="alpha findings"),
            _result(1, summary="beta findings"),
            _result(2, summary="gamma findings"),
        ]}

    _dispatch(runner)
    join = _wait_event(reg)
    assert [r["status"] for r in join["results"]] == ["completed"] * 3
    assert join["results"][1]["summary"] == "beta findings"
    assert join["incomplete_indexes"] == []


# ── delegate_task wiring ──────────────────────────────────────────────────

def _write_delegation_config(**values):
    import yaml
    from clover_constants import get_clover_home

    (get_clover_home() / "config.yaml").write_text(
        yaml.safe_dump({"delegation": values}), encoding="utf-8"
    )


@pytest.mark.parametrize("configured, expected", [
    (None, "joined"),
    ("joined", "joined"),
    ("each", "each"),
    ("EACH", "each"),
    ("sometimes", "joined"),
])
def test_config_resolves_fanout_mode(configured, expected):
    import tools.delegate_tool as dt

    _write_delegation_config(**({} if configured is None else {"fanout_delivery": configured}))
    assert dt._get_fanout_delivery() == expected


def test_default_config_ships_joined():
    from clover_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["delegation"]["fanout_delivery"] == "joined"


@pytest.mark.parametrize("configured, expected", [("joined", "joined"), ("each", "each")])
def test_background_batch_dispatch_carries_mode_and_request_identity(
    monkeypatch, configured, expected
):
    import tools.delegate_tool as dt
    from types import SimpleNamespace

    _write_delegation_config(fanout_delivery=configured)
    captured = {}

    def fake_dispatch(**kw):
        captured.update(kw)
        return {"status": "dispatched", "delegation_id": kw["delegation_id"]}

    class _Child:
        _delegate_role = "leaf"
        _subagent_id = "sa-1"
        session_id = "child"

        def close(self):
            pass

        def get_activity_summary(self):
            return {"api_call_count": 0, "current_tool": None, "last_activity_ts": time.time()}

    monkeypatch.setattr(ad, "dispatch_async_delegation_batch", fake_dispatch)
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: _Child())
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    })
    monkeypatch.setattr(dt, "_run_single_child", lambda **kw: _result(kw["task_index"]))
    monkeypatch.setattr(
        "gateway.session_context.async_delivery_supported", lambda: True
    )
    root = SimpleNamespace(
        valid_tool_names={"delegate_task"}, platform="telegram", _delegate_depth=0,
        _subagent_id=None, session_id="root-session", _interrupt_requested=False,
        _active_children=[], _active_children_lock=None,
        _current_turn_id="turn-root-1",
    )
    payload = json.loads(dt.delegate_task(
        parent_agent=root, background=True,
        tasks=[{"goal": "Part one of the work, in full."},
               {"goal": "Part two of the work, in full."}],
    ))
    assert payload["status"] == "dispatched"
    assert captured["fanout_delivery"] == expected
    assert captured["request_identity"]["turn_id"] == "turn-root-1"
    # What the root is told must match how the results will actually arrive.
    note = payload["note"]
    if expected == "joined":
        assert "together" in note
        assert "as soon as that child finishes" not in note
    else:
        assert "as soon as that child finishes" in note
        assert "together" not in note
