"""Notices for background results dropped before the assistant saw them.

Real ``SessionDB`` in a temp dir; only the send callable / adapter is fake.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from clover_state import AsyncSessionDB, SessionDB
from gateway.config import GatewayConfig, Platform
from gateway.inbox_notices import (
    BACKOFF_INITIAL_S,
    BACKOFF_MAX_S,
    NoticeNotSent,
    NoticeSweepState,
    notice_text,
    sweep_inbox_notices,
)
from gateway.run import GatewayRunner


@pytest.fixture
def db(tmp_path):
    handle = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield handle
    finally:
        handle.close()


def _dropped(db, key, *, chat_id="100", thread_id=None, title="Research task", shown=0):
    db.inbox_put({
        "key": key,
        "profile": "default",
        "platform": "telegram",
        "chat_id": chat_id,
        "thread_id": thread_id,
        "session_key": f"agent:main:telegram:dm:{chat_id}",
        "owner_root_id": "root",
        "kind": "delegation",
        "wake": 0,
        "title": title,
        "payload_json": "{}",
        "shown_to_user": shown,
    })
    assert db.inbox_drop(key, "unowned:test")


class _Sender:
    def __init__(self):
        self.calls = []

    async def __call__(self, record, text):
        self.calls.append((record["key"], record["chat_id"], record["thread_id"], text))


@pytest.mark.asyncio
async def test_one_notice_per_record_and_never_resent(db):
    _dropped(db, "deleg:a")
    _dropped(db, "deleg:b", chat_id="200", thread_id="9")
    send = _Sender()

    first = await sweep_inbox_notices(db, send)
    second = await sweep_inbox_notices(db, send)

    assert first["sent"] == 2 and second["sent"] == 0
    assert sorted(c[0] for c in send.calls) == ["deleg:a", "deleg:b"]
    assert db.inbox_get("deleg:a")["notice_state"] == "sent"
    assert db.inbox_get("deleg:b")["notice_state"] == "sent"


@pytest.mark.asyncio
async def test_each_notice_goes_to_its_own_route(db):
    _dropped(db, "deleg:a", chat_id="100")
    _dropped(db, "deleg:b", chat_id="200", thread_id="9")
    send = _Sender()

    await sweep_inbox_notices(db, send)

    routes = {c[0]: (c[1], c[2]) for c in send.calls}
    assert routes == {"deleg:a": ("100", None), "deleg:b": ("200", "9")}


@pytest.mark.asyncio
async def test_exception_after_possible_send_is_uncertain_and_never_resent(db):
    _dropped(db, "deleg:a")
    attempts = []

    async def boom(record, text):
        attempts.append(record["key"])
        raise TimeoutError("socket died mid-send")

    out = await sweep_inbox_notices(db, boom)
    again = await sweep_inbox_notices(db, boom)

    assert out["uncertain"] == 1 and again == {"sent": 0, "uncertain": 0, "deferred": 0}
    assert attempts == ["deleg:a"]
    assert db.inbox_get("deleg:a")["notice_state"] == "uncertain"


@pytest.mark.asyncio
async def test_definitively_unsent_notice_is_retried_next_tick(db):
    _dropped(db, "deleg:a")
    offline = {"on": True}
    sent = []

    async def send(record, text):
        if offline["on"]:
            raise NoticeNotSent("no live adapter")
        sent.append(record["key"])

    assert (await sweep_inbox_notices(db, send))["deferred"] == 1
    assert db.inbox_get("deleg:a")["notice_state"] == "pending"
    offline["on"] = False
    assert (await sweep_inbox_notices(db, send))["sent"] == 1
    assert sent == ["deleg:a"]


@pytest.mark.asyncio
async def test_records_the_user_already_saw_get_no_notice(db):
    _dropped(db, "council:p:r1:final", shown=1)
    send = _Sender()

    await sweep_inbox_notices(db, send)

    assert send.calls == []
    assert db.inbox_get("council:p:r1:final")["notice_state"] is None


@pytest.mark.asyncio
async def test_a_claimed_notice_is_not_sent_by_a_second_sweeper(db):
    _dropped(db, "deleg:a")
    assert db.inbox_set_notice_state("deleg:a", "uncertain", "pending")
    send = _Sender()

    await sweep_inbox_notices(db, send)

    assert send.calls == []


def test_notice_text_is_short_redacted_and_points_at_results():
    secret = "sk-" + "a" * 40
    text = notice_text(
        {"title": f"Deploy with token {secret}\n and a very long tail " + "x" * 200},
        redact=lambda t: t.replace(secret, "***"),
    )

    assert text.startswith("Background result not delivered to the assistant: ")
    assert text.endswith(
        "Use /results in that conversation (resume it with /resume if you started a new one)."
    )
    assert secret not in text and "\n" not in text
    assert len(text) < 260
    assert "background task" in notice_text({"title": ""})


def test_set_notice_state_rejects_unknown_states(db):
    _dropped(db, "deleg:a")
    with pytest.raises(ValueError):
        db.inbox_set_notice_state("deleg:a", "bogus")
    assert db.inbox_set_notice_state("deleg:a", "sent", "pending") is True
    assert db.inbox_set_notice_state("deleg:a", "sent", "pending") is False


# ---------------------------------------------------------------------------
# Runner sender: the record's own route and thread, never the active chat
# ---------------------------------------------------------------------------
def _runner(adapter):
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.config = SimpleNamespace(multiplex_profiles=False)
    return runner


@pytest.mark.asyncio
async def test_runner_sends_to_the_records_route(db):
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner = _runner(adapter)
    _dropped(db, "deleg:a", chat_id="200", thread_id="9")

    out = await sweep_inbox_notices(AsyncSessionDB(db), runner._send_inbox_notice)

    assert out["sent"] == 1
    (chat_id, text), kwargs = adapter.send.await_args.args, adapter.send.await_args.kwargs
    assert chat_id == "200"
    assert "/results" in text
    assert str((kwargs.get("metadata") or {}).get("thread_id")) == "9"


@pytest.mark.asyncio
async def test_runner_defers_only_when_nothing_could_have_been_sent(db):
    runner = _runner(adapter=None)
    runner.adapters = {}
    _dropped(db, "deleg:a")

    assert (await sweep_inbox_notices(AsyncSessionDB(db), runner._send_inbox_notice))["deferred"] == 1
    assert db.inbox_get("deleg:a")["notice_state"] == "pending"

    runner = _runner(SimpleNamespace(send=AsyncMock(), is_connected=False))
    assert (await sweep_inbox_notices(AsyncSessionDB(db), runner._send_inbox_notice))["deferred"] == 1
    runner.adapters[Platform.TELEGRAM].send.assert_not_awaited()
    assert db.inbox_get("deleg:a")["notice_state"] == "pending"


@pytest.mark.asyncio
async def test_a_failed_send_result_is_ambiguous_and_never_resent(db):
    """Adapters fold post-send errors (a lost HTTP response) into success=False;
    resending would duplicate a notice that may already be on the user's screen."""
    adapter = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(success=False, error="read timeout after POST"))
    )
    runner = _runner(adapter)
    _dropped(db, "deleg:a")

    first = await sweep_inbox_notices(AsyncSessionDB(db), runner._send_inbox_notice)
    second = await sweep_inbox_notices(AsyncSessionDB(db), runner._send_inbox_notice)

    assert first["uncertain"] == 1 and second == {"sent": 0, "uncertain": 0, "deferred": 0}
    assert adapter.send.await_count == 1
    assert db.inbox_get("deleg:a")["notice_state"] == "uncertain"


