"""Long-running helper cards must survive ordinary, worker-free chat turns."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent import ack_checklist as ac
from gateway import delegation_activity as da
from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway.test_ack_card_follow import _opener_card, _real_chat, _settle
from tests.gateway.test_delegation_activity import _child_cb, _turn_runner
from tests.gateway.test_delegation_card_follow import _SeqAdapter

pytestmark = pytest.mark.unified_card


@pytest.fixture(autouse=True)
def clean_cards(monkeypatch):
    ac.reset_for_tests()
    ac.set_style_for_tests("unified")
    registries = (da._LIVE, da._ACK_EDITORS, da._LAST_OUT, da._LAST_PLAIN, da._BOARDS)
    for registry in registries:
        registry.clear()
    monkeypatch.setattr(da, "FOLLOW_DEBOUNCE_SECONDS", 0.0)
    yield
    for timer in da._FOLLOW_TIMERS.values():
        timer.cancel()
    da._FOLLOW_TIMERS.clear()
    for editor in da._ACK_EDITORS.values():
        editor.gone = True
        if editor._task is not None:
            editor._task.cancel()
        if editor._repost_timer is not None:
            editor._repost_timer.cancel()
    for board in da._BOARDS.values():
        if board._timer is not None:
            board._timer.cancel()
    for registry in registries:
        registry.clear()
    ac.reset_for_tests()


def publisher(chat, chat_id="chat"):
    return da.DelegationActivityPublisher(
        adapter=chat, chat_id=chat_id, loop=asyncio.get_running_loop(),
        combined=True, auto_heartbeat=False, heartbeat_seconds=0, min_interval=0,
    )


@pytest.mark.asyncio
async def test_running_card_keeps_following_after_many_worker_free_turns():
    chat = _real_chat()  # Real BasePlatformAdapter.send outbound/bind wrapper.
    card = _opener_card("deleg_release", "finish release", "a tested release", "working")
    assert card is not None
    await chat.send("chat", card.say_text())
    editor = da._ACK_EDITORS[id(card)]
    now = [1000.0]
    editor._clock = lambda: now[0]
    turns = []
    try:
        # Every gateway turn constructs a combined publisher, even if it launches
        # no workers. The worker from the first turn is still running throughout.
        for turn in range(da._LIVE_MAX_PER_CHAT + 2):
            turns.append(publisher(chat))
            now[0] += da.ACK_REPOST_MIN_SECONDS + 1
            await chat.send("chat", f"ordinary reply {turn}")
            await _settle()
        assert chat.sent[-1] == editor._render(), "running card must follow the last reply"
        assert int(editor._message_id) == chat.n
        key = da._inbox_key(chat, "chat")
        assert editor in da._LIVE[key], "idle turn publishers must not evict the running editor"
        assert len([p for p in da._LIVE[key] if isinstance(p, da.DelegationActivityPublisher)]) == 0
    finally:
        for pub in turns:
            await pub.aclose()


def telegram_chat():
    """Production adapter/wrapper, but no network: record every Bot API call."""
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._rich_send_disabled = True
    wire = []
    next_id = 100

    async def send_message(**kwargs):
        nonlocal next_id
        next_id += 1
        wire.append((next_id, kwargs))
        return MagicMock(message_id=next_id)

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=send_message)
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=100))
    bot.delete_message = AsyncMock(return_value=True)
    adapter._bot = bot
    return adapter, wire


@pytest.mark.asyncio
async def test_new_dispatch_joins_and_follows_stranded_release_card_after_turn_churn(monkeypatch):
    from tools import delegation_inflight

    monkeypatch.setattr(da, "ACK_REPOST_MIN_SECONDS", 0.0)
    # Real record writer, confined by conftest's temporary CLOVER_HOME.
    for did in ("deleg_release", "deleg_repair"):
        assert delegation_inflight.write_dispatch_record(
            did, tasks=[{"goal": "test card follow"}],
            session_key="agent:main:telegram:dm:123", parent_session_id="test-parent",
            origin={"origin_platform": "telegram", "origin_chat_id": "123"},
        )
    chat, wire = telegram_chat()
    first = _opener_card("deleg_release", "finish release", "a tested release", "working")
    assert first is not None
    await chat.send("123", first.say_text())
    editor = da._ACK_EDITORS[id(first)]
    editor._min_interval = 0
    turns = []
    try:
        for turn in range(da._LIVE_MAX_PER_CHAT + 2):
            turns.append(publisher(chat, "123"))
            await chat.send("123", f"ordinary reply {turn}")
            await _settle()
        # The new helper joins via the same dispatch publisher path as production.
        repair = publisher(chat, "123")
        turns.append(repair)
        assert repair.open_dispatch_card("deleg_repair", ["repair card follow"], work="repair card follow")
        await _settle()
        assert len(da._ACK_EDITORS) == 1
        assert {p.delegation_id for p in first.parts} == {"deleg_release", "deleg_repair"}
        await chat.send("123", "both helpers are running")
        await _settle()
        assert wire[-1][0] == int(editor._message_id)
        assert "repair card follow" in editor._last_text
        assert chat._bot is not None
        assert all(str(call.kwargs["chat_id"]) == "123" for call in chat._bot.send_message.await_args_list)
        deleted = {int(call.kwargs["message_id"]) for call in chat._bot.delete_message.await_args_list}
        cards = {mid for mid, kw in wire if "tap to watch" in kw["text"]}
        assert cards - deleted == {int(editor._message_id)}, "exactly one persistent running card"
        assert first.target == (id(chat), "123", editor._message_id)
        for did in ("deleg_release", "deleg_repair"):
            record = delegation_inflight.read_record(did)
            assert record is not None
            assert record["parent_session_id"] == "test-parent"
            assert record["card"] == {
                "platform": "telegram", "chat_id": "123", "thread_id": "",
                "message_id": editor._message_id,
            }
    finally:
        for pub in turns:
            await pub.aclose()


@pytest.mark.asyncio
async def test_deferred_follow_timer_fires_after_turn_churn(monkeypatch):
    monkeypatch.setattr(da, "ACK_REPOST_MIN_SECONDS", 0.05)
    chat = _real_chat()
    card = _opener_card("deleg_release", "finish release", "a tested release", "working")
    assert card is not None
    await chat.send("chat", card.say_text())
    editor = da._ACK_EDITORS[id(card)]
    await chat.send("chat", "first reply")
    await _settle()
    first_moved = editor._message_id
    turns = [publisher(chat) for _ in range(da._LIVE_MAX_PER_CHAT + 2)]
    try:
        await chat.send("chat", "second reply")
        await _settle()
        assert editor._repost_timer is not None
        assert editor._message_id == first_moved
        await asyncio.sleep(0.12)  # Exercise the actual call_later callback, not a manual repost.
        assert editor._repost_timer is None
        assert int(editor._message_id) == chat.n
        assert chat.sent[-1] == editor._render()
        assert chat.deleted == ["102", first_moved]
    finally:
        for pub in turns:
            await pub.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "interrupted"])
async def test_completion_or_stop_before_deferred_follow_does_not_repost(monkeypatch, status):
    monkeypatch.setattr(da, "ACK_REPOST_MIN_SECONDS", 0.05)
    chat = _real_chat()
    card = _opener_card("deleg_release", "finish release", "a tested release", "working")
    assert card is not None
    await chat.send("chat", card.say_text())
    editor = da._ACK_EDITORS[id(card)]
    editor._min_interval = 0
    await chat.send("chat", "first reply")
    await _settle()
    await chat.send("chat", "second reply")
    await _settle()
    assert editor._repost_timer is not None
    before = (list(chat.sent), list(chat.deleted), editor._message_id)
    ac.on_child_event("subagent.complete", delegation_id="deleg_release", status=status, summary="Plain summary: finished.")
    await asyncio.sleep(0.12)
    assert card.finished
    assert (chat.sent, chat.deleted, editor._message_id) == before
    assert await da.follow_latest_message(chat, "chat") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("combined", [False, True])
async def test_active_legacy_publishers_survive_idle_turns_and_follow(monkeypatch, combined):
    ac.set_style_for_tests("classic")
    monkeypatch.setattr(da, "BOARD_MIN_EDIT_SECONDS", 0)
    chat = _SeqAdapter()
    pubs = [publisher(chat, "chat-A"), publisher(chat, "chat-A")]
    for pub in pubs:
        pub._combined = combined
        pub._board_mode = combined
    idle = []
    try:
        for i, pub in enumerate(pubs):
            _child_cb(_turn_runner(pub), delegation_id=f"deleg_{i}", subagent_id=f"sa-{i}")("subagent.start", preview=f"worker {i}")
            await pub.drain()
        for _ in range(da._LIVE_MAX_PER_CHAT + 2):
            idle.append(publisher(chat, "chat-A"))
        reply = await chat.send("chat-A", "ordinary reply")
        await _settle()
        live = da._LIVE[da._inbox_key(chat, "chat-A")]
        assert all(pub in live for pub in pubs)
        if combined:
            board = da._BOARDS[da._inbox_key(chat, "chat-A")]
            assert board.message_id is not None and reply.message_id is not None
            assert int(board.message_id) > int(reply.message_id)
            assert len(board.children()[0]) == 2
        else:
            for pub in pubs:
                message_id = next(iter(pub._cards.values())).message_id
                assert message_id is not None and reply.message_id is not None
                assert int(message_id) > int(reply.message_id)
    finally:
        for pub in pubs + idle:
            await pub.aclose()


@pytest.mark.asyncio
async def test_foreign_adapter_or_chat_sends_do_not_move_running_card():
    chat, other = _real_chat(), _real_chat()
    card = _opener_card("deleg_release", "finish release", "a tested release", "working")
    assert card is not None
    await chat.send("chat", card.say_text())
    editor = da._ACK_EDITORS[id(card)]
    original = editor._message_id
    await other.send("chat", "other profile adapter")
    await chat.send("other-chat", "different chat")
    await _settle()
    assert editor._message_id == original
    assert chat.deleted == []
    await chat.send("chat", "same owner reply", metadata={"_interim_send": True})
    await _settle()
    assert editor._message_id != original
    assert chat.sent[-1] == editor._render()
