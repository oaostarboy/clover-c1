"""``/results``: read-only view of background results, scoped by route AND owner.

Real ``SessionStore`` + ``SessionDB`` in a temp dir. Listing and explicit-key
reads apply the same two checks: the record's route equals the caller's, and
its owner passes the ownership path rule for the caller's current session.
An id alone never grants access.
"""

from __future__ import annotations

import json
import time

import pytest

from clover_state import AsyncSessionDB, SessionDB
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionSource, SessionStore


class _Chat:
    """One telegram route (chat/thread) on a shared store, DB and runner."""

    def __init__(self, env, chat_id="100", thread_id=None, user_id="u1", chat_type="dm"):
        self.env = env
        self.source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            thread_id=thread_id,
        )
        self.entry = env.store.get_or_create_session(self.source)
        self.key = self.entry.session_key
        self.chat_id = chat_id
        self.thread_id = thread_id

    @property
    def session_id(self):
        return self.env.store.lookup_by_session_key(self.key).session_id

    def put(self, key, *, owner=None, state="dropped", payload=None, title="Research task",
            kind="delegation", chat_id=None, thread_id="__same__"):
        db = self.env.db
        db.inbox_put({
            "key": key,
            "profile": "default",
            "platform": "telegram",
            "chat_id": chat_id or self.chat_id,
            "thread_id": self.thread_id if thread_id == "__same__" else thread_id,
            "session_key": self.key,
            "owner_root_id": owner or self.entry.session_id,
            "kind": kind,
            "wake": 0,
            "title": title,
            "payload_json": json.dumps(payload if payload is not None else {"goal": title, "status": "completed", "summary": "all done"}),
            "shown_to_user": 0,
        })
        if state == "dropped":
            assert db.inbox_drop(key, "unowned:test")

    async def results(self, args=""):
        text = "/results" + (f" {args}" if args else "")
        event = MessageEvent(text=text, source=self.source, message_id="m1")
        return await self.env.runner._handle_results_command(event)


class _Env:
    def __init__(self, tmp_path):
        self.db = SessionDB(db_path=tmp_path / "state.db")
        self.store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
        self.store._db = self.db
        runner = object.__new__(GatewayRunner)
        runner._session_db = AsyncSessionDB(self.db)
        runner.session_store = self.store
        runner._async_session_store = AsyncSessionStore(self.store)
        runner.config = GatewayConfig()
        runner._session_source_cache = {}
        self.runner = runner


@pytest.fixture
def env(tmp_path):
    e = _Env(tmp_path)
    try:
        yield e
    finally:
        e.db.close()


@pytest.mark.asyncio
async def test_lists_only_this_routes_records(env):
    mine, other = _Chat(env, "100"), _Chat(env, "200")
    mine.put("deleg:mine", title="My research")
    other.put("deleg:theirs", title="Their research")

    out = await mine.results()

    assert "deleg:mine" in out and "My research" in out
    assert "deleg:theirs" not in out and "Their research" not in out
    assert "No background results" in await _Chat(env, "300").results()


@pytest.mark.asyncio
async def test_explicit_key_from_another_chat_is_denied(env):
    mine, other = _Chat(env, "100"), _Chat(env, "200")
    other.put("deleg:theirs", title="Their research")

    out = await mine.results("deleg:theirs")

    assert "Their research" not in out and "all done" not in out
    assert out == (await mine.results("deleg:does-not-exist")).replace("does-not-exist", "theirs")


@pytest.mark.asyncio
async def test_other_topic_of_the_same_chat_is_denied(env):
    topic_a = _Chat(env, "100", thread_id="9")
    topic_b = _Chat(env, "100", thread_id="8")
    topic_a.put("deleg:a", title="Topic A work")

    assert "deleg:a" in await topic_a.results()
    assert "deleg:a" not in await topic_b.results()
    assert "all done" not in await topic_b.results("deleg:a")
    assert "all done" not in await _Chat(env, "100").results("deleg:a")  # main DM lane too


@pytest.mark.asyncio
async def test_after_new_the_old_conversations_results_are_hidden_until_resumed(env):
    chat = _Chat(env, "100")
    old = chat.session_id
    chat.put("deleg:old", owner=old, title="Old chat work")
    assert "deleg:old" in await chat.results()

    env.store.reset_session(chat.key)  # human /new

    assert chat.session_id != old
    assert "deleg:old" not in await chat.results()
    denied = await chat.results("deleg:old")
    assert "all done" not in denied and "Old chat work" not in denied

    env.store.switch_session(chat.key, old)  # human /resume
    assert "deleg:old" in await chat.results()
    assert "all done" in await chat.results("deleg:old")


@pytest.mark.asyncio
async def test_results_follow_an_idle_reset_successor(env):
    chat = _Chat(env, "100")
    old = chat.session_id
    chat.put("deleg:old", owner=old)
    env.db.create_session(
        "succ", source="telegram", parent_session_id=old,
        model_config={"_reset_from": old},
    )
    env.store.switch_session(chat.key, "succ")
    env.db._execute_write(lambda c: c.execute(
        "UPDATE sessions SET ended_at = ?, end_reason = 'idle' WHERE id = ?", (time.time(), old)
    ))

    assert "deleg:old" in await chat.results()


