"""Joined fan-out, review round 1: write-transaction atomicity, settlement of
only the receipts a join carries, the straggler window, honest status.

Real async registry and durable ledger in a scratch ``CLOVER_HOME``; only the
child work and the clock are fake.
"""

from __future__ import annotations

import json
import queue
import threading

import pytest

from tools import async_delegation as ad
from tools.process_registry import format_process_notification, process_registry

BATCH = "deleg_f1f1f1f1"
GOALS = ["audit alpha", "audit beta", "audit gamma"]
IDENTITY = {"turn_id": "turn-9", "request_id": "req-9", "generation": 4}


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


def _result(index, status="completed", summary=None, error=None):
    return {
        "task_index": index, "status": status,
        "summary": summary if summary is not None else (
            f"result {index}" if status == "completed" else None),
        "error": error, "api_calls": 1, "duration_seconds": 1.0,
        "exit_reason": status,
    }


def _publish(index, **kw):
    return ad.publish_batch_child_completion(_result(index, **kw), delegation_id=BATCH)


def _dispatch(runner, **kw):
    kw.setdefault("fanout_delivery", "joined")
    handle = ad.dispatch_async_delegation_batch(
        goals=GOALS, context="brief", toolsets=None, role="leaf", model="m",
        session_key="agent:main:telegram:dm:100", parent_session_id="sess-o",
        runner=runner, delegation_id=BATCH, request_identity=IDENTITY, **kw,
    )
    assert handle["status"] == "dispatched", handle


def _state(delegation_id):
    return ad.get_durable_delegation(delegation_id)["delivery_state"]


def _drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


class _Clock:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(ad, "_now", c)
    return c


def _park_then_hang(gate, parked, indexes):
    def runner():
        for i in indexes:
            _publish(i)
        parked.set()
        gate.wait(30)
        return {"results": []}
    return runner


# ── write transaction (cross-process atomicity) ──────────────────────────

