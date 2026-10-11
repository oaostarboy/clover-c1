"""Joined fan-out delivery through the real gateway handler (C1.5 B1).

Real async registry, ``SessionDB``/``SessionStore`` in a scratch
``CLOVER_HOME`` and the real ``GatewayRunner`` delivery path. Only the
platform adapter and the child work itself are fake.
"""

from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

import pytest

from clover_state import AsyncSessionDB, SessionDB
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from gateway.session_context import clear_session_vars, set_session_vars
from tools import async_delegation as ad
from tools.process_registry import process_registry

BATCH = "deleg_c1c1c1c1"
GOALS = ["audit alpha", "audit beta", "audit gamma"]


@pytest.fixture(autouse=True)
def _clean_state():
    ad._reset_for_tests(drain_timeout=10)
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    ad._reset_for_tests(drain_timeout=10)
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _result(index, status="completed", summary=None, error=None):
    return {
        "task_index": index, "status": status,
        "summary": summary if summary is not None else (
            f"{GOALS[index]} findings" if status == "completed" else None),
        "error": error, "api_calls": 2, "duration_seconds": 1.5,
        "exit_reason": status,
    }


class _World:
    def __init__(self, tmp_path):
        self.db = SessionDB()
        self.config = GatewayConfig()
        self.store = SessionStore(sessions_dir=tmp_path / "sessions", config=self.config)
        self.store._db = self.db
        self.source = SessionSource(platform=Platform.TELEGRAM, chat_id="100", chat_type="dm", user_id="u1")
        self.entry = self.store.get_or_create_session(self.source)
        self.accepted = []
        self.routed = []
        world = self

        async def handle_message(event):
            world.accepted.append(event)
            world.routed.append(
                await world.runner._route_event_session(event, event.source)
            )

        self.adapter = SimpleNamespace(handle_message=handle_message)
        self.runner = self.new_runner()

    def new_runner(self):
        """A runner with empty in-memory dedupe sets: a restarted gateway."""
        runner = object.__new__(GatewayRunner)
        runner._running = True
        runner._session_db = AsyncSessionDB(self.db)
        runner.session_store = self.store
        runner._async_session_store = AsyncSessionStore(self.store)
        runner.config = self.config
        runner._session_source_cache = {}
        runner.adapters = {Platform.TELEGRAM: self.adapter}
        runner._completion_delivery_lock = threading.Lock()
        runner._completion_deliveries_inflight = set()
        runner._completion_deliveries_delivered = {}
        runner._completion_delivery_retention = 64
        return runner

    def dispatch(self, runner_fn, *, mode="joined", goals=GOALS, **extra):
        tokens = set_session_vars(
            platform="telegram", chat_id="100", chat_type="dm",
            session_key=self.entry.session_key, user_id="u1",
        )
        try:
            handle = ad.dispatch_async_delegation_batch(
                goals=goals, context="Audit the three repos and report.",
                toolsets=None, role="leaf", model="m",
                session_key=self.entry.session_key,
                parent_session_id=self.entry.session_id,
                runner=runner_fn, delegation_id=BATCH, fanout_delivery=mode,
                request_identity={"turn_id": "turn-1", "request_id": "req-1", "generation": 1},
                **extra,
            )
        finally:
            clear_session_vars(tokens)
        assert handle["status"] == "dispatched", handle
        return handle

    def events(self, count, timeout=10.0):
        got = []
        deadline = time.monotonic() + timeout
        while len(got) < count and time.monotonic() < deadline:
            try:
                got.append(process_registry.completion_queue.get(timeout=0.1))
            except queue.Empty:
                continue
        assert len(got) == count, f"expected {count} events, got {len(got)}"
        for evt in got:
            self.runner._enrich_async_delegation_routing(evt)
        return got

    def events_nowait(self):
        out = []
        while not process_registry.completion_queue.empty():
            out.append(process_registry.completion_queue.get_nowait())
        return out

    def kill_owner(self):
        """The process that owned the batch died; nothing of it is in memory."""
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute(
                "UPDATE async_delegations SET owner_pid=? WHERE delegation_id=?",
                (99999999, BATCH),
            )
        with ad._records_lock:
            ad._records.clear()

    def restore(self):
        replay = queue.Queue()
        ad.restore_undelivered_completions(replay)
        out = []
        while not replay.empty():
            evt = replay.get_nowait()
            self.runner._enrich_async_delegation_routing(evt)
            out.append(evt)
        return out

    def close(self):
        self.db.close()


@pytest.fixture
def world(tmp_path):
    w = _World(tmp_path)
    try:
        yield w
    finally:
        w.close()


def _state(delegation_id):
    return ad.get_durable_delegation(delegation_id)["delivery_state"]