# ---------------------------------------------------------------------------
# Pacing: backoff and no head-of-line starvation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_deferred_notices_back_off_exponentially_up_to_an_hour(db):
    _dropped(db, "deleg:a")
    now = [1000.0]
    state = NoticeSweepState()
    attempts = []

    async def offline(record, text):
        attempts.append(now[0])
        raise NoticeNotSent("no adapter")

    async def sweep():
        return await sweep_inbox_notices(db, offline, state=state, clock=lambda: now[0])

    await sweep()
    await sweep()  # inside the backoff window: not retried
    assert attempts == [1000.0]
    delays = []
    for _ in range(12):
        now[0] = state.deferred["deleg:a"][0]
        await sweep()
        delays.append(state.deferred["deleg:a"][1])
    assert delays[:3] == [BACKOFF_INITIAL_S * 2, BACKOFF_INITIAL_S * 4, BACKOFF_INITIAL_S * 8]
    assert max(delays) == BACKOFF_MAX_S and delays[-1] == BACKOFF_MAX_S


@pytest.mark.asyncio
async def test_unsendable_notices_do_not_starve_a_deliverable_one(db):
    """Astra's probe: 50 deferred records ahead of one deliverable record."""
    for i in range(50):
        _dropped(db, f"deleg:stuck{i:02d}", chat_id="999")
    _dropped(db, "deleg:ok", chat_id="100")
    sent = []

    async def send(record, text):
        if record["chat_id"] == "999":
            raise NoticeNotSent("bot blocked / adapter gone")
        sent.append(record["key"])

    state = NoticeSweepState()
    first = await sweep_inbox_notices(db, send, state=state)
    second = await sweep_inbox_notices(db, send, state=state)

    assert first["deferred"] == 50 and sent == ["deleg:ok"] and second["sent"] == 1
    assert db.inbox_get("deleg:ok")["notice_state"] == "sent"