def test_write_transaction_takes_the_database_write_lock_before_reading():
    seen = []
    with ad._write_transaction() as conn:
        seen.append(conn.in_transaction)
        other = ad._connect()
        try:
            other.execute("PRAGMA busy_timeout=50")
            with pytest.raises(Exception, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()
    assert seen == [True]
    # ...and it is released afterwards.
    with ad._write_transaction():
        pass


# ── a join acknowledges ONLY the receipts it carries ─────────────────────

def test_join_ack_settles_only_included_receipts_and_orphan_is_promoted_to_late(reg):
    gate, parked = threading.Event(), threading.Event()
    try:
        _dispatch(_park_then_hang(gate, parked, [0, 2]))
        assert parked.wait(10)
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute(
                "UPDATE async_delegations SET owner_pid=? WHERE delegation_id=?",
                (99999999, BATCH),
            )
        with ad._records_lock:
            ad._records.clear()
        replay = queue.Queue()
        assert ad.restore_undelivered_completions(replay) == 1
        join = replay.get_nowait()
        assert join["incomplete_indexes"] == [1]
        assert sorted(join["included_receipts"]) == [
            f"{BATCH}:child:0", f"{BATCH}:child:2"]

        # A writer that raced the committed join leaves a parked receipt the
        # join does not carry (what the review reproduced across processes).
        orphan = {
            "type": "async_delegation", "delegation_id": f"{BATCH}:child:1",
            "batch_delegation_id": BATCH, "task_index": 1, "status": "completed",
            "summary": "UNIQUE_BETA", "goal": GOALS[1], "fanout_delivery": "joined",
            "task_count": 3, "session_key": "agent:main:telegram:dm:100",
            "parent_session_id": "sess-o",
        }
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute(
                """INSERT INTO async_delegations
                   (delegation_id, origin_session, state, dispatched_at,
                    completed_at, updated_at, event_json, result_json,
                    delivery_state) VALUES (?, 'o', 'completed', 1, 1, 1, ?, ?, 'joined')""",
                (orphan["delegation_id"], json.dumps(orphan),
                 json.dumps(_result(1, summary="UNIQUE_BETA"))),
            )

        claim = ad.claim_event_delivery(join, "gateway-a")
        assert claim
        ad.complete_event_delivery(join, claim)
        assert _state(f"{BATCH}:child:0") == "delivered"
        assert _state(f"{BATCH}:child:2") == "delivered"
        assert _state(f"{BATCH}:child:1") == "joined", "must not be marked delivered"

        restart = queue.Queue()
        assert ad.restore_undelivered_completions(restart) == 1
        (late,) = _drain(restart)
        assert late["delegation_id"] == f"{BATCH}:child:1"
        assert late["late_result"] is True and late["summary"] == "UNIQUE_BETA"
        assert _state(f"{BATCH}:child:1") == "pending"

        claim = ad.claim_event_delivery(late, "gateway-b")
        ad.complete_event_delivery(late, claim)
        assert _state(f"{BATCH}:child:1") == "delivered"
        again = queue.Queue()
        assert ad.restore_undelivered_completions(again) == 0
    finally:
        gate.set()


# ── honest status ────────────────────────────────────────────────────────

def test_stall_finalize_after_every_child_finished_reports_completed(reg):
    gate, parked = threading.Event(), threading.Event()
    try:
        _dispatch(_park_then_hang(gate, parked, [1, 0, 2]))
        assert parked.wait(10)
        with ad._records_lock:
            ad._records[BATCH]["status"] = "stalling"
        ad._finalize_stalled(BATCH)
        join = reg.get(timeout=5)
        assert join["incomplete_indexes"] == []
        assert join["status"] == "completed"
        assert not join.get("error")
        assert "stalled_after_quiet_seconds" not in join
        assert ad.get_durable_delegation(BATCH)["state"] == "completed"
    finally:
        gate.set()


def test_stall_finalize_with_a_missing_helper_still_reports_stalled(reg):
    gate, parked = threading.Event(), threading.Event()
    try:
        _dispatch(_park_then_hang(gate, parked, [0, 2]))
        assert parked.wait(10)
        with ad._records_lock:
            ad._records[BATCH]["status"] = "stalling"
        ad._finalize_stalled(BATCH)
        join = reg.get(timeout=5)
        assert join["incomplete_indexes"] == [1]
        assert join["status"] == "stalled"
    finally:
        gate.set()


# ── straggler window (fake clock) ────────────────────────────────────────

def test_straggler_window_joins_without_the_last_helper_then_one_late_result(reg, clock):
    notes = []
    gate, parked, release = threading.Event(), threading.Event(), threading.Event()
    done = threading.Event()
    finalized = threading.Event()
    original = ad._finalize_batch

    def runner():
        _publish(0, summary="alpha findings")
        _publish(2, summary="gamma findings")
        parked.set()
        assert release.wait(30)
        _publish(1, summary="beta findings, finally")
        done.set()
        return {"results": [_result(i) for i in range(3)]}

    def spy(*a, **k):
        try:
            return original(*a, **k)
        finally:
            finalized.set()

    ad._finalize_batch = spy
    try:
        _dispatch(runner, straggler_seconds=300,
                  straggler_notify=lambda idx, waiting: notes.append((idx, waiting)))
        assert parked.wait(10)
        assert notes == [([1], True)]          # card: "waiting on helper 2"
        assert reg.empty()

        clock.t += 299
        assert ad._sweep_stragglers(clock()) == []
        assert reg.empty()

        clock.t += 2
        assert ad._sweep_stragglers(clock()) == [BATCH]
        join = reg.get_nowait()
        assert join["incomplete_indexes"] == [1]
        assert [r["status"] for r in join["results"]] == ["completed", "incomplete", "completed"]
        assert join["status"] == "unknown"
        assert notes[-1] == ([1], False)       # the card stops saying "waiting"
        assert ad._sweep_stragglers(clock() + 10_000) == [], "never a second join"

        release.set()
        assert done.wait(10) and finalized.wait(10)
        late = reg.get(timeout=5)
        assert late["late_result"] is True
        assert late["delegation_id"] == f"{BATCH}:child:1"
        assert late["batch_delegation_id"] == BATCH
        assert "late" in format_process_notification(late).lower()
        assert reg.empty(), "runner return must not add a second join"
    finally:
        ad._finalize_batch = original
        gate.set()
        release.set()


def test_straggler_window_off_when_zero(reg, clock):
    gate, parked = threading.Event(), threading.Event()
    try:
        _dispatch(_park_then_hang(gate, parked, [0, 2]), straggler_seconds=0)
        assert parked.wait(10)
        assert ad._sweep_stragglers(clock() + 10_000) == []
        assert reg.empty()
    finally:
        gate.set()


def test_straggler_window_waits_while_more_than_one_helper_is_unfinished(reg, clock):
    gate, parked = threading.Event(), threading.Event()
    try:
        _dispatch(_park_then_hang(gate, parked, [0]), straggler_seconds=5)
        assert parked.wait(10)
        assert ad._sweep_stragglers(clock() + 10_000) == []
        assert reg.empty()
    finally:
        gate.set()


def test_straggler_window_never_applies_to_each_mode(reg, clock):
    gate, parked = threading.Event(), threading.Event()
    try:
        _dispatch(_park_then_hang(gate, parked, [0, 2]),
                  fanout_delivery="each", straggler_seconds=5)
        assert parked.wait(10)
        assert ad._sweep_stragglers(clock() + 10_000) == []
        events = _drain(reg)
        assert [e["delegation_id"] for e in events] == [
            f"{BATCH}:child:0", f"{BATCH}:child:2"]
    finally:
        gate.set()


def test_monitor_thread_runs_the_straggler_sweep(reg, clock, monkeypatch):
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.05)
    gate, parked = threading.Event(), threading.Event()
    try:
        _dispatch(_park_then_hang(gate, parked, [0, 2]), straggler_seconds=60)
        assert parked.wait(10)
        # The window opened at the fake clock's 1000.0; move past it.
        clock.t += 61
        join = reg.get(timeout=10)
        assert join["incomplete_indexes"] == [1]
    finally:
        gate.set()