def _park_two_then_hang(gate, parked):
    def runner():
        ad.publish_batch_child_completion(_result(0), delegation_id=BATCH)
        ad.publish_batch_child_completion(_result(2), delegation_id=BATCH)
        parked.set()
        gate.wait(30)
        return {"results": []}
    return runner


# ── the whole batch is one turn, in order, exactly once ──────────────────

@pytest.mark.asyncio
async def test_joined_batch_reenters_the_root_once_with_every_result_in_order(world):
    def runner():
        ad.publish_batch_child_completion(_result(2), delegation_id=BATCH)
        ad.publish_batch_child_completion(_result(0), delegation_id=BATCH)
        ad.publish_batch_child_completion(
            _result(1, status="error", error="beta worker crashed"), delegation_id=BATCH)
        return {"results": [
            _result(0), _result(1, status="error", error="beta worker crashed"), _result(2),
        ]}

    world.dispatch(runner)
    (join,) = world.events(1)
    assert process_registry.completion_queue.empty()

    assert await world.runner._deliver_async_delegation_group([join]) is True

    assert len(world.accepted) == 1
    text = world.accepted[0].text
    positions = [text.index(f"TASK {i}/3") for i in (1, 2, 3)]
    assert positions == sorted(positions)
    assert "audit alpha findings" in text and "audit gamma findings" in text
    assert "beta worker crashed" in text
    assert world.accepted[0].source.chat_id == "100"
    assert world.routed[0] is not None
    assert world.routed[0].session_id == world.entry.session_id

    assert _state(BATCH) == "delivered"
    assert all(_state(f"{BATCH}:child:{i}") == "delivered" for i in range(3))

    # The trusted request tuple rides the delivered event, not only the
    # session/inbox identity.
    meta = world.accepted[0].metadata
    assert meta["request_turn_id"] == "turn-1"
    assert meta["request_id"] == "req-1"
    assert meta["request_generation"] == 1

    # The same completion replayed (queue replay, second gateway) never lands twice.
    assert await world.runner._deliver_async_delegation_group([join]) is None
    other = world.new_runner()
    assert await other._deliver_async_delegation_group([join]) is None
    assert len(world.accepted) == 1


@pytest.mark.asyncio
async def test_each_mode_delivers_per_child_exactly_as_before(world):
    def runner():
        for i in range(3):
            ad.publish_batch_child_completion(_result(i), delegation_id=BATCH)
        return {"results": [_result(i) for i in range(3)]}

    world.dispatch(runner, mode="each")
    events = world.events(3)
    assert [e["delegation_id"] for e in events] == [f"{BATCH}:child:{i}" for i in range(3)]

    for evt in events:
        assert await world.runner._deliver_async_delegation_group([evt]) is True

    assert len(world.accepted) == 3
    for i, event in enumerate(world.accepted):
        assert f"[ASYNC DELEGATION COMPLETE — {BATCH}:child:{i}]" in event.text
        assert f"{GOALS[i]} findings" in event.text
    assert all(_state(e["delegation_id"]) == "delivered" for e in events)


# ── the origin session is the destination, never "the latest chat" ───────

@pytest.mark.asyncio
async def test_unrelated_chat_interleaved_during_the_batch_does_not_capture_the_join(world):
    gate = threading.Event()

    def runner():
        gate.wait(30)
        for i in range(3):
            ad.publish_batch_child_completion(_result(i), delegation_id=BATCH)
        return {"results": [_result(i) for i in range(3)]}

    world.dispatch(runner)

    other = SessionSource(platform=Platform.TELEGRAM, chat_id="200", chat_type="dm", user_id="u2")
    other_entry = world.store.get_or_create_session(other)
    await world.adapter.handle_message(
        MessageEvent(text="what is the weather like?", source=other, message_id="h1")
    )
    gate.set()
    (join,) = world.events(1)
    assert await world.runner._deliver_async_delegation_group([join]) is True

    human, joined = world.accepted
    assert human.source.chat_id == "200" and human.text == "what is the weather like?"
    assert joined.source.chat_id == "100"
    assert joined.metadata["gateway_session_id"] == world.entry.session_id
    assert world.routed[1].session_id == world.entry.session_id
    assert world.routed[1].session_id != other_entry.session_id
    assert "audit alpha findings" in joined.text
    assert "weather" not in joined.text


