"""A dropped delegation is recorded against the route it was dispatched from.

The route comes from the origin persisted on the durable row at dispatch (the
dispatching turn's session vars), never from the positional layout of the
session key: named-profile keys, scoped Slack keys and threaded group keys do
not follow ``agent:main:<platform>:<type>:<chat>[:<thread>]``. Real dispatch,
real attempt-cap drop, real ``SessionStore``/``SessionDB`` and ``/results``.
"""

from __future__ import annotations

import logging
import time

import pytest

from clover_state import AsyncSessionDB, SessionDB
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
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
    def __init__(self, tmp_path, *, multiplex=False):
        self.db = SessionDB()  # the same CLOVER_HOME state.db the delegation ledger uses
        config = GatewayConfig(multiplex_profiles=multiplex)
        self.store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
        self.store._db = self.db
        runner = object.__new__(GatewayRunner)
        runner._session_db = AsyncSessionDB(self.db)
        runner.session_store = self.store
        runner._async_session_store = AsyncSessionStore(self.store)
        runner.config = config
        runner._session_source_cache = {}
        self.runner = runner

    def drop_a_delegation(self, source, *, with_origin=True, goal="Audit the repo", pin=None):
        """Dispatch from ``source``'s turn, then exhaust its delivery attempts."""
        entry = self.store.get_or_create_session(source)
        tokens = None
        if with_origin:
            tokens = set_session_vars(
                platform=source.platform.value, chat_id=str(source.chat_id),
                chat_type=source.chat_type, thread_id=str(source.thread_id or ""),
                user_id=str(source.user_id or ""), scope_id=str(source.scope_id or ""),
                session_key=entry.session_key, profile=source.profile or "",
            )
        try:
            handle = ad.dispatch_async_delegation(
                goal=goal, context=None, toolsets=None, role="leaf", model="m",
                session_key=entry.session_key, parent_session_id=pin or entry.session_id,
                runner=lambda: {"status": "completed", "summary": "found 3 issues"},
            )
        finally:
            if tokens is not None:
                clear_session_vars(tokens)
        did = handle["delegation_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process_registry.completion_queue.empty():
                time.sleep(0.02)
                continue
            if process_registry.completion_queue.get_nowait().get("delegation_id") == did:
                break
        for _ in range(ad._MAX_DELIVERY_ATTEMPTS):
            claim = f"c-{time.monotonic_ns()}"
            assert ad.claim_completion_delivery(did, claim)
            ad.release_completion_delivery(did, claim)
        assert ad.get_durable_delegation(did)["delivery_state"] == "dropped"
        return entry, did

    async def results(self, source, args=""):
        event = MessageEvent(text="/results " + args, source=source, message_id="m1")
        return await self.runner._handle_results_command(event)


@pytest.fixture
def world(tmp_path):
    w = _World(tmp_path)
    try:
        yield w
    finally:
        w.db.close()


@pytest.mark.asyncio
async def test_named_profile_route_is_recorded_and_found(tmp_path):
    w = _World(tmp_path, multiplex=True)
    try:
        src = SessionSource(platform=Platform.TELEGRAM, chat_id="100", chat_type="dm",
                            user_id="u1", profile="sec")
        entry, did = w.drop_a_delegation(src)
        assert entry.session_key.startswith("agent:sec:telegram:dm:100")

        rec = w.db.inbox_get(f"deleg:{did}")
        assert (rec["profile"], rec["platform"], rec["chat_id"], rec["thread_id"]) == (
            "sec", "telegram", "100", None,
        )
        assert f"deleg:{did}" in await w.results(src)
        assert rec["notice_state"] == "pending"
    finally:
        w.db.close()


@pytest.mark.asyncio
async def test_scoped_slack_route_is_recorded_and_found(world):
    src = SessionSource(platform=Platform.SLACK, chat_id="C1", chat_type="channel",
                        user_id="U1", scope_id="T1")
    entry, did = world.drop_a_delegation(src)

    rec = world.db.inbox_get(f"deleg:{did}")
    assert (rec["platform"], rec["chat_id"]) == ("slack", "C1"), entry.session_key
    assert f"deleg:{did}" in await world.results(src)


@pytest.mark.asyncio
async def test_threaded_telegram_group_route_is_recorded_and_found(world):
    src = SessionSource(platform=Platform.TELEGRAM, chat_id="-100777", chat_type="group",
                        user_id="u1", thread_id="555")
    entry, did = world.drop_a_delegation(src)

    rec = world.db.inbox_get(f"deleg:{did}")
    assert (rec["platform"], rec["chat_id"], rec["thread_id"]) == ("telegram", "-100777", "555")
    assert f"deleg:{did}" in await world.results(src)
    other_topic = SessionSource(platform=Platform.TELEGRAM, chat_id="-100777", chat_type="group",
                                user_id="u1", thread_id="556")
    assert f"deleg:{did}" not in await world.results(other_topic)


@pytest.mark.asyncio
async def test_owner_session_origin_supplies_the_route_when_the_row_has_none(world):
    src = SessionSource(platform=Platform.TELEGRAM, chat_id="100", chat_type="dm",
                        user_id="u1", thread_id="555")
    entry, did = world.drop_a_delegation(src, with_origin=False)

    rec = world.db.inbox_get(f"deleg:{did}")
    assert (rec["profile"], rec["platform"], rec["chat_id"], rec["thread_id"]) == (
        "default", "telegram", "100", "555",
    )
    assert rec["owner_root_id"] == entry.session_id and rec["notice_state"] == "pending"
    assert f"deleg:{did}" in await world.results(src)


@pytest.mark.asyncio
async def test_without_any_origin_it_is_still_recorded_and_found_by_owner(world, caplog):
    src = SessionSource(platform=Platform.TELEGRAM, chat_id="100", chat_type="dm", user_id="u1")
    entry = world.store.get_or_create_session(src)
    world.db._execute_write(lambda c: c.execute(
        "UPDATE sessions SET source = 'cli', chat_id = NULL, thread_id = NULL, "
        "origin_json = NULL WHERE id = ?", (entry.session_id,)))
    with caplog.at_level(logging.WARNING, logger="tools.async_delegation"):
        _entry, did = world.drop_a_delegation(src, with_origin=False)

    rec = world.db.inbox_get(f"deleg:{did}")
    assert (rec["platform"], rec["chat_id"], rec["thread_id"]) == (None, None, None)
    assert rec["owner_root_id"] == entry.session_id and rec["state"] == "dropped"
    assert any("no durable route origin" in r.getMessage() for r in caplog.records)
    assert f"deleg:{did}" in await world.results(src)
    elsewhere = SessionSource(platform=Platform.TELEGRAM, chat_id="200", chat_type="dm", user_id="u2")
    world.store.get_or_create_session(elsewhere)
    assert f"deleg:{did}" not in await world.results(elsewhere)


# ---------------------------------------------------------------------------
# Delegated-child pins: the owner is stored resolved, legacy raw pins still resolve
# ---------------------------------------------------------------------------
def _child_of(world, entry, child_id="child-sess"):
    world.db.create_session(
        child_id, source="telegram", parent_session_id=entry.session_id,
        model_config={"_delegate_from": entry.session_id},
    )
    return child_id


@pytest.mark.asyncio
async def test_a_child_pin_is_stored_as_the_resolved_owner_root(world):
    src = SessionSource(platform=Platform.TELEGRAM, chat_id="100", chat_type="dm", user_id="u1")
    entry = world.store.get_or_create_session(src)
    child = _child_of(world, entry)

    _entry, did = world.drop_a_delegation(src, with_origin=False, pin=child)

    rec = world.db.inbox_get(f"deleg:{did}")
    assert rec["owner_root_id"] == entry.session_id, "stored the child's raw id"
    assert f"deleg:{did}" in await world.results(src)
    elsewhere = SessionSource(platform=Platform.TELEGRAM, chat_id="200", chat_type="dm", user_id="u2")
    world.store.get_or_create_session(elsewhere)
    assert f"deleg:{did}" not in await world.results(elsewhere)


def _legacy_routeless_record(world, key, pin):
    """What the previous release wrote: the raw child pin as owner, no route."""
    world.db.inbox_put({
        "key": key, "profile": None, "platform": None, "chat_id": None, "thread_id": None,
        "session_key": None, "owner_root_id": pin, "kind": "delegation", "wake": 0,
        "title": "Legacy audit", "payload_json": '{"goal": "Legacy audit", "summary": "legacy result"}',
        "shown_to_user": 0,
    })
    world.db.inbox_drop(key, "delivery_attempts_exhausted")


@pytest.mark.asyncio
async def test_a_legacy_routeless_child_pin_record_is_found_by_its_owners_conversation(world):
    src = SessionSource(platform=Platform.TELEGRAM, chat_id="100", chat_type="dm", user_id="u1")
    entry = world.store.get_or_create_session(src)
    _legacy_routeless_record(world, "deleg:legacy", _child_of(world, entry))

    assert "deleg:legacy" in await world.results(src)
    assert "legacy result" in await world.results(src, "deleg:legacy")

    elsewhere = SessionSource(platform=Platform.TELEGRAM, chat_id="200", chat_type="dm", user_id="u2")
    world.store.get_or_create_session(elsewhere)
    assert "deleg:legacy" not in await world.results(elsewhere)
    assert "legacy result" not in await world.results(elsewhere, "deleg:legacy")

    world.store.reset_session(entry.session_key)  # /new closes the owner
    assert "deleg:legacy" not in await world.results(src)


# ---------------------------------------------------------------------------
# The notice for a route-less record
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_routeless_record_notice_goes_out_when_the_owner_session_has_an_origin(world):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from gateway.inbox_notices import sweep_inbox_notices

    src = SessionSource(platform=Platform.TELEGRAM, chat_id="100", chat_type="dm",
                        user_id="u1", thread_id="555")
    entry = world.store.get_or_create_session(src)
    _legacy_routeless_record(world, "deleg:legacy", entry.session_id)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    world.runner.adapters = {Platform.TELEGRAM: adapter}
    world.runner._running = True

    out = await sweep_inbox_notices(world.runner._session_db, world.runner._send_inbox_notice)

    assert out["sent"] == 1, out
    assert adapter.send.await_args.args[0] == "100"
    assert str((adapter.send.await_args.kwargs.get("metadata") or {}).get("thread_id")) == "555"


@pytest.mark.asyncio
async def test_routeless_record_with_no_origin_anywhere_sends_no_notice(world):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from gateway.inbox_notices import sweep_inbox_notices

    _legacy_routeless_record(world, "deleg:legacy", "session-with-no-origin")
    adapter = SimpleNamespace(send=AsyncMock())
    world.runner.adapters = {Platform.TELEGRAM: adapter}
    world.runner._running = True

    out = await sweep_inbox_notices(world.runner._session_db, world.runner._send_inbox_notice)

    assert out["deferred"] == 1 and out["sent"] == 0
    adapter.send.assert_not_awaited()