class _HookedLock:
    """``_records_lock`` stand-in: when the MONITOR thread leaves its first
    ``with`` block (it has just decided there is nothing to monitor), tell the
    test and hold the thread until the test lets it go. Every other user of
    the lock passes straight through."""

    def __init__(self, inner, decided, proceed):
        self._inner = inner
        self._decided = decided
        self._proceed = proceed
        self._armed = True

    def __enter__(self):
        self._inner.acquire()
        return self

    def __exit__(self, *exc):
        self._inner.release()
        me = threading.current_thread()
        if self._armed and me.name == "async-delegate-stale-monitor":
            self._armed = False
            self._decided.set()
            assert self._proceed.wait(15), "test never released the monitor"
        return False


def test_dispatch_landing_as_the_monitor_decides_to_exit_is_still_monitored(
    reg, clock, monkeypatch
):
    """The monitor decided "nothing to watch" and is about to exit when a
    dispatch lands. That dispatch must never end up unmonitored: the exit
    decision is re-made under the lock the dispatch's ensure also takes."""
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.05)
    decided, proceed = threading.Event(), threading.Event()
    hooked = _HookedLock(ad._records_lock, decided, proceed)
    monkeypatch.setattr(ad, "_records_lock", hooked)
    # The monitor's pass that follows the held decision. Once it is over, the
    # monitor has either exited (the bug) or looped on; only then does the
    # fake clock move, so nothing but a live monitor can produce the join.
    passed = threading.Event()
    real_sweep = ad._sweep_stragglers

    def sweep(now):
        try:
            return real_sweep(now)
        finally:
            if proceed.is_set():
                passed.set()

    monkeypatch.setattr(ad, "_sweep_stragglers", sweep)
    gate, parked = threading.Event(), threading.Event()
    try:
        ad._ensure_stale_monitor()          # no records yet: it will want out
        assert decided.wait(10), "monitor never swept"
        # Window: the monitor has decided and not yet returned.
        _dispatch(_park_then_hang(gate, parked, [0, 2]), straggler_seconds=60)
        assert parked.wait(10)
        proceed.set()
        assert passed.wait(10)
        clock.t += 61
        join = reg.get(timeout=10)          # only a live monitor produces this
        assert join["incomplete_indexes"] == [1]
    finally:
        proceed.set()
        gate.set()


def test_exiting_monitor_clears_its_slot_so_the_next_dispatch_starts_a_new_one(
    reg, monkeypatch
):
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.05)
    ad._ensure_stale_monitor()
    first = ad._monitor_thread
    first.join(10)
    assert not first.is_alive(), "an idle monitor exits on its own"
    assert ad._monitor_thread is not first or ad._monitor_thread is None
    ad._ensure_stale_monitor()
    second = ad._monitor_thread
    assert second is not first and second.is_alive()


# ── busy ledger: real write-lock contention longer than the busy timeout ──

import contextlib
import sqlite3


