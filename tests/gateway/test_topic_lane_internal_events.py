"""Internal events on a Telegram topic lane: no auto-reset, never silent.

Drives the real pipeline entry (``_route_event_session``) with a real
``SessionStore`` + ``SessionDB`` and the idle reset policy. The lane's binding
decides where a human lands, so an internal event (a delegation / process
completion, a watch notification) must neither reset the lane's session behind
a successor nobody is bound to, nor vanish when it cannot be delivered.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from clover_state import AsyncSessionDB, SessionDB
from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionSource, SessionStore


class _Lane:
    def __init__(self, tmp_path, *, topic_lane=True, mode="idle"):
        config = GatewayConfig(default_reset_policy=SessionResetPolicy(mode=mode, idle_minutes=1))
        self.db = SessionDB(db_path=tmp_path / "state.db")
        self.store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
        self.store._db = self.db
        self.source = SessionSource(
            platform=Platform.TELEGRAM, chat_id="100", chat_type="dm",
            user_id="u1", thread_id="555",
        )
        self.entry = self.store.get_or_create_session(self.source)
        self.key = self.entry.session_key
        self.a = self.entry.session_id
        self.bind(self.a)
        runner = object.__new__(GatewayRunner)
        runner._session_db = AsyncSessionDB(self.db)
        runner.session_store = self.store
        runner._async_session_store = AsyncSessionStore(self.store)
        runner.config = config
        runner._session_source_cache = {}
        runner._is_telegram_topic_lane = lambda source, _v=topic_lane: _v
        self.runner = runner

    def bind(self, session_id):
        self.db.bind_telegram_topic(
            chat_id="100", thread_id="555", user_id="u1",
            session_key=self.key, session_id=session_id,
        )

    def age(self, minutes=10):
        e = self.store._entries[self.key]
        e.updated_at = datetime.now() - timedelta(minutes=minutes)
        self.store._save()

    def end(self, session_id, reason):
        self.db._execute_write(lambda c: c.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
            (time.time(), reason, session_id),
        ))

    def event(self, pin=None, **meta):
        md = dict(meta)
        if pin:
            md["gateway_session_id"] = pin
        return MessageEvent(text="[background result] all done", source=self.source,
                            internal=True, metadata=md)

    async def route(self, event):
        return await self.runner._route_event_session(event, self.source)

    def index(self):
        return self.store.lookup_by_session_key(self.key).session_id

    def inbox(self):
        return self.db.inbox_for_route("default", "telegram", "100", "555")


@pytest.fixture
def lane(tmp_path):
    ln = _Lane(tmp_path)
    try:
        yield ln
    finally:
        ln.db.close()


@pytest.mark.asyncio
async def test_internal_event_does_not_auto_reset_a_topic_lane(lane):
    lane.age()

    entry = await lane.route(lane.event(pin=lane.a, completion_inbox_key="deleg:d1"))

    assert entry is not None and entry.session_id == lane.a
    assert lane.index() == lane.a
    assert lane.db.get_session(lane.a)["ended_at"] is None
    assert lane.inbox() == []


@pytest.mark.asyncio
async def test_idle_finalized_bound_session_receives_the_result_and_is_reopened(lane):
    lane.end(lane.a, "idle")  # the expiry watcher finalized it; binding + index still point here

    entry = await lane.route(lane.event(pin=lane.a))

    assert entry is not None and entry.session_id == lane.a
    row = lane.db.get_session(lane.a)
    assert row["ended_at"] is None and row["end_reason"] is None
    assert lane.inbox() == []


@pytest.mark.asyncio
async def test_non_topic_route_internal_event_still_follows_auto_reset(tmp_path):
    ln = _Lane(tmp_path, topic_lane=False)
    try:
        ln.age()
        entry = await ln.route(ln.event(pin=ln.a))

        assert entry is not None and entry.session_id != ln.a
        assert ln.db.get_session(ln.a)["end_reason"] == "idle"
        assert ln.db.get_session(entry.session_id)["parent_session_id"] == ln.a
    finally:
        ln.db.close()


@pytest.mark.asyncio
async def test_pinned_event_with_binding_index_disagreement_is_recorded_not_silent(tmp_path):
    ln = _Lane(tmp_path, mode="none")
    try:
        ln.db.create_session("bound", source="telegram")
        ln.bind("bound")  # lane bound to B; the index still says A

        entry = await ln.route(ln.event(
            pin="bound", completion_inbox_key="deleg:d1",
            completion_kind="delegation", completion_title="Audit the repo",
        ))

        assert entry is None
        assert ln.index() == ln.a, "an internal event moved the route"
        (rec,) = ln.inbox()
        assert (rec["key"], rec["kind"], rec["state"], rec["notice_state"]) == (
            "deleg:d1", "delegation", "dropped", "pending",
        )
        assert rec["title"] == "Audit the repo"
        assert rec["drop_reason"] == "undeliverable:binding_index_disagree"
        assert rec["owner_root_id"] == "bound"
        assert "all done" in rec["payload_json"]
    finally:
        ln.db.close()


@pytest.mark.asyncio
async def test_unpinned_watch_event_with_disagreement_is_recorded_not_silent(tmp_path):
    ln = _Lane(tmp_path, mode="none")
    try:
        ln.db.create_session("bound", source="telegram")
        ln.bind("bound")

        assert await ln.route(ln.event(process_session_id="proc_w")) is None
        assert await ln.route(ln.event(process_session_id="proc_w")) is None

        recs = ln.inbox()
        assert len(recs) == 2, "two watch events must be two records"
        for rec in recs:
            assert rec["key"].startswith("proc-watch:proc_w:")
            assert (rec["kind"], rec["state"], rec["notice_state"]) == ("process", "dropped", "pending")
        assert recs[0]["key"] != recs[1]["key"]
    finally:
        ln.db.close()


@pytest.mark.asyncio
async def test_unowned_pinned_event_at_the_pipeline_is_recorded(tmp_path):
    ln = _Lane(tmp_path, topic_lane=False, mode="none")
    try:
        ln.db.create_session("elsewhere", source="telegram")  # live, owned by another route
        entry = await ln.route(ln.event(pin="elsewhere", completion_inbox_key="proc:p9",
                                        completion_kind="process"))
        assert entry is None
        ln.source = SessionSource(platform=Platform.TELEGRAM, chat_id="100", chat_type="dm",
                                  user_id="u1", thread_id="555")
        (rec,) = ln.db.inbox_for_route("default", "telegram", "100", "555")
        assert (rec["key"], rec["state"], rec["notice_state"]) == ("proc:p9", "dropped", "pending")
        assert rec["drop_reason"].startswith("unowned:")
    finally:
        ln.db.close()


@pytest.mark.asyncio
async def test_pipeline_drop_after_a_real_auto_reset_is_recorded(tmp_path):
    """Drives the production drop site through a REAL idle auto-reset: the route
    resets A -> B inside get_or_create_session, then a pin owned by another
    route is refused. The refusal must leave an inbox record, not a bare return."""
    ln = _Lane(tmp_path, topic_lane=False)
    try:
        ln.db.create_session("elsewhere", source="telegram")  # live session of another route
        ln.age()

        entry = await ln.route(ln.event(
            pin="elsewhere", completion_inbox_key="deleg:d9",
            completion_kind="delegation", completion_title="Foreign work",
        ))

        assert entry is None
        assert ln.db.get_session(ln.a)["end_reason"] == "idle"  # the reset really happened
        assert ln.index() != ln.a
        (rec,) = ln.inbox()
        assert (rec["key"], rec["state"], rec["notice_state"]) == ("deleg:d9", "dropped", "pending")
        assert rec["drop_reason"] == "unowned:owned_elsewhere"
        assert rec["owner_root_id"] == "elsewhere"
    finally:
        ln.db.close()


@pytest.mark.asyncio
async def test_inject_watch_notification_stamps_the_inbox_identity(lane):
    adapter = SimpleNamespace(handle_message=AsyncMock())
    lane.runner.adapters = {Platform.TELEGRAM: adapter}
    lane.runner._running = True
    evt = {
        "type": "async_delegation", "delegation_id": "d7", "goal": "Audit the repo",
        "session_key": lane.key, "parent_session_id": lane.a,
    }

    assert await lane.runner._inject_watch_notification("text", evt) is True

    md = adapter.handle_message.await_args.args[0].metadata
    assert md["completion_inbox_key"] == "deleg:d7"
    assert md["completion_kind"] == "delegation"
    assert md["completion_title"] == "Audit the repo"
    assert md["gateway_session_id"] == lane.a


# ---------------------------------------------------------------------------
# The expiry watcher: the real overnight case
# ---------------------------------------------------------------------------
async def _topic_route(lane):
    return await lane.runner._build_completion_route(
        lane.source, lane.key, lane.index(), entry=lane.store.lookup_by_session_key(lane.key),
    )


@pytest.mark.asyncio
async def test_expiry_finalized_bound_session_receives_the_result_and_is_reopened(lane):
    """The expiry watcher ends the lane's session ``session_reset`` (not ``idle``);
    the index and the binding still point at it and a human is switched back to it."""
    lane.age()
    lane.store.set_expiry_finalized(lane.store.lookup_by_session_key(lane.key))
    row = lane.db.get_session(lane.a)
    assert (row["end_reason"], row["expiry_finalized"]) == ("session_reset", 1)

    verdict = await lane.runner._resolve_completion_owner(lane.a, await _topic_route(lane))
    assert (verdict.verdict, verdict.reason) == ("deliver", "topic_lane_expired")

    entry = await lane.route(lane.event(pin=lane.a))

    assert entry is not None and entry.session_id == lane.a
    row = lane.db.get_session(lane.a)
    assert row["ended_at"] is None and row["end_reason"] is None
    assert lane.inbox() == []


@pytest.mark.asyncio
async def test_manual_new_on_the_lane_still_blocks(lane):
    lane.store.reset_session(lane.key)  # human /new: A ends session_reset, the index moves to B
    new_id = lane.index()
    assert new_id != lane.a
    lane.bind(new_id)  # the lane binding follows the new session
    assert lane.db.get_session(lane.a)["expiry_finalized"] in (0, None)

    verdict = await lane.runner._resolve_completion_owner(lane.a, await _topic_route(lane))
    assert verdict.verdict == "unowned"

    assert await lane.route(lane.event(pin=lane.a, completion_inbox_key="deleg:d2")) is None
    (rec,) = lane.inbox()
    assert (rec["key"], rec["state"], rec["notice_state"]) == ("deleg:d2", "dropped", "pending")
    assert rec["drop_reason"].startswith("unowned:user_boundary")
    assert lane.db.get_session(lane.a)["ended_at"] is not None


@pytest.mark.asyncio
async def test_session_reset_without_the_expiry_flag_is_a_boundary_even_if_the_index_is_stale(lane):
    lane.end(lane.a, "session_reset")  # a closed session the index/binding still point at

    verdict = await lane.runner._resolve_completion_owner(lane.a, await _topic_route(lane))

    assert (verdict.verdict, verdict.reason) == ("unowned", "route_session_closed")


# ---------------------------------------------------------------------------
# Strict-session branch (plugin injection): matching and mismatched pins
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_strict_branch_with_the_current_pin_returns_the_entry(tmp_path):
    ln = _Lane(tmp_path, topic_lane=False, mode="none")
    try:
        entry = await ln.route(ln.event(
            pin=ln.a, gateway_session_strict=True, gateway_session_key=ln.key,
        ))
        assert entry is not None and entry.session_id == ln.a
        assert ln.inbox() == []
    finally:
        ln.db.close()


@pytest.mark.asyncio
async def test_strict_branch_with_a_stale_pin_is_recorded_not_dropped_silently(tmp_path):
    ln = _Lane(tmp_path, topic_lane=False, mode="none")
    try:
        ln.db.create_session("stale", source="telegram")
        entry = await ln.route(ln.event(
            pin="stale", gateway_session_strict=True, gateway_session_key=ln.key,
            completion_inbox_key="proc:plug1", completion_kind="process",
        ))

        assert entry is None
        (rec,) = ln.inbox()
        assert (rec["key"], rec["state"], rec["notice_state"]) == ("proc:plug1", "dropped", "pending")
        assert rec["drop_reason"] == "undeliverable:strict_session_mismatch"
    finally:
        ln.db.close()


# ---------------------------------------------------------------------------
# expiry_finalized must prove the LATEST closure was an expiry (alias keys)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_alias_key_resume_then_manual_reset_is_not_an_expiry_closure(lane):
    """Astra's sequence: expiry-finalize A; another key resumes A; that key is
    reset by a human; the original topic binding/index still names A. The
    historical flag must not let an internal event reopen a manually closed A."""
    lane.age()
    lane.store.set_expiry_finalized(lane.store.lookup_by_session_key(lane.key))
    assert lane.db.get_session(lane.a)["expiry_finalized"] == 1

    other = SessionSource(platform=Platform.TELEGRAM, chat_id="200", chat_type="dm", user_id="u2")
    other_entry = lane.store.get_or_create_session(other)
    lane.store.switch_session(other_entry.session_key, lane.a)  # resume A under another key
    assert lane.db.get_session(lane.a)["ended_at"] is None
    lane.store.reset_session(other_entry.session_key)  # the human then resets that key

    row = lane.db.get_session(lane.a)
    assert (row["end_reason"], row["expiry_finalized"]) == ("session_reset", 0)
    assert lane.index() == lane.a  # the topic lane still names A

    verdict = await lane.runner._resolve_completion_owner(lane.a, await _topic_route(lane))
    assert (verdict.verdict, verdict.reason) == ("unowned", "route_session_closed")

    assert await lane.route(lane.event(pin=lane.a, completion_inbox_key="deleg:alias")) is None
    assert lane.db.get_session(lane.a)["ended_at"] is not None, "an internal event reopened A"
    (rec,) = lane.inbox()
    assert (rec["key"], rec["state"], rec["notice_state"]) == ("deleg:alias", "dropped", "pending")


def test_every_end_or_reopen_other_than_expiry_clears_the_flag(lane):
    db = lane.db

    def flagged(session_id):
        db.set_expiry_finalized(session_id, True)
        assert db.get_session(session_id)["expiry_finalized"] == 1

    # the watcher path keeps it (and sets it only because it ended the row)
    lane.store.set_expiry_finalized(lane.store.lookup_by_session_key(lane.key))
    assert db.get_session(lane.a)["expiry_finalized"] == 1

    db.reopen_session(lane.a)
    assert db.get_session(lane.a)["expiry_finalized"] == 0

    for how in ("end_session", "promote_to_session_reset"):
        db.create_session(f"s_{how}", source="telegram")
        flagged(f"s_{how}")
        if how == "end_session":
            db.end_session(f"s_{how}", "session_switch")
        else:
            db.promote_to_session_reset(f"s_{how}", "session_switch")
        row = db.get_session(f"s_{how}")
        assert row["ended_at"] is not None and row["expiry_finalized"] == 0, how


def test_expiry_finalization_of_an_already_closed_row_does_not_claim_it(lane):
    """The flag is set only when the expiry call itself ended the row."""
    lane.end(lane.a, "session_reset")  # someone else closed it first
    lane.store.set_expiry_finalized(lane.store.lookup_by_session_key(lane.key))

    assert lane.db.get_session(lane.a)["expiry_finalized"] in (0, None)