@pytest.mark.asyncio
async def test_record_with_unresolvable_owner_is_not_shown(env):
    chat = _Chat(env, "100")
    chat.put("deleg:y", owner="no-such-session")

    assert "deleg:y" not in await chat.results()
    assert "all done" not in await chat.results("deleg:y")


@pytest.mark.asyncio
async def test_ownerless_record_is_shown_on_its_exact_route_only(env):
    """An unpinned watch drop has no verified owner: it is stored with the route
    the event was addressed to and shown only to that exact audience."""
    chat, other_chat = _Chat(env, "100"), _Chat(env, "200")
    topic_a, topic_b = _Chat(env, "300", thread_id="9"), _Chat(env, "300", thread_id="8")
    for c, key in ((chat, "proc-watch:p:1"), (topic_a, "proc-watch:p:2")):
        c.put(key, kind="process", title="watch hit")
        env.db._execute_write(lambda conn, k=key: conn.execute(
            "UPDATE session_inbox SET owner_root_id = NULL WHERE key = ?", (k,)))

    assert "proc-watch:p:1" in await chat.results()
    assert "all done" in await chat.results("proc-watch:p:1")
    assert "proc-watch:p:1" not in await other_chat.results()
    assert "all done" not in await other_chat.results("proc-watch:p:1")
    assert "proc-watch:p:2" in await topic_a.results()
    assert "proc-watch:p:2" not in await topic_b.results()
    assert "proc-watch:p:2" not in await chat.results()


@pytest.mark.asyncio
async def test_routeless_record_is_found_by_owner_from_the_owners_conversation(env):
    mine, other = _Chat(env, "100"), _Chat(env, "200")
    mine.put("deleg:lost", owner=mine.session_id, title="Lost route")
    env.db._execute_write(lambda c: c.execute(
        "UPDATE session_inbox SET platform = NULL, chat_id = NULL, thread_id = NULL, "
        "profile = NULL WHERE key = 'deleg:lost'"))

    assert "deleg:lost" in await mine.results()
    assert "all done" in await mine.results("deleg:lost")
    assert "deleg:lost" not in await other.results()
    assert "all done" not in await other.results("deleg:lost")

    env.store.reset_session(mine.key)  # human /new: the owner is closed
    assert "deleg:lost" not in await mine.results()


@pytest.mark.asyncio
async def test_shows_state_and_payload_for_delegation_and_process(env):
    chat = _Chat(env, "100")
    chat.put("deleg:d1", title="Audit", payload={"goal": "Audit the repo", "status": "completed", "summary": "found 3 issues"})
    chat.put("proc:p1", kind="process", title="make test",
             payload={"command": "make test", "exit_code": 2, "output": "FAILED tests/x"})

    listing = await chat.results()
    delegation = await chat.results("deleg:d1")
    process = await chat.results("proc:p1")

    assert "[not delivered (unowned:test)]" in listing
    assert "Summary: found 3 issues" in delegation
    assert "Exit code: 2" in process and "FAILED tests/x" in process


@pytest.mark.asyncio
async def test_pruned_payload_says_expired(env):
    chat = _Chat(env, "100")
    chat.put("deleg:old", title="Old work")
    assert env.db.inbox_prune_payloads(0) == 1

    out = await chat.results("deleg:old")

    assert "payload expired" in out
    assert "all done" not in out


@pytest.mark.asyncio
async def test_payload_secrets_are_redacted(env):
    chat = _Chat(env, "100")
    secret = "sk-" + "a" * 40
    chat.put("deleg:s", payload={"goal": "g", "summary": f"token is {secret}"})

    assert secret not in await chat.results("deleg:s")


@pytest.mark.asyncio
async def test_results_is_read_only(env):
    chat = _Chat(env, "100")
    chat.put("deleg:a")
    chat.put("deleg:b", state="pending")
    before = {r["key"]: r for r in env.db.inbox_for_route("default", "telegram", "100", None)}

    await chat.results()
    await chat.results("deleg:a")
    await chat.results("deleg:b")

    after = {r["key"]: r for r in env.db.inbox_for_route("default", "telegram", "100", None)}
    assert before == after


def test_results_is_a_registered_gateway_command():
    from clover_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command

    cmd = resolve_command("results")
    assert cmd is not None and cmd.name == "results"
    assert "results" in GATEWAY_KNOWN_COMMANDS


@pytest.mark.asyncio
async def test_ownerless_record_in_a_per_user_group_session_is_private_to_that_user(env):
    """Group sessions are per user (``...:-100777:u1`` / ``:u2``): the audience of
    an unpinned watch event is the one user whose session it was addressed to."""
    u1 = _Chat(env, "-100777", user_id="u1", chat_type="group")
    u2 = _Chat(env, "-100777", user_id="u2", chat_type="group")
    assert u1.key != u2.key and u1.chat_id == u2.chat_id
    u1.put("proc-watch:p:1", kind="process", title="u1 watch hit",
           payload={"command": "tail -f x", "output": "u1 private output"})
    env.db._execute_write(lambda c: c.execute(
        "UPDATE session_inbox SET owner_root_id = NULL WHERE key = 'proc-watch:p:1'"))

    assert "proc-watch:p:1" in await u1.results()
    assert "u1 private output" in await u1.results("proc-watch:p:1")
    assert "proc-watch:p:1" not in await u2.results()
    assert "u1 private output" not in await u2.results("proc-watch:p:1")
