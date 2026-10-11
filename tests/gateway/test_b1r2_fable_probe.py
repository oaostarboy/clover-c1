"""Round-2 review probes (Fable): real registry, SQLite ledger, SessionDB, gateway.

1/2: the straggler SWEEP as the finalizer against a forked publisher, in both
     schedules BEGIN IMMEDIATE still allows.
3:   a straggler whose own publish raised (ledger write failure) after the
     straggler join already fired: is the retained result still delivered?
4:   bounded blocking: the ledger's BEGIN IMMEDIATE behind another writer on
     the same state.db, with _DB_LOCK held meanwhile.
"""
from __future__ import annotations

import os
import queue
import select
import sqlite3
import threading
import time

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry
from tests.gateway.test_joined_fanout_gateway import (  # noqa: F401
    BATCH, GOALS, _clean_state, _result, world,
)
from tests.gateway.test_b1_astra_race import (
    _fork_publisher, _park_two, _unique_deliveries,
)


def _arm_straggler(seconds=300.0, since=0.0):
    with ad._records_lock:
        rec = ad._records[BATCH]
        rec["_straggler_seconds"] = seconds
        rec["_straggler_since"] = since


@pytest.mark.asyncio
async def test_sweep_blocks_behind_a_publisher_inside_its_transaction(world):
    gate = _park_two(world)
    pid, ready_r, ready_w, resume_r, resume_w = _fork_publisher(
        lambda sql: sql.lstrip().startswith("INSERT OR IGNORE INTO async_delegations"))
    out = []
    sweeper = None
    try:
        assert select.select([ready_r], [], [], 10)[0]
        assert os.read(ready_r, 1) == b"R"
        _arm_straggler()
        sweeper = threading.Thread(target=lambda: out.append(ad._sweep_stragglers(10_000.0)))
        sweeper.start()
        sweeper.join(1.0)
        assert sweeper.is_alive(), "sweep committed a join under the publisher's read"
        assert world.events_nowait() == []
        os.write(resume_w, b"G")
        sweeper.join(15)
        assert not sweeper.is_alive()
        assert select.select([ready_r], [], [], 10)[0]
        assert os.read(ready_r, 100).decode() == "parked"
        assert os.waitpid(pid, 0)[1] == 0
        assert out == [[BATCH]]
        (join,) = world.events(1)
        assert join["incomplete_indexes"] == [] and join["status"] == "completed"
        assert sorted(join["included_receipts"]) == sorted(f"{BATCH}:child:{i}" for i in range(3))
        assert await world.runner._deliver_async_delegation_group([join]) is True
        assert len(_unique_deliveries(world)) == 1
        assert all(ad.get_durable_delegation(f"{BATCH}:child:{i}")["delivery_state"] == "delivered" for i in range(3))
        assert world.restore() == []
        assert ad._sweep_stragglers(20_000.0) == []
    finally:
        gate.set()
        if sweeper is not None:
            sweeper.join(5)
        for fd in (ready_r, ready_w, resume_r, resume_w):
            os.close(fd)


@pytest.mark.asyncio
async def test_sweep_wins_then_publisher_is_one_late_result_with_request_tuple(world):
    gate = _park_two(world)
    pid, ready_r, ready_w, resume_r, resume_w = _fork_publisher(
        lambda sql: sql.lstrip().upper().startswith("BEGIN IMMEDIATE"))
    try:
        assert select.select([ready_r], [], [], 10)[0]
        assert os.read(ready_r, 1) == b"R"
        _arm_straggler()
        assert ad._sweep_stragglers(10_000.0) == [BATCH]
        (join,) = world.events(1)
        assert join["incomplete_indexes"] == [1] and join["status"] == "unknown"
        os.write(resume_w, b"G")
        assert select.select([ready_r], [], [], 10)[0]
        assert os.read(ready_r, 100).decode() == "late"
        assert os.waitpid(pid, 0)[1] == 0
        assert ad.get_durable_delegation(f"{BATCH}:child:1")["delivery_state"] == "pending"
        assert await world.runner._deliver_async_delegation_group([join]) is True
        assert _unique_deliveries(world) == []
        assert ad.get_durable_delegation(f"{BATCH}:child:1")["delivery_state"] == "pending"
        meta = world.accepted[0].metadata
        assert (meta["request_turn_id"], meta["request_id"], meta["request_generation"]) == ("turn-1", "req-1", 1)
        # The forked publisher enqueued the late event in ITS process; here the
        # owner restarts and restores exactly that one.
        replay = world.restore()
        assert [e["delegation_id"] for e in replay] == [f"{BATCH}:child:1"]
        other = world.new_runner()
        assert await other._deliver_async_delegation_group(replay) is True
        assert len(_unique_deliveries(world)) == 1
        late_meta = world.accepted[1].metadata
        assert late_meta["late_result"] is True and late_meta["batch_delegation_id"] == BATCH
        assert late_meta["request_generation"] == 1
        assert ad.get_durable_delegation(f"{BATCH}:child:1")["delivery_state"] == "delivered"
        assert world.restore() == []
        assert await other._deliver_async_delegation_group(replay) is None
    finally:
        gate.set()
        for fd in (ready_r, ready_w, resume_r, resume_w):
            os.close(fd)


