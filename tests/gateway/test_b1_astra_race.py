"""Cross-process publish vs join (review probe, C1.5 B1 round 1).

A real forked publisher, real SQLite, ``SessionDB`` and the gateway. A SQLite
trace callback only controls scheduling; nothing in the ledger is faked.

Before the fix a child could read the batch as "running", be descheduled, and
insert its result as a parked receipt AFTER another process committed an
incomplete join. The gateway then acknowledged that receipt as delivered
although its result was never in the join. Now the read and the write share
one ``BEGIN IMMEDIATE`` transaction, so the interleaving is gone: the loser is
either inside the join or a late result delivered once.
"""

import os
import select
import threading

import pytest

from tests.gateway.test_joined_fanout_gateway import (  # noqa: F401  (fixtures)
    BATCH, _clean_state, _result, world,
)
from tools import async_delegation as ad


def _fork_publisher(pause_on):
    """Fork a publisher that pauses when SQLite starts a statement matching
    ``pause_on``. Returns ``(pid, ready_r, ready_w, resume_r, resume_w)``."""
    ready_r, ready_w = os.pipe()
    resume_r, resume_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            real_connect = ad._connect
            paused = []

            def connect():
                conn = real_connect()

                def trace(sql):
                    if not paused and pause_on(sql):
                        paused.append(True)
                        os.write(ready_w, b"R")
                        os.read(resume_r, 1)

                conn.set_trace_callback(trace)
                return conn

            ad._connect = connect
            outcome = ad.publish_batch_child_completion(
                _result(1, summary="UNIQUE_BETA_RESULT"), delegation_id=BATCH)
            os.write(ready_w, outcome.encode())
            os._exit(0)
        except BaseException:
            os._exit(2)
    return pid, ready_r, ready_w, resume_r, resume_w


def _park_two(world):
    gate, parked = threading.Event(), threading.Event()

    def runner():
        ad.publish_batch_child_completion(_result(0), delegation_id=BATCH)
        ad.publish_batch_child_completion(_result(2), delegation_id=BATCH)
        parked.set()
        gate.wait(20)
        return {"results": []}

    world.dispatch(runner)
    assert parked.wait(10)
    return gate


def _unique_deliveries(world):
    return [e for e in world.accepted if "UNIQUE_BETA_RESULT" in e.text]


@pytest.mark.asyncio
@pytest.mark.parametrize("finalizer", ["stall", "restore"])
async def test_publish_losing_the_race_to_a_committed_join_is_one_late_result(
    world, finalizer
):
    gate = _park_two(world)
    pid, ready_r, ready_w, resume_r, resume_w = _fork_publisher(
        lambda sql: sql.lstrip().upper().startswith("BEGIN IMMEDIATE"))
    try:
        # The publisher is descheduled before its write transaction began.
        assert select.select([ready_r], [], [], 10)[0], "publisher never started"
        assert os.read(ready_r, 1) == b"R"
        if finalizer == "stall":
            ad._finalize_stalled(BATCH)
            (join,) = world.events(1)
        else:
            world.kill_owner()
            (join,) = world.restore()
        assert join["incomplete_indexes"] == [1]

        os.write(resume_w, b"G")
        assert select.select([ready_r], [], [], 10)[0]
        outcome = os.read(ready_r, 100).decode()
        _, status = os.waitpid(pid, 0)
        assert status == 0
        assert outcome == "late"

        # Recorded as a late result, NOT parked behind a join that never had it.
        assert ad.get_durable_delegation(f"{BATCH}:child:1")["delivery_state"] == "pending"

        assert await world.runner._deliver_async_delegation_group([join]) is True
        # Acknowledging the join must not touch the receipt it did not carry.
        assert ad.get_durable_delegation(f"{BATCH}:child:1")["delivery_state"] == "pending"
        assert _unique_deliveries(world) == []

        # A restart restores exactly the undelivered late result...
        replay = world.restore()
        assert [e["delegation_id"] for e in replay] == [f"{BATCH}:child:1"]
        assert replay[0]["late_result"] is True
        for evt in replay:
            assert await world.runner._deliver_async_delegation_group([evt]) is True
        # ...which is then delivered exactly once.
        assert len(_unique_deliveries(world)) == 1
        assert ad.get_durable_delegation(f"{BATCH}:child:1")["delivery_state"] == "delivered"
        assert world.restore() == []
    finally:
        gate.set()
        for fd in (ready_r, ready_w, resume_r, resume_w):
            os.close(fd)


@pytest.mark.asyncio
async def test_finalizer_cannot_commit_a_join_between_a_publishers_read_and_write(world):
    gate = _park_two(world)
    pid, ready_r, ready_w, resume_r, resume_w = _fork_publisher(
        lambda sql: sql.lstrip().startswith("INSERT OR IGNORE INTO async_delegations"))
    finalizer = None
    try:
        # The publisher is paused INSIDE its transaction: it already read the
        # batch as running and holds the database write lock.
        assert select.select([ready_r], [], [], 10)[0], "publisher never reached insert"
        assert os.read(ready_r, 1) == b"R"
        finalizer = threading.Thread(target=ad._finalize_stalled, args=(BATCH,))
        finalizer.start()
        finalizer.join(1.0)
        assert finalizer.is_alive(), "join committed under the publisher's read"
        assert world.events_nowait() == []

        os.write(resume_w, b"G")
        finalizer.join(15)
        assert not finalizer.is_alive()
        assert select.select([ready_r], [], [], 10)[0]
        assert os.read(ready_r, 100).decode() == "parked"
        _, status = os.waitpid(pid, 0)
        assert status == 0

        (join,) = world.events(1)
        # The publisher committed first, so the join carries its result.
        assert join["incomplete_indexes"] == []
        assert f"{BATCH}:child:1" in join["included_receipts"]
        assert await world.runner._deliver_async_delegation_group([join]) is True
        assert len(_unique_deliveries(world)) == 1
        assert ad.get_durable_delegation(f"{BATCH}:child:1")["delivery_state"] == "delivered"
        assert world.restore() == []
    finally:
        gate.set()
        if finalizer is not None:
            finalizer.join(5)
        for fd in (ready_r, ready_w, resume_r, resume_w):
            os.close(fd)
