"""A RUNNING unified helper card follows the bottom of the chat (like the old
🔀 roster card); a finished card stays where it is."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

import pytest

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl
from gateway import delegation_activity as da
from gateway.platforms.base import SendResult

pytestmark = pytest.mark.unified_card

HEAD = "\U0001f504 fixing the bugs \u2192 test build + PR, ~10 min"
DID = "deleg_follow01"


class _Chat:
    """A chat with sequential message ids; every send is noted like the real
    adapter wrapper does."""

    def __init__(self, can_delete: bool = True) -> None:
        self.n = 100
        self.sends: List[Dict[str, Any]] = []
        self.edits: List[Dict[str, Any]] = []
        self.deleted: List[str] = []
        if not can_delete:
            self.delete_message = None  # type: ignore[assignment]

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.n += 1
        mid = str(self.n)
        self.sends.append({"id": mid, "text": content})
        da.note_outbound(self, chat_id, mid, content)
        return SendResult(success=True, message_id=mid)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        self.edits.append({"id": message_id, "text": content})
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(str(message_id))
        return True


class _NoDeleteChat(_Chat):
    delete_message = None  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ac.reset_for_tests()
    ac.set_style_for_tests("unified")
    da._ACK_EDITORS.clear()
    da._LIVE.clear()
    da._LAST_OUT.clear()
    da._FOLLOW_TIMERS.clear()
    da._LAST_PLAIN.clear()
    monkeypatch.setattr(da, "FOLLOW_DEBOUNCE_SECONDS", 0.0)
    yield
    ac.reset_for_tests()
    da._ACK_EDITORS.clear()
    da._LIVE.clear()
    da._LAST_OUT.clear()
    da._LAST_PLAIN.clear()
    da._FOLLOW_TIMERS.clear()


def _card(did=DID, head=HEAD):
    r = fl.DispatchReceipt(delegation_id=did, seq=1, goals=("g",), summary=head, steps=("find", "fix"))
    return ac.card_for([r])


async def _bind(chat, card, clock=time.monotonic):
    sent = await chat.send("chat", card.initial_text)
    editor = da.AckCardEditor(
        card, chat, "chat", sent.message_id, asyncio.get_running_loop(),
        min_interval=0.0, clock=clock,
    )
    card.target = (id(chat), "chat", sent.message_id)
    return editor


async def _settle(n: int = 60):
    for _ in range(n):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_newer_message_moves_the_running_card_to_the_bottom():
    chat, card = _Chat(), _card()
    editor = await _bind(chat, card)
    first = editor._message_id
    await chat.send("chat", "the parent's reply")
    await _settle()
    assert len(chat.sends) == 3, "ack, reply, re-posted card"
    assert chat.sends[-1]["text"] == card.render()
    assert chat.deleted == [first], "the old card message is deleted"
    assert editor._message_id == chat.sends[-1]["id"]
    assert card.target[2] == editor._message_id


@pytest.mark.asyncio
async def test_later_edits_go_to_the_new_message_and_never_look_newer():
    chat, card = _Chat(), _card()
    editor = await _bind(chat, card)
    await chat.send("chat", "reply")
    await _settle()
    moved_to = editor._message_id
    ac.on_child_event("subagent.tool", delegation_id=DID, tool_name="read_file", preview="x.py",
                      args={"path": "x.py"}, tool_count=1)
    await _settle()
    assert chat.edits and chat.edits[-1]["id"] == moved_to
    assert len(chat.sends) == 3, "its own send/edits never trigger another move"
    assert len(chat.deleted) == 1


@pytest.mark.asyncio
async def test_its_own_edits_alone_never_repost():
    chat, card = _Chat(), _card()
    await _bind(chat, card)
    for i in range(3):
        ac.on_child_event("subagent.tool", delegation_id=DID, tool_name="read_file", preview=f"f{i}.py",
                          args={"path": f"f{i}.py"}, tool_count=i + 1)
        await _settle()
    assert len(chat.sends) == 1 and chat.deleted == []


@pytest.mark.asyncio
async def test_a_finished_card_never_moves():
    chat, card = _Chat(), _card()
    editor = await _bind(chat, card)
    first = editor._message_id
    ac.on_child_event("subagent.complete", delegation_id=DID, status="completed", summary="Plain summary: ok.")
    await _settle()
    await chat.send("chat", "the parent's reply")
    await _settle()
    assert len(chat.sends) == 2, "ack + reply only"
    assert chat.deleted == [] and editor._message_id == first
    assert await editor.repost_live_cards() == 0


@pytest.mark.asyncio
async def test_a_card_that_finishes_before_the_debounce_stays_put(monkeypatch):
    monkeypatch.setattr(da, "FOLLOW_DEBOUNCE_SECONDS", 0.05)
    chat, card = _Chat(), _card()
    editor = await _bind(chat, card)
    await chat.send("chat", "reply")
    ac.on_child_event("subagent.complete", delegation_id=DID, status="completed", summary="Plain summary: ok.")
    await asyncio.sleep(0.2)
    await _settle()
    assert len(chat.sends) == 2 and chat.deleted == []
    assert editor._message_id == "101"


@pytest.mark.asyncio
async def test_reposts_are_throttled():
    now = [1000.0]
    chat, card = _Chat(), _card()
    editor = await _bind(chat, card, clock=lambda: now[0])
    await chat.send("chat", "reply 1")
    await _settle()
    assert len(chat.sends) == 3
    await chat.send("chat", "reply 2")
    await _settle()
    assert len(chat.sends) == 4, "second reply sent, but the card has not moved again yet"
    assert editor._repost_timer is not None, "a single deferred repost is pending"
    now[0] += da.ACK_REPOST_MIN_SECONDS + 0.1
    assert await editor.repost_live_cards() == 1
    assert len(chat.sends) == 5 and chat.sends[-1]["text"] == card.render()
    assert chat.deleted == ["101", "103"]


@pytest.mark.asyncio
async def test_no_delete_support_keeps_editing_in_place():
    chat, card = _NoDeleteChat(), _card()
    editor = await _bind(chat, card)
    await chat.send("chat", "reply")
    await _settle()
    assert len(chat.sends) == 2 and editor._message_id == "101"
    ac.on_child_event("subagent.tool", delegation_id=DID, tool_name="read_file", preview="x.py",
                      args={"path": "x.py"}, tool_count=1)
    await _settle()
    assert chat.edits and chat.edits[-1]["id"] == "101"


@pytest.mark.asyncio
async def test_classic_style_never_follows():
    ac.set_style_for_tests("classic")
    chat, card = _Chat(), _card()
    editor = await _bind(chat, card)
    await chat.send("chat", "reply")
    await _settle()
    assert len(chat.sends) == 2 and chat.deleted == [] and editor._message_id == "101"


@pytest.mark.asyncio
async def test_failed_repost_send_keeps_the_old_card():
    chat, card = _Chat(), _card()
    editor = await _bind(chat, card)
    orig = chat.send

    async def failing(chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=False, error="boom")

    await chat.send("chat", "reply")  # triggers a normal repost first
    await _settle()
    chat.send = failing  # type: ignore[assignment]
    editor._last_repost = float("-inf")
    da._LAST_OUT[da._inbox_key(chat, "chat")] = "999"
    assert await editor.repost_live_cards() == 0
    assert editor._message_id == "103" and chat.deleted == ["101"]
    del orig


@pytest.mark.asyncio
async def test_two_running_cards_only_the_newest_follows_and_nothing_loops():
    chat = _Chat()
    card_a = _card("deleg_a", "\U0001f504 first job \u2192 result A, ~5 min")
    editor_a = await _bind(chat, card_a)
    card_b = _card("deleg_b", "\U0001f504 second job \u2192 result B, ~5 min")
    editor_b = await _bind(chat, card_b)
    a_id, b_id = editor_a._message_id, editor_b._message_id
    assert da._LAST_PLAIN == {}, "another helper card's send is not 'something newer'"
    await _settle()
    assert len(chat.sends) == 2 and chat.deleted == []

    await chat.send("chat", "the parent's reply")
    await _settle()
    # exactly one re-post: the newest card; the older one stays put
    assert len(chat.sends) == 4, [s["text"][:20] for s in chat.sends]
    assert chat.deleted == [b_id]
    assert editor_a._message_id == a_id and editor_b._message_id != b_id
    assert chat.sends[-1]["text"] == card_b.render()

    # nothing else moves afterwards, however long we wait or tick
    for _ in range(3):
        await _settle()
        await editor_a.repost_live_cards()
        await editor_b.repost_live_cards()
    assert len(chat.sends) == 4 and chat.deleted == [b_id]

    # the older card keeps editing in place
    ac.on_child_event("subagent.tool", delegation_id="deleg_a", tool_name="read_file", preview="x.py",
                      args={"path": "x.py"}, tool_count=1)
    await _settle()
    assert chat.edits and chat.edits[-1]["id"] == a_id
    assert len(chat.sends) == 4


@pytest.mark.asyncio
async def test_when_the_newest_card_finishes_the_older_one_takes_over():
    chat = _Chat()
    card_a = _card("deleg_a", "\U0001f504 first job \u2192 result A, ~5 min")
    editor_a = await _bind(chat, card_a)
    card_b = _card("deleg_b", "\U0001f504 second job \u2192 result B, ~5 min")
    editor_b = await _bind(chat, card_b)
    ac.on_child_event("subagent.complete", delegation_id="deleg_b", status="completed", summary="Plain summary: ok.")
    await _settle()
    a_id = editor_a._message_id
    await chat.send("chat", "reply")
    await _settle()
    assert editor_a._message_id != a_id and chat.deleted == [a_id]
    assert editor_b._message_id == "102", "the finished card never moves"


@pytest.mark.asyncio
async def test_the_roster_card_sends_never_count_as_newer():
    """The 🔀 roster/board posts under _CARD_SEND: ignored by note_outbound."""
    chat = _Chat()
    editor = await _bind(chat, _card())
    token = da._CARD_SEND.set(True)
    try:
        await chat.send("chat", "\U0001f500 roster board")
    finally:
        da._CARD_SEND.reset(token)
    await _settle()
    assert len(chat.sends) == 2 and chat.deleted == [] and editor._message_id == "101"