@pytest.mark.asyncio
async def test_same_chat_message_during_the_batch_keeps_the_join_bound_to_its_request(world):
    gate = threading.Event()

    def runner():
        gate.wait(30)
        for i in range(3):
            ad.publish_batch_child_completion(_result(i), delegation_id=BATCH)
        return {"results": [_result(i) for i in range(3)]}

    world.dispatch(runner)
    await world.adapter.handle_message(
        MessageEvent(text="also, what time is it?", source=world.source, message_id="h1")
    )
    gate.set()
    (join,) = world.events(1)
    assert await world.runner._deliver_async_delegation_group([join]) is True

    human, joined = world.accepted
    assert human.text == "also, what time is it?"
    assert joined.internal is True
    assert BATCH in joined.text
    assert "Audit the three repos and report." in joined.text
    assert joined.metadata["completion_inbox_key"] == f"deleg:{BATCH}"


@pytest.mark.asyncio
async def test_session_reset_before_the_join_records_it_instead_of_injecting(world):
    def runner():
        for i in range(3):
            ad.publish_batch_child_completion(_result(i), delegation_id=BATCH)
        return {"results": [_result(i) for i in range(3)]}

    world.dispatch(runner)
    (join,) = world.events(1)
    world.store.reset_session(world.entry.session_key)  # the human sent /new

    assert await world.runner._deliver_async_delegation_group([join]) is None

    assert world.accepted == []
    assert _state(BATCH) == "dropped"
    assert all(_state(f"{BATCH}:child:{i}") == "inboxed" for i in range(3))
    record = world.db.inbox_get(f"deleg:{BATCH}")
    assert record is not None and record["state"] == "dropped"
    replay = world.restore()
    assert replay == []


@pytest.mark.asyncio
async def test_cancelled_batch_after_reset_owes_no_notice_and_is_listed_once(world):
    def runner():
        for i in range(3):
            ad.publish_batch_child_completion(
                _result(i, status="interrupted", error="Operation interrupted"),
                delegation_id=BATCH)
        return {"results": [
            _result(i, status="interrupted", error="Operation interrupted") for i in range(3)
        ]}

    world.dispatch(runner)
    (join,) = world.events(1)
    world.store.reset_session(world.entry.session_key)
    assert await world.runner._deliver_async_delegation_group([join]) is None

    record = world.db.inbox_get(f"deleg:{BATCH}")
    assert record["state"] == "dropped"
    assert record["notice_state"] is None, "a helper the user cancelled owes no notice"
    assert world.accepted == []


# ── gateway restart mid-batch ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gateway_restart_mid_batch_delivers_one_join_with_the_surviving_results(world):
    gate, parked = threading.Event(), threading.Event()
    try:
        world.dispatch(_park_two_then_hang(gate, parked))
        assert parked.wait(10)
        assert process_registry.completion_queue.empty()
        world.kill_owner()

        restarted = world.new_runner()
        (join,) = world.restore()
        assert join["delegation_id"] == BATCH
        assert await restarted._deliver_async_delegation_group([join]) is True

        assert len(world.accepted) == 1
        text = world.accepted[0].text
        assert "audit alpha findings" in text and "audit gamma findings" in text
        assert "audit beta" in text
        assert "INCOMPLETE" in text
        assert _state(BATCH) == "delivered"

        # Another restart finds nothing left to replay.
        assert world.restore() == []
        again = world.new_runner()
        assert await again._deliver_async_delegation_group([join]) is None
        assert len(world.accepted) == 1
    finally:
        gate.set()


@pytest.mark.asyncio
async def test_late_child_after_an_incomplete_join_is_one_result_bound_to_the_batch(world):
    gate, parked = threading.Event(), threading.Event()
    try:
        world.dispatch(_park_two_then_hang(gate, parked))
        assert parked.wait(10)
        world.kill_owner()
        (join,) = world.restore()
        assert await world.runner._deliver_async_delegation_group([join]) is True

        ad.publish_batch_child_completion(
            _result(1, summary="beta findings, finally"), delegation_id=BATCH)
        ad.publish_batch_child_completion(
            _result(1, summary="beta findings, finally"), delegation_id=BATCH)
        (late,) = world.events(1)
        assert process_registry.completion_queue.empty()
        assert late["late_result"] is True and late["batch_delegation_id"] == BATCH

        assert await world.runner._deliver_async_delegation_group([late]) is True
        assert len(world.accepted) == 2
        text = world.accepted[1].text
        assert "beta findings, finally" in text
        assert BATCH in text
        assert "late" in text.lower()
        assert "TASK 1/3" not in text, "a late result is not a second join"
        assert world.accepted[1].source.chat_id == "100"
        assert world.routed[1].session_id == world.entry.session_id
        late_meta = world.accepted[1].metadata
        assert late_meta["request_turn_id"] == "turn-1"
        assert late_meta["request_generation"] == 1
        assert late_meta["batch_delegation_id"] == BATCH
        assert late_meta["late_result"] is True

        assert await world.runner._deliver_async_delegation_group([late]) is None
        assert len(world.accepted) == 2
    finally:
        gate.set()