# ---------------------------------------------------------------------------
# Multiplex: every hosted profile's database is swept in its own scope
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_secondary_profile_notices_are_swept_in_their_own_scope(tmp_path, monkeypatch):
    import clover_state
    import gateway.run as run_mod
    from clover_constants import get_clover_home
    from gateway.run import _SESSION_DB_UNPINNED, _profile_runtime_scope

    # tests/conftest.py pins DEFAULT_DB_PATH, which beats the profile scope and
    # would collapse root and secondary onto one database.
    monkeypatch.setattr(clover_state, "DEFAULT_DB_PATH", clover_state._IMPORT_DEFAULT_DB_PATH)
    sec_home = tmp_path / "profiles" / "sec"
    sec_home.mkdir(parents=True)
    monkeypatch.setattr(run_mod, "_multiplex_profile_homes", lambda cfg: [("sec", sec_home)])

    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._session_db_pinned = _SESSION_DB_UNPINNED
    runner._session_db_handles = {}
    runner._session_db_handles_lock = __import__("threading").Lock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.config = GatewayConfig(multiplex_profiles=True)

    root_path = runner._session_db._db.db_path
    with _profile_runtime_scope(sec_home):
        sec_path = runner._session_db._db.db_path
    assert root_path != sec_path, "root and secondary profile resolved the same database"
    assert Path_in(sec_path, sec_home) and Path_in(root_path, get_clover_home())

    root_db = runner._session_db._db
    _dropped(root_db, "deleg:root", chat_id="100")
    with _profile_runtime_scope(sec_home):
        sec_db = runner._session_db._db
        _dropped(sec_db, "deleg:sec", chat_id="200")

    await runner._inbox_notice_sweep_all()

    assert {c.args[0] for c in adapter.send.await_args_list} == {"100", "200"}
    assert root_db.inbox_get("deleg:root")["notice_state"] == "sent"
    assert sec_db.inbox_get("deleg:sec")["notice_state"] == "sent"
    runner.close_all_session_db_handles()


def Path_in(path, parent):
    from pathlib import Path

    return Path(parent).resolve() in Path(path).resolve().parents
