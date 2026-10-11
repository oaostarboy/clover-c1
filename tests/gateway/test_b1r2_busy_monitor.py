"""Shared-database contention at the straggler join boundary (review probe,
C1.5 B1 round 2).

SessionDB and the delegation ledger share ``state.db``. A real SessionDB write
transaction held while the straggler window expires made the join's
``BEGIN IMMEDIATE`` fail; the failure used to arrive after the batch was
already flagged as joined and kill the only monitor thread: no delivery, no
stall detection, nothing to retry it. Now nothing is flagged until the join is
durable and one failed sweep never ends the monitor, so the join fires on the
next tick once the database is free, with no new dispatch and no restart.

The original probe asserted the crash; this version asserts the recovery.
"""
import pathlib
import threading

import pytest

from tests.gateway.test_joined_fanout_gateway import BATCH, _World, _result
from tests.tools.test_async_joined_fanout_fix1 import clock, reg  # noqa: F401  (fixtures)
from tools import async_delegation as ad


@pytest.mark.asyncio
async def test_shared_sessiondb_contention_does_not_abandon_the_straggler(
    reg, clock, tmp_path, monkeypatch
):
    world = _World(tmp_path)
    # Put the delegation ledger in the very file SessionDB writes to.
    db_file = world.db._conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setenv("CLOVER_HOME", str(pathlib.Path(db_file).parent))
    gate, parked = threading.Event(), threading.Event()
    crashed, failed = threading.Event(), threading.Event()
    monkeypatch.setattr(threading, "excepthook", lambda args: crashed.set())
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.02)
    # Shorter than the contention below, so it is genuinely exceeded.
    monkeypatch.setattr(ad, "_BUSY_TIMEOUT_SECONDS", 0.3)
    real_push = ad._push_joined_batch_completion

    def push(*a, **k):
        try:
            return real_push(*a, **k)
        except Exception:
            failed.set()
            raise

    monkeypatch.setattr(ad, "_push_joined_batch_completion", push)

    def runner():
        for i in (0, 2):
            ad.publish_batch_child_completion(_result(i), delegation_id=BATCH)
        parked.set()
        gate.wait(20)
        return {"results": []}

    held = False
    try:
        world.dispatch(runner, straggler_seconds=300, progress_fn=lambda: (1, False))
        assert parked.wait(10)
        # SessionDB and the delegation ledger really share state.db.
        assert world.db._conn.execute("PRAGMA database_list").fetchone()[2] == str(
            ad._db_path())
        monitor = ad._monitor_thread
        world.db._conn.execute("BEGIN IMMEDIATE")   # a normal SessionDB write
        held = True
        clock.t += 301
        assert failed.wait(10), "the join was never attempted under contention"
        assert monitor.is_alive() and not crashed.is_set()
        with ad._records_lock:
            record = dict(ad._records[BATCH])
        assert record["status"] == "running"
        assert record["_straggler_joined"] is False, "flagged before commit"
        assert reg.empty()

        world.db._conn.rollback()
        held = False
        join = reg.get(timeout=10)                  # next tick, nothing else
        assert join["incomplete_indexes"] == [1]
        assert monitor.is_alive() and not crashed.is_set()

        # And it is deliverable: the gateway takes the recovered join.
        world.runner._enrich_async_delegation_routing(join)
        assert await world.runner._deliver_async_delegation_group([join]) is True
        assert ad.get_durable_delegation(BATCH)["delivery_state"] == "delivered"
        assert world.restore() == []
    finally:
        if held:
            world.db._conn.rollback()
        gate.set()
        ad._reset_for_tests(drain_timeout=10)
        world.close()