@contextlib.contextmanager
def _ledger_write_locked():
    """Another writer (as SessionDB is) holds state.db's write lock."""
    other = sqlite3.connect(str(ad._db_path()), timeout=0)
    other.execute("BEGIN IMMEDIATE")
    try:
        yield
    finally:
        other.rollback()
        other.close()


@pytest.fixture
def short_busy(monkeypatch):
    """Contention that outlasts the busy timeout, without a 10 s wait."""
    monkeypatch.setattr(ad, "_BUSY_TIMEOUT_SECONDS", 0.2)


def _row(delegation_id, column):
    with ad._DB_LOCK, ad._transaction() as conn:
        found = conn.execute(
            f"SELECT {column} FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
    return found[0] if found else None


def test_straggler_sweep_on_a_busy_ledger_commits_nothing_and_retries_next_tick(
    reg, clock, short_busy
):
    gate, parked = threading.Event(), threading.Event()
    try:
        _dispatch(_park_then_hang(gate, parked, [0, 2]), straggler_seconds=60)
        assert parked.wait(10)
        clock.t += 61
        with _ledger_write_locked():
            assert ad._sweep_stragglers(clock()) == []   # busy: not raised
        with ad._records_lock:
            assert ad._records[BATCH]["_straggler_joined"] is False
        assert reg.empty()
        assert _row(BATCH, "state") == "running"
        # Storage is healthy again: the very next tick joins, no new dispatch.
        assert ad._sweep_stragglers(clock()) == [BATCH]
        join = reg.get_nowait()
        assert join["incomplete_indexes"] == [1]
        assert _row(BATCH, "state") == "unknown"
    finally:
        gate.set()


def test_monitor_thread_survives_a_busy_ledger_and_joins_once_it_clears(
    reg, clock, short_busy, monkeypatch
):
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.02)
    crashed = threading.Event()
    monkeypatch.setattr(threading, "excepthook", lambda args: crashed.set())
    failed = threading.Event()
    real_push = ad._push_joined_batch_completion

    def push(*a, **k):
        try:
            return real_push(*a, **k)
        except Exception:
            failed.set()
            raise

    monkeypatch.setattr(ad, "_push_joined_batch_completion", push)
    gate, parked = threading.Event(), threading.Event()
    try:
        _dispatch(_park_then_hang(gate, parked, [0, 2]), straggler_seconds=60,
                  progress_fn=lambda: (1, False))
        assert parked.wait(10)
        monitor = ad._monitor_thread
        with _ledger_write_locked():
            clock.t += 61
            assert failed.wait(10), "the monitor never attempted the join"
            assert monitor.is_alive()
            assert not crashed.is_set()
            assert reg.empty()
        join = reg.get(timeout=10)          # next tick, no restart/dispatch
        assert join["incomplete_indexes"] == [1]
        assert ad._monitor_thread is monitor and monitor.is_alive()
        assert not crashed.is_set()
    finally:
        gate.set()


def test_stall_force_finalize_on_a_busy_ledger_is_left_as_it_was_then_retried(
    reg, short_busy
):
    gate, parked = threading.Event(), threading.Event()
    interrupted = []
    try:
        _dispatch(_park_then_hang(gate, parked, [0, 2]),
                  progress_fn=lambda: (1, False),
                  interrupt_fn=lambda: interrupted.append(1))
        assert parked.wait(10)
        with ad._records_lock:
            record = ad._records[BATCH]
            progress_fn = record["progress_fn"]
            interrupt_fn = record["interrupt_fn"]
            record["status"] = "stalling"
            record["_interrupted_at"] = 1.0     # grace long expired
        with _ledger_write_locked():
            assert ad._monitor_tick() is True    # swept, did not raise
        with ad._records_lock:
            record = ad._records[BATCH]
            assert record["status"] == "stalling"
            assert record["progress_fn"] is progress_fn
            assert record["interrupt_fn"] is interrupt_fn
            assert record["completed_at"] is None
        assert reg.empty()
        assert _row(BATCH, "state") == "running"

        ad._monitor_tick()                       # healthy: the retry
        join = reg.get_nowait()
        assert join["status"] == "stalled"
        assert join["incomplete_indexes"] == [1]
        with ad._records_lock:
            assert ad._records[BATCH]["status"] == "stalled"
    finally:
        gate.set()