def test_straggler_whose_publish_failed_after_the_straggler_join(world, monkeypatch):
    """delegate_tool's _publish_child catches a publish exception and RETAINS the
    entry in the runner's combined results. Round 1 delivered it through the
    runner's own join; after a straggler join that finalize is a no-op."""
    clock = [1000.0]
    monkeypatch.setattr(ad, "_now", lambda: clock[0])
    parked, release, done = threading.Event(), threading.Event(), threading.Event()
    retained = []

    def runner():
        ad.publish_batch_child_completion(_result(0), delegation_id=BATCH)
        ad.publish_batch_child_completion(_result(2), delegation_id=BATCH)
        parked.set()
        release.wait(30)
        real = ad._connect
        ad._connect = lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
        try:
            try:
                ad.publish_batch_child_completion(_result(1, summary="UNIQUE_BETA_RESULT"), delegation_id=BATCH)
            except Exception as exc:  # what _publish_child does: log and retain
                retained.append(repr(exc))
        finally:
            ad._connect = real
        done.set()
        return {"results": [_result(0), _result(1, summary="UNIQUE_BETA_RESULT"), _result(2)]}

    handle = ad.dispatch_async_delegation_batch(
        goals=GOALS, context="c", toolsets=None, role="leaf", model="m",
        session_key=world.entry.session_key, parent_session_id=world.entry.session_id,
        runner=runner, delegation_id=BATCH, fanout_delivery="joined",
        straggler_seconds=300,
    )
    assert handle["status"] == "dispatched"
    assert parked.wait(10)
    with ad._records_lock:
        assert ad._records[BATCH]["_straggler_since"] == 1000.0
    clock[0] += 301
    assert ad._sweep_stragglers(clock[0]) == [BATCH]
    (join,) = world.events(1)
    assert join["incomplete_indexes"] == [1]
    release.set()
    assert done.wait(10)
    assert retained, "publish did raise"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with ad._records_lock:
            rec = ad._records.get(BATCH)
        if rec is None or rec.get("status") not in ("running", "finalizing"):
            break
        time.sleep(0.05)
    assert rec is None or rec.get("status") not in ("running", "finalizing")
    extra = world.events_nowait()
    durable = ad.get_durable_delegation(f"{BATCH}:child:1")
    replay = world.restore()
    print({"extra_events": [e.get("delegation_id") for e in extra], "child1_row": durable,
           "replay": [e.get("delegation_id") for e in replay]})
    assert any("UNIQUE_BETA_RESULT" in str(e.get("summary")) for e in extra) or durable is not None or replay, \
        "LOST: retained result never delivered, no durable row, nothing to restore"


def test_ledger_write_blocks_boundedly_behind_another_writer_on_state_db(world):
    gate = _park_two(world)
    holder = sqlite3.connect(ad._db_path(), timeout=1)
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE async_delegations SET updated_at=updated_at WHERE 0")
        t0 = time.monotonic()
        outcome, err = [], []

        def pub():
            try:
                outcome.append(ad.publish_batch_child_completion(_result(1), delegation_id=BATCH))
            except Exception as exc:
                err.append((type(exc).__name__, str(exc), round(time.monotonic() - t0, 1)))

        th = threading.Thread(target=pub)
        th.start()
        th.join(0.5)
        assert th.is_alive(), "publish should be waiting on the holder's write lock"
        # Another ledger reader in this process is stuck behind _DB_LOCK meanwhile.
        rd = []
        reader = threading.Thread(target=lambda: rd.append(ad.get_durable_delegation(BATCH)))
        reader.start()
        reader.join(0.5)
        reader_blocked = reader.is_alive()
        holder.commit()  # release after ~1s: publish proceeds
        th.join(15)
        reader.join(5)
        print({"outcome": outcome, "err": err, "reader_blocked_behind_db_lock": reader_blocked,
               "waited_s": round(time.monotonic() - t0, 1)})
        assert outcome == ["parked"] and not err
        assert reader_blocked and rd and rd[0]["state"] == "running"

        # Held past the 10 s busy timeout: bounded failure, not an unbounded wait.
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE async_delegations SET updated_at=updated_at WHERE 0")
        t1 = time.monotonic()
        err2 = []

        def pub2():
            try:
                ad.publish_batch_child_completion(_result(1, status="error", error="x"), delegation_id=BATCH)
                err2.append(("duplicate-or-ok", round(time.monotonic() - t1, 1)))
            except Exception as exc:
                err2.append((type(exc).__name__, round(time.monotonic() - t1, 1)))

        th2 = threading.Thread(target=pub2)
        th2.start()
        th2.join(20)
        holder.rollback()
        print({"err2": err2})
        assert not th2.is_alive()
        assert err2 and err2[0][0] == "OperationalError" and 9 <= err2[0][1] <= 13
    finally:
        try:
            holder.rollback()
        except Exception:
            pass
        holder.close()
        gate.set()
