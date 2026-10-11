"""C1.5 B1 round-3 review probes (Fable).

Probe 3's intermediate assertions encoded the bug (the stall path firing for
an already-returned runner); they now assert the record stays owned by the
retry path. Its final exactly-once assertion is unchanged.

Real async registry, real SQLite ledger, real forked processes for the
contention holder and for the dying owner. Only the child work and the clock
are fake.
"""
from __future__ import annotations

import os
import queue
import sqlite3
import threading
import time

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry
from tests.tools.test_async_joined_fanout_fix1 import (  # noqa: F401  (fixtures)
    BATCH, _dispatch, _drain, _ledger_write_locked, _publish, _result, _row,
    clock, reg,
)

CHILD1 = f"{BATCH}:child:1"


def _pipe():
    r, w = os.pipe()
    return os.fdopen(r, "r"), os.fdopen(w, "w", buffering=1)


def _fork_lock_holder():
    """Another PROCESS (a second gateway's SessionDB) holds state.db's write lock."""
    to_child_r, to_child_w = _pipe()
    from_child_r, from_child_w = _pipe()
    pid = os.fork()
    if pid == 0:
        try:
            conn = sqlite3.connect(str(ad._db_path()), timeout=0)
            conn.execute("BEGIN IMMEDIATE")
            from_child_w.write("held\n")
            to_child_r.readline()
            conn.rollback()
            conn.close()
        finally:
            os._exit(0)
    assert from_child_r.readline().strip() == "held"

    def release():
        to_child_w.write("release\n")
        os.waitpid(pid, 0)

    return release


def _straggler_runner(parked, release, retained, attempted, summary):
    def runner():
        _publish(0)
        _publish(2)
        parked.set()
        assert release.wait(30)
        try:
            _publish(1, summary=summary)
        except sqlite3.OperationalError as exc:
            retained.append(repr(exc))
        attempted.set()
        return {"results": [_result(0), _result(1, summary=summary), _result(2)]}
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


def _record():
    with ad._records_lock:
        rec = ad._records.get(BATCH)
        return dict(rec) if rec else None


# ── 1. runner returns while ANOTHER PROCESS holds the write lock; real monitor thread ──

