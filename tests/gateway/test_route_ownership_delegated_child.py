"""Typed route ownership for async completions (I1).

A completion pinned to a delegated child's session id must be delivered to the
conversation that OWNS the route, and an internal event must never move a
route. Real ``SessionStore`` + ``SessionDB`` in a temp dir; the only stubs are
the adapter-facing injection and the durable-claim plumbing.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from clover_state import AsyncSessionDB, SessionDB
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionSource, SessionStore


class _Env:
    """One telegram DM route with a real store/DB and a runner stub."""

    def __init__(self, tmp_path, *, thread_id=None):
        self.db = SessionDB(db_path=tmp_path / "state.db")
        self.store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
        self.store._db = self.db
        self.source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="100",
            chat_type="dm",
            user_id="u1",
            thread_id=thread_id,
        )
        self.entry = self.store.get_or_create_session(self.source)
        self.key = self.entry.session_key
        self.parent = self.entry.session_id
        self.switches = []
        real_switch = self.store.switch_session

        def _spy(session_key, target):
            self.switches.append((session_key, target))
            return real_switch(session_key, target)

        self.store.switch_session = _spy

        runner = object.__new__(GatewayRunner)
        runner._session_db = AsyncSessionDB(self.db)
        runner.session_store = self.store
        runner._async_session_store = AsyncSessionStore(self.store)
        runner.config = GatewayConfig()
        runner._session_source_cache = {}
        self.runner = runner

    def current(self):
        return self.store.lookup_by_session_key(self.key)

    def route(self, current_session_id=None):
        cur = current_session_id or self.current().session_id
        return ("default", self.key, cur, "telegram", "100", self.source.thread_id)

    async def classify(self, pin):
        return await self.runner._classify_completion_target(pin, self.route())

    def delegate(self, child_id, parent_id, *, ended=None):
        self.db.create_session(
            child_id,
            source="telegram",
            parent_session_id=parent_id,
            model_config={"_delegate_from": parent_id},
        )
        if ended:
            self.db.end_session(child_id, ended)

    def compress(self, session_id, child_id, *, inherit=None):
        """End ``session_id`` by compression and publish its continuation."""
        self.db.end_session(session_id, "compression")
        self.db.create_session(
            child_id,
            source="telegram",
            parent_session_id=session_id,
            model_config=inherit,
        )

    def ended(self, session_id):
        row = self.db.get_session(session_id)
        return row.get("ended_at"), row.get("end_reason")


@pytest.fixture
def env(tmp_path):
    e = _Env(tmp_path)
    try:
        yield e
    finally:
        e.db.close()


# ---------------------------------------------------------------------------
# RED on the P0 head: a child-stamped pin must not move the route
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_child_stamped_pin_keeps_route_on_parent(env):
    env.delegate("child_1", env.parent)

    resolved = await env.runner._resolve_async_delegation_session(env.entry, "child_1")

    assert resolved is not None and resolved.session_id == env.parent
    assert env.current().session_id == env.parent, "route was moved to the child"
    assert env.ended(env.parent)[0] is None, "parent was ended by an internal event"
    assert env.switches == []


@pytest.mark.asyncio
async def test_later_parent_pinned_completion_is_delivered(env):
    env.delegate("child_1", env.parent)
    await env.runner._resolve_async_delegation_session(env.entry, "child_1")

    assert await env.classify(env.parent) == "deliver"
    assert await env.classify("child_1") == "deliver"
    resolved = await env.runner._resolve_async_delegation_session(env.current(), env.parent)
    assert resolved is not None and resolved.session_id == env.parent


@pytest.mark.asyncio
async def test_orchestrator_child_pin_delivers_to_root_route(env):
    env.delegate("orch", env.parent)
    env.delegate("worker", "orch")

    resolved = await env.runner._resolve_async_delegation_session(env.entry, "worker")

    assert resolved is not None and resolved.session_id == env.parent
    assert env.current().session_id == env.parent
    assert await env.classify("worker") == "deliver"
    assert env.switches == []


@pytest.mark.asyncio
async def test_compressed_delegate_child_resolves_to_parent(env):
    # P -> C (delegate) -> C2 (compression continuation inheriting _delegate_from)
    env.delegate("c1", env.parent)
    env.compress("c1", "c2", inherit={"_delegate_from": env.parent})

    for pin in ("c1", "c2"):
        assert await env.classify(pin) == "deliver", pin
        resolved = await env.runner._resolve_async_delegation_session(env.entry, pin)
        assert resolved is not None and resolved.session_id == env.parent, pin
    assert env.switches == []


@pytest.mark.asyncio
async def test_delegate_child_pin_follows_parent_compression(env):
    env.delegate("c1", env.parent)
    env.compress(env.parent, "p2")
    advanced = await env.runner._async_session_store.advance_compression_session(
        env.key, env.parent, "p2"
    )
    assert advanced is not None

    resolved = await env.runner._resolve_async_delegation_session(env.current(), "c1")

    assert resolved is not None and resolved.session_id == "p2"
    assert env.current().session_id == "p2"
    assert env.switches == []


@pytest.mark.asyncio
async def test_child_of_new_closed_parent_not_delivered_to_new_chat(env):
    env.delegate("child_1", env.parent)
    new_entry = env.store.reset_session(env.key)  # human /new
    assert new_entry.session_id != env.parent

    assert await env.classify("child_1") == "terminal"
    resolved = await env.runner._resolve_async_delegation_session(new_entry, "child_1")

    assert resolved is None
    assert env.current().session_id == new_entry.session_id


# ---------------------------------------------------------------------------
# MUST STAY GREEN
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_real_new_drops_old_pins(env):
    new_entry = env.store.reset_session(env.key)

    assert await env.classify(env.parent) == "terminal"
    assert await env.runner._resolve_async_delegation_session(new_entry, env.parent) is None
    assert env.current().session_id == new_entry.session_id


@pytest.mark.asyncio
async def test_human_switch_session_still_repoints(env):
    other = env.store.reset_session(env.key)  # parent ended session_reset
    switched = env.store.switch_session(env.key, env.parent)  # human /resume

    assert switched.session_id == env.parent
    assert env.current().session_id == env.parent
    assert env.ended(other.session_id)[1] == "session_switch"
    assert env.ended(env.parent)[0] is None  # reopened
    assert await env.classify(env.parent) == "deliver"


@pytest.mark.asyncio
async def test_idle_ended_predecessor_delivers_to_successor(env):
    # Auto-reset (idle) stamps _reset_from on the successor.
    env.db.create_session(
        "succ",
        source="telegram",
        parent_session_id=env.parent,
        model_config={"_reset_from": env.parent},
    )
    succ = env.store.switch_session(env.key, "succ")
    env.switches.clear()
    # What an idle auto-reset leaves behind: predecessor ended "idle".
    _force_end_reason(env.db, env.parent, "idle")

    assert await env.classify(env.parent) == "deliver"
    resolved = await env.runner._resolve_async_delegation_session(succ, env.parent)
    assert resolved is not None and resolved.session_id == "succ"
    assert env.switches == []


@pytest.mark.asyncio
async def test_idle_then_human_new_is_unowned(env):
    _force_end_reason(env.db, env.parent, "idle")
    env.db.create_session(
        "succ",
        source="telegram",
        parent_session_id=env.parent,
        model_config={"_reset_from": env.parent},
    )
    env.store.switch_session(env.key, "succ")
    _force_end_reason(env.db, env.parent, "idle")
    new_entry = env.store.reset_session(env.key)  # human /new on succ

    assert await env.classify(env.parent) == "terminal"
    assert await env.runner._resolve_async_delegation_session(new_entry, env.parent) is None
    assert env.current().session_id == new_entry.session_id


@pytest.mark.asyncio
async def test_reset_resume_old_then_new_chat_child_is_unowned(env):
    a = env.parent
    b = env.store.reset_session(env.key).session_id  # A -> B (human /new)
    env.delegate("b_child", b)
    resumed = env.store.switch_session(env.key, a)  # human /resume A
    env.switches.clear()

    assert resumed.session_id == a
    assert await env.classify("b_child") == "terminal"
    assert await env.runner._resolve_async_delegation_session(resumed, "b_child") is None
    assert env.current().session_id == a
    assert env.switches == []


@pytest.mark.asyncio
async def test_unknown_or_missing_end_reason_on_path_is_unowned(env):
    _force_end_reason(env.db, env.parent, "mystery_reason")
    env.db.create_session(
        "succ",
        source="telegram",
        parent_session_id=env.parent,
        model_config={"_reset_from": env.parent},
    )
    env.store.switch_session(env.key, "succ")
    _force_end_reason(env.db, env.parent, "mystery_reason")

    assert await env.classify(env.parent) == "terminal"


@pytest.mark.asyncio
async def test_branch_edge_on_path_is_unowned(env):
    env.db.create_session(
        "branch",
        source="telegram",
        parent_session_id=env.parent,
        model_config={"_branched_from": env.parent},
    )
    env.store.switch_session(env.key, "branch")
    _force_end_reason(env.db, env.parent, "idle")

    # A pin on the branch's parent must not climb/descend through the branch.
    assert await env.classify(env.parent) == "terminal"


@pytest.mark.asyncio
async def test_pin_open_on_another_route_is_unowned(env, tmp_path):
    other = SessionSource(platform=Platform.TELEGRAM, chat_id="200", chat_type="dm", user_id="u2")
    other_entry = env.store.get_or_create_session(other)

    assert await env.classify(other_entry.session_id) == "terminal"
    resolved = await env.runner._resolve_async_delegation_session(env.entry, other_entry.session_id)
    assert resolved is None
    assert env.current().session_id == env.parent
    assert env.switches == []


@pytest.mark.asyncio
async def test_missing_pin_row_is_retry(env):
    assert await env.classify("does-not-exist") == "retry"


@pytest.mark.asyncio
async def test_delegate_cycle_is_retry_not_a_hang(env):
    env.db.create_session("x", source="telegram")
    env.db.create_session("y", source="telegram")

    def _link(conn):
        for child, parent in (("x", "y"), ("y", "x")):
            conn.execute(
                "UPDATE sessions SET parent_session_id = ?, model_config = ? WHERE id = ?",
                (parent, '{"_delegate_from": "%s"}' % parent, child),
            )

    env.db._execute_write(_link)
    assert await env.classify("x") == "retry"


@pytest.mark.asyncio
async def test_resolve_owner_returns_canonical_owner_even_when_not_deliverable(env):
    from gateway.completion_ownership import resolve_owner

    env.delegate("child_1", env.parent)
    new_entry = env.store.reset_session(env.key)

    res = await resolve_owner(
        "child_1", env.route(new_entry.session_id), db=env.runner._session_db
    )

    assert res.verdict == "unowned"
    assert res.owner_root_id == env.parent
    assert res.owner_tip_id == env.parent


# ---------------------------------------------------------------------------
# Topic lanes: binding is the route's current session for internal events
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_topic_lane_binding_index_disagreement_is_retry_and_never_switches(tmp_path):
    e = _Env(tmp_path, thread_id="555")
    try:
        e.delegate("child_1", e.parent)
        # The lane is bound to a different session than the index points at.
        e.db.create_session("bound", source="telegram")
        e.db.bind_telegram_topic(
            chat_id="100", thread_id="555", user_id="u1",
            session_key=e.key, session_id="bound",
        )
        e.runner._is_telegram_topic_lane = lambda source: True
        evt = {
            "type": "async_delegation",
            "delegation_id": "d1",
            "session_key": e.key,
            "platform": "telegram",
            "chat_id": "100",
            "thread_id": "555",
            "parent_session_id": "child_1",
        }

        route = await e.runner._completion_route(evt)
        assert route is not None
        assert route.current_session_id == "bound"  # binding, tip-walked
        assert await e.runner._classify_completion_target("child_1", route) == "retry"
        assert e.switches == []
        assert e.current().session_id == e.parent
    finally:
        e.db.close()


# ---------------------------------------------------------------------------
# Producer stamp
# ---------------------------------------------------------------------------
def test_route_owner_is_captured_once_through_nested_delegation():
    from agent.delegation_context import delegated_child_context
    from gateway.session_context import get_route_owner_session_id, scoped_current_session_id

    assert get_route_owner_session_id() == ""
    with scoped_current_session_id("root_sess"):
        with delegated_child_context("child_a"):
            assert get_route_owner_session_id() == "root_sess"
            with delegated_child_context("child_b"):
                assert get_route_owner_session_id() == "root_sess"
    assert get_route_owner_session_id() == ""


# ---------------------------------------------------------------------------
# Drop site: unowned -> inbox record dropped, notice pending
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unowned_process_completion_drop_writes_dropped_inbox_record(env):
    env.delegate("child_1", env.parent)
    env.store.reset_session(env.key)  # parent closed by the human
    runner = env.runner
    runner._completion_delivery_lock = __import__("threading").Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = {}
    runner._completion_delivery_retention = 16
    runner._inject_watch_notification = AsyncMock(return_value=True)
    evt = {
        "type": "completion",
        "session_id": "proc_9",
        "session_key": env.key,
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "100",
        "thread_id": "",
        "command": "make test",
        "exit_code": 0,
        "output": "ok",
        "started_at": 1.0,
        "parent_session_id": "child_1",
    }

    result = await runner._deliver_completion_notification("done", evt)

    assert result is None
    runner._inject_watch_notification.assert_not_awaited()
    rec = env.db.inbox_get("proc:proc_9")
    assert rec is not None
    assert rec["state"] == "dropped"
    assert rec["notice_state"] == "pending"
    assert rec["kind"] == "process"
    assert rec["owner_root_id"] == env.parent
    assert rec["wake"] == 0
    assert rec["session_key"] == env.key
    assert rec["drop_reason"]


@pytest.mark.asyncio
async def test_unowned_delegation_completion_is_inboxed_and_durably_dropped(env):
    """Real durable ``async_delegations`` row: the result lands in the inbox as
    ``dropped`` (notice pending) AND the existing delivery_state='dropped' holds."""
    import tools.async_delegation as ad
    from tools.process_registry import process_registry

    ad._reset_for_tests()
    try:
        env.delegate("child_1", env.parent)
        env.store.reset_session(env.key)  # human closed the owner
        handle = ad.dispatch_async_delegation(
            goal="Audit the repo", context=None, toolsets=None, role="leaf",
            model="m", session_key=env.key, parent_session_id="child_1",
            runner=lambda: {"status": "completed", "summary": "found 3 issues"},
        )
        delegation_id = handle["delegation_id"]
        evt = None
        deadline = time.monotonic() + 5
        while evt is None and time.monotonic() < deadline:
            if process_registry.completion_queue.empty():
                time.sleep(0.02)
                continue
            cand = process_registry.completion_queue.get_nowait()
            if cand.get("delegation_id") == delegation_id:
                evt = cand
        assert evt is not None, "delegation never completed"

        runner = env.runner
        runner._completion_delivery_lock = __import__("threading").Lock()
        runner._completion_deliveries_inflight = set()
        runner._completion_deliveries_delivered = {}
        runner._completion_delivery_retention = 16
        runner._inject_watch_notification = AsyncMock(return_value=True)

        result = await runner._deliver_completion_notification("done", evt)

        assert result is None
        runner._inject_watch_notification.assert_not_awaited()
        rec = env.db.inbox_get(f"deleg:{delegation_id}")
        assert rec is not None
        assert (rec["state"], rec["notice_state"], rec["kind"]) == ("dropped", "pending", "delegation")
        assert rec["owner_root_id"] == env.parent
        assert rec["title"] == "Audit the repo"
        assert "found 3 issues" in rec["payload_json"]
        assert ad.get_durable_delegation(delegation_id)["delivery_state"] == "dropped"
    finally:
        ad._reset_for_tests()
        while not process_registry.completion_queue.empty():
            process_registry.completion_queue.get_nowait()


# ---------------------------------------------------------------------------
# Allowlist / liveness boundaries
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_index_entry_is_retry_not_unowned(env):
    # No index entry: the route lookup may still recover the live session.
    route = ("default", env.key, "", "telegram", "100", None)
    assert await env.runner._classify_completion_target(env.parent, route) == "retry"


@pytest.mark.asyncio
async def test_ws_orphan_reap_is_recoverable_only_for_the_routes_own_session(env):
    _force_end_reason(env.db, env.parent, "ws_orphan_reap")
    # pin == route's current session: the route lookup reopens it.
    assert await env.classify(env.parent) == "deliver"

    # As a reset predecessor it proves nothing about succession.
    env.db.create_session(
        "succ", source="telegram", parent_session_id=env.parent,
        model_config={"_reset_from": env.parent},
    )
    env.store.switch_session(env.key, "succ")
    _force_end_reason(env.db, env.parent, "ws_orphan_reap")
    assert await env.classify(env.parent) == "terminal"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["suspended", "resume_pending_expired"])
async def test_store_produced_auto_reset_reasons_stay_unowned_on_purpose(env, reason):
    env.db.create_session(
        "succ", source="telegram", parent_session_id=env.parent,
        model_config={"_reset_from": env.parent},
    )
    env.store.switch_session(env.key, "succ")
    _force_end_reason(env.db, env.parent, reason)

    assert await env.classify(env.parent) == "terminal"
    res = await _resolve(env, env.parent)
    assert res.verdict == "unowned" and reason in res.reason

    # ...also when it is the route's own ended session.
    _force_end_reason(env.db, "succ", reason)
    assert (await _resolve(env, "succ")).verdict == "unowned"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["idle", "daily", "agent_close"])
async def test_ended_root_without_successor_is_retry_outside_a_topic_lane(env, reason):
    _force_end_reason(env.db, env.parent, reason)

    res = await _resolve(env, env.parent)

    assert (res.verdict, res.reason) == ("retry", "owner_ended_no_successor")
    assert res.owner_root_id == env.parent
    assert await env.classify(env.parent) == "retry"
    assert await env.runner._resolve_async_delegation_session(env.current(), env.parent) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,verdict", [("idle", "deliver"), ("daily", "deliver"), ("agent_close", "retry")])
async def test_topic_lane_delivers_to_an_idle_or_daily_ended_bound_session(env, reason, verdict):
    _force_end_reason(env.db, env.parent, reason)
    route = ("default", env.key, env.parent, "telegram", "100", None, env.parent, True)

    assert (await _resolve(env, env.parent, route)).verdict == verdict


@pytest.mark.asyncio
async def test_live_root_still_delivers(env):
    assert (await _resolve(env, env.parent)).verdict == "deliver"


async def _resolve(env, pin, route=None):
    from gateway.completion_ownership import resolve_owner

    return await resolve_owner(pin, route or env.route(), db=env.runner._session_db)


def _force_end_reason(db, session_id, reason):
    """Rewrite an end_reason the way an auto-reset would have left it."""
    def _do(conn):
        conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
            (time.time(), reason, session_id),
        )
    db._execute_write(_do)
