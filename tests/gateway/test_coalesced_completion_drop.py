"""A consolidated completion turn settles every sibling with its primary.

Two REAL durable delegations from one conversation are delivered as one turn
(``_deliver_async_delegation_group``). Accepted: both rows are acked together.
Dropped by the pipeline after acceptance (a human ``/new`` races the turn):
the siblings were already acked with the primary, so every key must be
recorded in the inbox, not only the primary's.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from clover_state import AsyncSessionDB, SessionDB
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from gateway.session_context import clear_session_vars, set_session_vars
from tools import async_delegation as ad
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def _clean_state():
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


class _World:
    def __init__(self, tmp_path, *, race_new):
        self.db = SessionDB()
        config = GatewayConfig()
        self.store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
        self.store._db = self.db
        self.source = SessionSource(platform=Platform.TELEGRAM, chat_id="100", chat_type="dm", user_id="u1")
        self.entry = self.store.get_or_create_session(self.source)
        self.accepted = []
        world = self

        async def handle_message(event):
            world.accepted.append(event)
            if race_new:
                world.store.reset_session(world.entry.session_key)  # human /new after the pre-flight
            world.routed = await world.runner._route_event_session(event, event.source)

        runner = object.__new__(GatewayRunner)
        runner._running = True
        runner._session_db = AsyncSessionDB(self.db)
        runner.session_store = self.store
        runner._async_session_store = AsyncSessionStore(self.store)
        runner.config = config
        runner._session_source_cache = {}
        runner.adapters = {Platform.TELEGRAM: SimpleNamespace(handle_message=handle_message)}
        runner._completion_delivery_lock = __import__("threading").Lock()
        runner._completion_deliveries_inflight = set()
        runner._completion_deliveries_delivered = {}
        runner._completion_delivery_retention = 64
        self.runner = runner
        self.routed = "unset"

    def two_completions(self):
        events = []
        for goal in ("First audit", "Second audit"):
            tokens = set_session_vars(
                platform="telegram", chat_id="100", chat_type="dm",
                session_key=self.entry.session_key, user_id="u1",
            )
            try:
                handle = ad.dispatch_async_delegation(
                    goal=goal, context=None, toolsets=None, role="leaf", model="m",
                    session_key=self.entry.session_key,
                    parent_session_id=self.entry.session_id,
                    runner=lambda g=goal: {"status": "completed", "summary": f"{g} done"},
                )
            finally:
                clear_session_vars(tokens)
            events.append(handle["delegation_id"])
        got = {}
        deadline = time.monotonic() + 5
        while len(got) < 2 and time.monotonic() < deadline:
            if process_registry.completion_queue.empty():
                time.sleep(0.02)
                continue
            evt = process_registry.completion_queue.get_nowait()
            if evt.get("delegation_id") in events:
                got[evt["delegation_id"]] = evt
        assert len(got) == 2
        evts = [got[d] for d in events]
        for evt in evts:
            self.runner._enrich_async_delegation_routing(evt)
        return evts


def _state(did):
    return ad.get_durable_delegation(did)["delivery_state"]


@pytest.mark.asyncio
async def test_accepted_consolidated_turn_acks_every_sibling_with_the_primary(tmp_path):
    w = _World(tmp_path, race_new=False)
    try:
        e1, e2 = w.two_completions()

        assert await w.runner._deliver_async_delegation_group([e1, e2]) is True

        assert len(w.accepted) == 1, "siblings must ride the primary's single turn"
        assert [_state(e["delegation_id"]) for e in (e1, e2)] == ["delivered", "delivered"]
        assert w.routed is not None
        assert w.db.inbox_for_route("default", "telegram", "100", None) == []
    finally:
        w.db.close()


@pytest.mark.asyncio
async def test_consolidated_turn_dropped_after_acceptance_records_every_sibling(tmp_path):
    w = _World(tmp_path, race_new=True)
    try:
        e1, e2 = w.two_completions()

        assert await w.runner._deliver_async_delegation_group([e1, e2]) is True

        assert w.routed is None  # the pipeline refused the turn after acceptance
        keys = {f"deleg:{e['delegation_id']}" for e in (e1, e2)}
        recs = {r["key"]: r for r in w.db.inbox_for_route("default", "telegram", "100", None)}
        assert set(recs) == keys, "a sibling's result was acked and left no record"
        titles = {r["title"] for r in recs.values()}
        assert titles == {"First audit", "Second audit"}
        for rec in recs.values():
            assert rec["state"] == "dropped"
            assert rec["drop_reason"].startswith("unowned:")
            assert "First audit done" in rec["payload_json"] and "Second audit done" in rec["payload_json"]
        # One dropped turn is announced once: the primary owes the notice, the
        # sibling stays listable in /results without a second message.
        assert sorted(r["notice_state"] or "-" for r in recs.values()) == ["-", "pending"]
        assert recs[f"deleg:{e1['delegation_id']}"]["notice_state"] == "pending"

        from gateway.inbox_notices import sweep_inbox_notices

        adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
        w.runner.adapters = {Platform.TELEGRAM: adapter}
        out = await sweep_inbox_notices(w.runner._session_db, w.runner._send_inbox_notice)
        assert out["sent"] == 1 and adapter.send.await_count == 1
    finally:
        w.db.close()