def test_runner_finalize_on_a_busy_ledger_is_kept_and_recorded_by_a_later_tick(
    reg, short_busy, monkeypatch
):
    parked, release, returned = (threading.Event() for _ in range(3))
    attempted = threading.Event()
    original = ad._finalize_batch

    def spy(*a, **k):
        try:
            return original(*a, **k)
        finally:
            attempted.set()

    monkeypatch.setattr(ad, "_finalize_batch", spy)

    def runner():
        _publish(0)
        _publish(1)
        _publish(2)
        parked.set()
        assert release.wait(30)
        returned.set()
        return {"results": [_result(i) for i in range(3)]}

    try:
        _dispatch(runner)
        assert parked.wait(10)
        with _ledger_write_locked():
            release.set()
            assert returned.wait(10) and attempted.wait(10)
            # Runner is done but its completion is not durable: the record
            # must neither claim to be finished nor be forgotten.
            with ad._records_lock:
                rec = ad._records[BATCH]
                assert rec["status"] == "running"
                assert rec.get("_pending_finalize") is not None
            assert reg.empty()
        ad._monitor_tick()                      # healthy: the retry
        join = reg.get(timeout=5)
        assert join["incomplete_indexes"] == []
        assert [r["status"] for r in join["results"]] == ["completed"] * 3
        with ad._records_lock:
            assert ad._records[BATCH]["status"] == "completed"
        ad._monitor_tick()
        assert reg.empty(), "retried completion is recorded exactly once"
    finally:
        release.set()


def test_child_publish_on_a_busy_ledger_leaves_nothing_half_set(reg, short_busy):
    gate, parked = threading.Event(), threading.Event()
    try:
        _dispatch(_park_then_hang(gate, parked, [0]), straggler_seconds=60)
        assert parked.wait(10)
        with _ledger_write_locked():
            with pytest.raises(sqlite3.OperationalError):
                _publish(2)
        assert _row(f"{BATCH}:child:2", "delivery_state") is None
        with ad._records_lock:
            assert ad._records[BATCH]["_straggler_since"] is None
        # The publisher (or the batch aggregate) delivers it afterwards.
        assert _publish(2) == "parked"
        with ad._records_lock:
            assert ad._records[BATCH]["_straggler_since"] is not None
    finally:
        gate.set()


def test_restore_on_a_busy_ledger_changes_nothing_and_succeeds_afterwards(
    reg, short_busy
):
    gate, parked = threading.Event(), threading.Event()
    try:
        _dispatch(_park_then_hang(gate, parked, [0, 2]))
        assert parked.wait(10)
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute(
                "UPDATE async_delegations SET owner_pid=? WHERE delegation_id=?",
                (99999999, BATCH),
            )
        with ad._records_lock:
            ad._records.clear()
        replay = queue.Queue()
        with _ledger_write_locked():
            with pytest.raises(sqlite3.OperationalError):
                ad.restore_undelivered_completions(replay)
        assert replay.empty()
        assert _row(BATCH, "state") == "running"
        assert ad.restore_undelivered_completions(replay) == 1
        assert replay.get_nowait()["incomplete_indexes"] == [1]
    finally:
        gate.set()


# ── a straggler whose own publish failed is still delivered (late) ───────

def _straggler_runner(parked, release, retained, attempted_publish):
    """Parks 0 and 2, then (once released) the straggler's publish hits the
    busy ledger, is logged and RETAINED in the aggregate like delegate_tool's
    _publish_child does, and the runner returns."""
    def runner():
        _publish(0)
        _publish(2)
        parked.set()
        assert release.wait(30)
        try:
            _publish(1, summary="UNIQUE_BETA_RESULT")
        except sqlite3.OperationalError as exc:
            retained.append(repr(exc))
        attempted_publish.set()
        return {"results": [_result(0), _result(1, summary="UNIQUE_BETA_RESULT"),
                            _result(2)]}
    return runner


def _spy_finalize(monkeypatch):
    done = threading.Event()
    original = ad._finalize_batch

    def spy(*a, **k):
        try:
            return original(*a, **k)
        finally:
            done.set()

    monkeypatch.setattr(ad, "_finalize_batch", spy)
    return done