def test_runner_returning_under_cross_process_contention_lands_the_straggler_once(
    reg, clock, monkeypatch
):
    monkeypatch.setattr(ad, "_BUSY_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.05)
    parked, release, attempted = (threading.Event() for _ in range(3))
    retained = []
    try:
        _dispatch(_straggler_runner(parked, release, retained, attempted, "UNIQUE_R3"),
                  straggler_seconds=60)
        assert parked.wait(10)
        clock.t += 61
        assert ad._sweep_stragglers(clock()) == [BATCH]
        join = reg.get_nowait()
        assert join["incomplete_indexes"] == [1]

        unlock = _fork_lock_holder()
        try:
            release.set()
            assert attempted.wait(10) and retained, "straggler publish did fail"
            deadline = time.time() + 10
            rec = None
            while time.time() < deadline:
                rec = _record()
                if rec and rec.get("_pending_finalize") is not None:
                    break
                time.sleep(0.02)
            assert rec and rec.get("_pending_finalize") is not None, rec
            assert rec["status"] == "running"
            time.sleep(0.6)  # ~10 ticks under contention
            assert reg.empty()
            assert _row(CHILD1, "delivery_state") is None
            t = ad._monitor_thread
            assert t is not None and t.is_alive(), "monitor must stay alive"
        finally:
            unlock()
        late = reg.get(timeout=10)
        assert late["late_result"] is True
        assert late["delegation_id"] == CHILD1
        assert late["summary"] == "UNIQUE_R3"
        assert late["batch_delegation_id"] == BATCH
        for k in ("request_turn_id", "request_id", "request_generation"):
            assert k in late, k
        time.sleep(0.6)
        assert reg.empty(), "exactly once"
        rec = _record()
        assert rec["status"] == "completed" and rec.get("_pending_finalize") is None
        claim = ad.claim_event_delivery(late, "gw-a")
        assert claim and ad.claim_event_delivery(late, "gw-b") is None
        ad.complete_event_delivery(late, claim)
        again = queue.Queue()
        assert ad.restore_undelivered_completions(again) == 0
        # Monitor has nothing left and exits.
        deadline = time.time() + 5
        while time.time() < deadline and ad._monitor_thread is not None:
            time.sleep(0.05)
        assert ad._monitor_thread is None
    finally:
        release.set()


# ── 2. the builder's stated gap, measured against `each` under the SAME failure ──

def _owner_dies_before_return(mode, monkeypatch, clock):
    """Forked owner: siblings recorded and delivered, straggler's publish fails on
    a lock held by THIS process, owner dies before its runner returns.
    Returns what a restart restores."""
    monkeypatch.setattr(ad, "_BUSY_TIMEOUT_SECONDS", 0.3)
    to_child_r, to_child_w = _pipe()
    from_child_r, from_child_w = _pipe()
    pid = os.fork()
    if pid == 0:
        try:
            def runner():
                q = process_registry.completion_queue
                _publish(0)
                _publish(2)
                if mode == "joined_after_straggler_join":
                    clock.t += 61
                    assert ad._sweep_stragglers(clock()) == [BATCH]
                for evt in _drain(q):  # the owner delivered what it had
                    c = ad.claim_event_delivery(evt, "gw")
                    ad.complete_event_delivery(evt, c)
                from_child_w.write("ready\n")
                to_child_r.readline()
                try:
                    _publish(1, summary="UNIQUE_GAP")
                except sqlite3.OperationalError as exc:
                    from_child_w.write(f"failed {exc!r}\n")
                else:
                    from_child_w.write("published\n")
                os._exit(0)

            _dispatch(runner, fanout_delivery="each" if mode == "each" else "joined",
                      straggler_seconds=60)
            time.sleep(15)
        finally:
            os._exit(1)
    assert from_child_r.readline().strip() == "ready"
    other = sqlite3.connect(str(ad._db_path()), timeout=0)
    other.execute("BEGIN IMMEDIATE")
    to_child_w.write("go\n")
    outcome = from_child_r.readline().strip()
    os.waitpid(pid, 0)
    other.rollback()
    other.close()
    assert outcome.startswith("failed"), outcome
    restored = queue.Queue()
    n = ad.restore_undelivered_completions(restored)
    return n, _drain(restored), ad.get_durable_delegation(BATCH)


def test_gap_each_mode_restart_reports_unknown_for_the_lost_child(reg, clock, monkeypatch):
    n, events, batch = _owner_dies_before_return("each", monkeypatch, clock)
    assert _row(CHILD1, "delivery_state") is None  # the result itself is gone
    assert n == 1
    (evt,) = events
    assert evt["status"] == "unknown" and evt["already_published_task_indexes"] == [0, 2]
    print("EACH:", batch["state"], evt["error"])


def test_gap_joined_open_window_restart_joins_with_the_slot_incomplete(reg, clock, monkeypatch):
    n, events, batch = _owner_dies_before_return("joined_open_window", monkeypatch, clock)
    assert _row(CHILD1, "delivery_state") is None
    assert n == 1
    (evt,) = events
    assert evt["incomplete_indexes"] == [1]
    assert sorted(e["task_index"] for e in evt["results"] if not e.get("incomplete")) == [0, 2]
    print("JOINED-OPEN:", batch["state"], evt["status"])


def test_gap_joined_after_straggler_join_restart_restores_nothing_more(reg, clock, monkeypatch):
    n, events, batch = _owner_dies_before_return(
        "joined_after_straggler_join", monkeypatch, clock)
    assert _row(CHILD1, "delivery_state") is None
    # The join already went out naming slot 1 incomplete; a restart has nothing
    # durable left to say about it (same result loss as `each`, notice earlier).
    assert n == 0 and events == []
    assert batch["state"] not in ("running", "finalizing")
    print("JOINED-AFTER:", batch["state"], batch["delivery_state"])


# ── 3. edge: contention outlasting idle+grace while a finalize is pending ──

def test_pending_finalize_survives_a_stall_that_fires_during_the_contention(
    reg, clock, monkeypatch
):
    monkeypatch.setattr(ad, "_BUSY_TIMEOUT_SECONDS", 0.2)
    parked, release, attempted = (threading.Event() for _ in range(3))
    retained = []
    finalized = _spy_finalize(monkeypatch)
    try:
        _dispatch(_straggler_runner(parked, release, retained, attempted, "UNIQUE_STALL"),
                  straggler_seconds=60, progress_fn=lambda: (1, False))
        assert parked.wait(10)
        clock.t += 61
        assert ad._sweep_stragglers(clock()) == [BATCH]
        assert reg.get_nowait()["incomplete_indexes"] == [1]
        with _ledger_write_locked():
            release.set()
            assert attempted.wait(10) and finalized.wait(10) and retained
            rec = _record()
            assert rec["_pending_finalize"] is not None and rec["status"] == "running"
            # Contention outlasts idle (450 s) + grace (120 s) in production.
            monkeypatch.setattr(ad, "_STALE_IDLE_SECONDS", 0.0)
            monkeypatch.setattr(ad, "_STALL_GRACE_SECONDS", 0.0)
            # The runner has returned, so there is nothing to stall-detect:
            # no tick may turn the record 'stalling' and orphan the pending
            # completion (the original probe asserted that state).
            for _ in range(3):
                ad._monitor_tick()
                rec = _record()
                assert rec["status"] == "running", rec
                assert rec["_pending_finalize"] is not None
                assert rec["progress_fn"] is None
            assert reg.empty()
        for _ in range(3):
            ad._monitor_tick()
        events = _drain(reg)
        late = [e for e in events if e.get("delegation_id") == CHILD1]
        assert len(late) == 1, (
            events, _row(CHILD1, "delivery_state"), _record().get("status"),
            _record().get("_pending_finalize") is not None,
        )
    finally:
        release.set()


# ── 4. edge: the finalized record was pruned before the runner returned ──

def test_rescue_still_lands_when_the_finalized_record_was_pruned(reg, monkeypatch):
    monkeypatch.setattr(ad, "_BUSY_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(ad, "_MAX_RETAINED_COMPLETED", 1)
    parked, release, attempted = (threading.Event() for _ in range(3))
    retained = []
    finalized = _spy_finalize(monkeypatch)
    try:
        _dispatch(_straggler_runner(parked, release, retained, attempted, "UNIQUE_PRUNE"),
                  progress_fn=lambda: (1, False))
        assert parked.wait(10)
        with ad._records_lock:
            ad._records[BATCH]["status"] = "stalling"
            ad._records[BATCH]["_interrupted_at"] = 1.0
        ad._monitor_tick()
        assert reg.get_nowait()["status"] == "stalled"
        assert _record()["status"] == "stalled"
        with ad._records_lock:
            for i in (1, 2):
                ad._records[f"other{i}"] = {
                    "delegation_id": f"other{i}", "status": "completed",
                    "completed_at": time.time() + 1000,
                }
            ad._prune_completed_locked()
            assert BATCH not in ad._records
        with _ledger_write_locked():
            release.set()
            assert attempted.wait(10) and finalized.wait(10) and retained
            assert reg.empty()
        for _ in range(2):
            ad._monitor_tick()
        events = _drain(reg)
        late = [e for e in events if e.get("delegation_id") == CHILD1]
        assert len(late) == 1, (events, _row(CHILD1, "delivery_state"), dict(ad._pending_rescues))
    finally:
        release.set()