def test_straggler_publish_failing_on_a_busy_ledger_lands_once_as_late_when_it_clears(
    reg, clock, short_busy, monkeypatch
):
    parked, release, attempted = (threading.Event() for _ in range(3))
    retained = []
    finalized = _spy_finalize(monkeypatch)
    try:
        _dispatch(_straggler_runner(parked, release, retained, attempted),
                  straggler_seconds=60)
        assert parked.wait(10)
        clock.t += 61
        assert ad._sweep_stragglers(clock()) == [BATCH]
        join = reg.get_nowait()
        assert join["incomplete_indexes"] == [1]

        with _ledger_write_locked():
            release.set()
            assert attempted.wait(10) and finalized.wait(10)
            assert retained, "the straggler's publish did fail"
            assert _row(f"{BATCH}:child:1", "delivery_state") is None
            assert reg.empty()
        # The ledger is free: the very next tick records it. No new dispatch,
        # no restart.
        ad._monitor_tick()
        late = reg.get_nowait()
        assert late["late_result"] is True
        assert late["delegation_id"] == f"{BATCH}:child:1"
        assert late["summary"] == "UNIQUE_BETA_RESULT"
        assert late["batch_delegation_id"] == BATCH
        assert _row(f"{BATCH}:child:1", "delivery_state") == "pending"
        ad._monitor_tick()
        assert reg.empty(), "retried exactly once"

        # A restart now restores exactly that one, which is delivered once.
        replay = queue.Queue()
        assert ad.restore_undelivered_completions(replay) == 1
        (restored,) = _drain(replay)
        assert restored["delegation_id"] == f"{BATCH}:child:1"
        claim = ad.claim_event_delivery(restored, "gateway-a")
        assert claim and ad.claim_event_delivery(late, "gateway-b") is None
        ad.complete_event_delivery(restored, claim)
        again = queue.Queue()
        assert ad.restore_undelivered_completions(again) == 0
    finally:
        release.set()


def test_stall_finalized_batch_whose_straggler_publish_failed_still_delivers_it(
    reg, short_busy, monkeypatch
):
    parked, release, attempted = (threading.Event() for _ in range(3))
    retained = []
    finalized = _spy_finalize(monkeypatch)
    try:
        _dispatch(_straggler_runner(parked, release, retained, attempted),
                  progress_fn=lambda: (1, False))
        assert parked.wait(10)
        with ad._records_lock:
            rec = ad._records[BATCH]
            rec["status"] = "stalling"
            rec["_interrupted_at"] = 1.0
        ad._monitor_tick()                       # healthy: stall join
        join = reg.get_nowait()
        assert join["status"] == "stalled" and join["incomplete_indexes"] == [1]
        with ad._records_lock:
            assert ad._records[BATCH]["status"] == "stalled"

        with _ledger_write_locked():
            release.set()
            assert attempted.wait(10) and finalized.wait(10)
            assert retained
            with ad._records_lock:
                assert BATCH in ad._pending_rescues
            assert reg.empty()
        ad._monitor_tick()
        late = reg.get_nowait()
        assert late["late_result"] is True and late["summary"] == "UNIQUE_BETA_RESULT"
        with ad._records_lock:
            assert BATCH not in ad._pending_rescues
        ad._monitor_tick()
        assert reg.empty()
    finally:
        release.set()


def test_monitor_stays_alive_for_a_pending_late_result(reg, short_busy, monkeypatch):
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.02)
    with ad._records_lock:
        ad._pending_rescues[BATCH] = [_result(1)]
    ad._ensure_stale_monitor()
    first = ad._monitor_thread
    assert not ad._monitor_try_exit(), "a queued late result needs the monitor"
    with ad._records_lock:
        ad._pending_rescues.clear()
    first.join(10)
    assert not first.is_alive()


# ── delegate_task wiring ─────────────────────────────────────────────────

def _write_delegation_config(**values):
    import yaml
    from clover_constants import get_clover_home

    (get_clover_home() / "config.yaml").write_text(
        yaml.safe_dump({"delegation": values}), encoding="utf-8")


@pytest.mark.parametrize("configured, expected", [
    (None, 300.0), (120, 120.0), ("45", 45.0), (0, 0.0), (-3, 0.0),
    ("soon", 300.0), (True, 300.0),
])
def test_config_resolves_straggler_seconds(configured, expected):
    import tools.delegate_tool as dt

    _write_delegation_config(
        **({} if configured is None else {"fanout_straggler_seconds": configured}))
    assert dt._get_fanout_straggler_seconds() == expected


def test_default_config_ships_a_straggler_window():
    from clover_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["delegation"]["fanout_straggler_seconds"] > 0
