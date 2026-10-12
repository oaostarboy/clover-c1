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


def _card(did=DID, head=HEAD, say=""):
    r = fl.DispatchReceipt(
        delegation_id=did, seq=1, goals=("g",), summary=head, steps=("find", "fix"), say=say,
    )
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


# -- the opener line is its own message; only the card follows ----------------

SAY = "on it. send me a message while it runs"


@pytest.mark.asyncio
async def test_card_render_without_say_and_say_text():
    card = _card(say=SAY)
    assert card.initial_text.startswith(SAY + "\n\n")
    assert card.say_text() == SAY
    assert SAY not in card.render(include_say=False)
    assert card.render(include_say=False) == card.initial_text[len(SAY) + 2:]
    card.split_opener = True
    assert card.render() == card.render(include_say=False), "a split card never holds the opener"
    assert _card("deleg_x").say_text() == ""


async def _bind_split(chat, card, clock=time.monotonic):
    """The way the gateway does it: the opener is the reply, the card goes under it."""
    card.split_opener = True
    sent = await chat.send("chat", card.say_text())
    pending = da.bind_ack_card(chat, "chat", card.say_text(), sent)
    assert pending is not None, "the opener reply makes the gateway send the card"
    await pending
    return sent.message_id, da._ACK_EDITORS[id(card)]


@pytest.mark.asyncio
async def test_opener_and_card_are_two_messages_from_the_start():
    chat, card = _Chat(), _card(say=SAY)
    opener_id, editor = await _bind_split(chat, card)
    assert [s["text"] for s in chat.sends][0] == SAY
    assert len(chat.sends) == 2 and SAY not in chat.sends[1]["text"]
    assert chat.sends[1]["text"] == editor._render()
    assert editor._message_id == chat.sends[1]["id"] != opener_id
    assert card.target[2] == editor._message_id
    assert chat.edits == [] and chat.deleted == []


@pytest.mark.asyncio
async def test_the_opener_is_never_edited_moved_or_deleted():
    now = [1000.0]
    chat, card = _Chat(), _card(say=SAY)
    opener_id, editor = await _bind_split(chat, card, clock=lambda: now[0])
    editor._clock = lambda: now[0]
    for n in range(2):
        await chat.send("chat", f"reply {n}")
        await _settle()
        now[0] += da.ACK_REPOST_MIN_SECONDS + 0.1
    ac.on_child_event("subagent.tool", delegation_id=DID, tool_name="read_file", preview="x.py",
                      args={"path": "x.py"}, tool_count=1)
    await _settle()
    assert all(e["id"] != opener_id for e in chat.edits)
    assert opener_id not in chat.deleted
    assert editor._message_id == chat.sends[-1]["id"]


@pytest.mark.asyncio
async def test_follow_moves_only_the_card_message():
    now = [1000.0]
    chat, card = _Chat(), _card(say=SAY)
    opener_id, editor = await _bind_split(chat, card, clock=lambda: now[0])
    editor._clock = lambda: now[0]
    card_one = editor._message_id
    await chat.send("chat", "the parent's reply")
    await _settle()
    assert chat.deleted == [card_one], "only the old card message goes"
    assert chat.sends[-1]["text"] == editor._render() and SAY not in chat.sends[-1]["text"]
    card_two = editor._message_id
    now[0] += da.ACK_REPOST_MIN_SECONDS + 0.1
    await chat.send("chat", "reply 2")
    await _settle()
    assert chat.deleted == [card_one, card_two]
    assert opener_id not in chat.deleted and not [e for e in chat.edits if e["id"] == opener_id]


@pytest.mark.asyncio
async def test_without_a_say_line_the_whole_message_moves_as_before():
    chat, card = _Chat(), _card()
    editor = await _bind(chat, card)
    first = editor._message_id
    await chat.send("chat", "reply")
    await _settle()
    assert chat.sends[-1]["text"] == card.render()
    assert chat.deleted == [first] and chat.edits == []
    assert da.bind_ack_card(chat, "chat", card.initial_text, SendResult(success=True, message_id="x")) is None


@pytest.mark.asyncio
async def test_opener_match_is_exact_and_claims_the_card_once():
    chat, card = _Chat(), _card(say=SAY)
    card.split_opener = True
    other = SendResult(success=True, message_id="5")
    assert da.bind_ack_card(chat, "chat", SAY + " and then a lot more words", other) is None
    assert da.bind_ack_card(chat, "chat", "unrelated", other) is None
    first = da.bind_ack_card(chat, "chat", SAY + " \u258c", other)
    assert first is not None
    first.close()
    assert da.bind_ack_card(chat, "chat", SAY, other) is None, "already claimed"


@pytest.mark.asyncio
async def test_a_failed_card_send_leaves_the_opener_alone_and_does_not_raise():
    chat, card = _Chat(), _card(say=SAY)
    card.split_opener = True
    sent = await chat.send("chat", SAY)

    async def failing_send(chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=False, error="boom")

    chat.send = failing_send  # type: ignore[assignment]
    await da.bind_ack_card(chat, "chat", SAY, sent)
    assert card.target is None and id(card) not in da._ACK_EDITORS


@pytest.mark.asyncio
async def test_real_adapter_send_wrapper_sends_opener_then_card():
    from gateway.platforms.base import BasePlatformAdapter

    log: List[Any] = []

    class Sub(BasePlatformAdapter):
        async def connect(self): return True
        async def disconnect(self): return None
        async def get_chat_info(self, chat_id): return {}
        async def send_typing(self, *a, **k): return None
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            log.append(content)
            return SendResult(success=True, message_id=f"m{len(log)}")

    card = _card(say=SAY)
    card.split_opener = True
    adapter = Sub.__new__(Sub)
    res = await adapter.send("chat", content=card.say_text())
    assert res.message_id == "m1", "the caller's result is the opener's"
    assert log[0] == SAY and len(log) == 2 and SAY not in log[1]
    assert card.target[2] == "m2" and da._ACK_EDITORS[id(card)]._message_id == "m2"


# -- a new dispatch JOINS the running card of its chat ----------------------


class _TgChat(_Chat):
    """A Telegram-shaped chat: the card goes out as the expandable quote."""

    def __init__(self) -> None:
        from types import SimpleNamespace

        super().__init__()
        self.platform = SimpleNamespace(value="telegram")


def _opener_card(did, work, outcome, say, goals=("g",), steps=("find", "fix"), eta="~5 min"):
    head = f"\U0001f504 {work} \u2192 {outcome}, {eta}" if outcome else f"\U0001f504 {work}"
    r = fl.DispatchReceipt(delegation_id=did, seq=1, goals=tuple(goals), summary=head,
                           steps=tuple(steps) if len(goals) == 1 else (), say=say)
    card = ac.card_for([r])
    card.split_opener = True
    return card


async def _dispatch(chat, card, adapter_chat_id="chat"):
    """What the real send wrapper does for a turn's reply: the opener goes out,
    then bind_ack_card sends (or joins) the card."""
    opener = card.say_text()
    sent = await chat.send(adapter_chat_id, opener)
    pending = da.bind_ack_card(chat, adapter_chat_id, opener, sent)
    if pending is not None:
        await pending
    await _settle()
    return sent


@pytest.mark.asyncio
async def test_a_second_dispatch_joins_the_running_card_instead_of_getting_its_own():
    chat = _TgChat()
    first = _opener_card("dj1", "checking the disks", "a disk report", "on it, disks first")
    await _dispatch(chat, first)
    assert [s["text"][:3] for s in chat.sends] == ["on ", "**>"], "opener, then ONE card message"
    card_id = da._ACK_EDITORS[id(first)]._message_id
    second = _opener_card("dj2", "checking the network", "a network report", "and the network too")
    await _dispatch(chat, second)
    texts = [s["text"] for s in chat.sends]
    assert "and the network too" in texts, "its opener went out as a normal message"
    assert len(da._ACK_EDITORS) == 1 and id(second) not in da._ACK_EDITORS, "no second editor"
    assert second.parts[0].card is first and len(first.parts) == 2
    # the card was moved under the new opener (old message deleted, re-posted at the bottom)
    last = chat.sends[-1]
    assert last["text"].startswith("**> \U0001f500 **2 jobs** \u00b7 0 of 2 done")
    assert chat.deleted == [card_id] and da._ACK_EDITORS[id(first)]._message_id == last["id"]
    assert [i for i, t in enumerate(texts) if t == "and the network too"][0] < len(texts) - 1
    assert last["text"].count("Goal: ") == 2 and "\n> Goal: " not in last["text"], "goals sit in their sections"
    assert "\u2003\u2003Goal: a disk report\n" in last["text"] and "\u2003\u2003Goal: a network report\n" in last["text"]
    assert "~5 min" not in last["text"], "no ETA inside a joined card"
    # a card message was sent exactly twice in total: the first one and its repost
    assert len([t for t in texts if t.startswith("**>")]) == 2


@pytest.mark.asyncio
async def test_events_of_the_joined_part_edit_the_one_card_and_counts_span_all_parts():
    chat = _TgChat()
    first = _opener_card("dj1", "disks", "a disk report", "on it")
    await _dispatch(chat, first)
    second = _opener_card("dj2", "network", "a network report", "and this")
    await _dispatch(chat, second)
    editor = da._ACK_EDITORS[id(first)]
    editor._min_interval = 0.0
    editor._last_attempt = float("-inf")
    if editor._task is not None:
        editor._task.cancel()  # it is waiting out the real 4 s gate
        await _settle()
        editor._task = None
    for did, n in (("dj1", 3), ("dj2", 4)):
        ac.on_child_event("subagent.start", delegation_id=did, model="claude-sonnet-5-5")
        ac.on_child_event("subagent.tool", delegation_id=did, tool_name="terminal", preview="ls",
                          args={"command": "ls"}, tool_count=n)
    await _settle()
    text = chat.edits[-1]["text"]
    assert chat.edits[-1]["id"] == editor._message_id
    assert "**disks** \u00b7 Sonnet 5.5 \u00b7 \U0001f6e0 3 calls" in text
    assert "**network** \u00b7 Sonnet 5.5 \u00b7 \U0001f6e0 4 calls" in text
    assert "*2 running \u00b7 \U0001f6e0 7 calls \u00b7" in text
    ac.on_child_event("subagent.complete", delegation_id="dj1", status="completed", summary="Plain summary: Disks fine.")
    await _settle()
    mid = chat.edits[-1]["text"]
    # the ended job LEFT the card: the card is the single layout of the job still running
    assert mid.splitlines()[0].startswith("**> \U0001f500 **network**") and "tap to watch" in mid
    assert "Goal: a network report" in mid and "disks" not in mid and "Disks fine." not in mid
    ac.on_child_event("subagent.complete", delegation_id="dj2", status="completed", summary="Plain summary: Net fine.")
    await _settle()
    done = chat.edits[-1]["text"]
    assert done.startswith("\U0001f340 **network** \u00b7 done") and first.finished
    assert "**>" not in done and "||" not in done and "tap to" not in done and "\n>" not in done, "one plain line"


@pytest.mark.asyncio
async def test_the_card_stays_live_until_every_part_has_ended():
    chat = _TgChat()
    first = _opener_card("dj1", "disks", "a disk report", "on it")
    await _dispatch(chat, first)
    second = _opener_card("dj2", "network", "a network report", "and this")
    await _dispatch(chat, second)
    ac.on_child_event("subagent.complete", delegation_id="dj1", status="completed", summary="Plain summary: ok.")
    assert first.finished is False
    ac.on_child_event("subagent.complete", delegation_id="dj2", status="completed", summary="Plain summary: ok.")
    assert first.finished is True


@pytest.mark.asyncio
async def test_a_third_part_titles_the_card_n_jobs_not_cut_off_names():
    chat = _TgChat()
    cards = [
        _opener_card(f"dj{i}", w, f"result {i}", f"opener {i}")
        for i, w in enumerate(("disks", "network", "backups", "logs"), 1)
    ]
    for c in cards:
        await _dispatch(chat, c)
    assert len({id(c.parts[0].card) for c in cards}) == 1
    text = da._ACK_EDITORS[id(cards[0])]._render()
    assert text.splitlines()[0] == "**> \U0001f500 **4 jobs** \u00b7 0 of 4 done"
    assert "more" not in text.splitlines()[0] and "disks" not in text.splitlines()[0]
    for w in ("disks", "network", "backups", "logs"):
        assert f"\u25a0 **{w}**" in text, "each job stays named in its section"
    assert text.count("Goal: ") == 4
    third = _opener_card("dj9", "x", "y", "z")
    assert da._running_editor_for(chat, "chat", third) is not None


@pytest.mark.asyncio
async def test_a_dispatch_never_joins_a_finished_card():
    chat = _TgChat()
    first = _opener_card("dj1", "disks", "a disk report", "on it")
    await _dispatch(chat, first)
    ac.on_child_event("subagent.complete", delegation_id="dj1", status="completed", summary="Plain summary: ok.")
    await _settle()
    second = _opener_card("dj2", "network", "a network report", "and this")
    await _dispatch(chat, second)
    assert second.parts[0].card is second and len(first.parts) == 1
    assert len(da._ACK_EDITORS) == 2, "the new dispatch got its own card"
    cards = [s["text"] for s in chat.sends if s["text"].startswith("**>")]
    assert len(cards) == 2 and cards[-1].splitlines()[0] == "**> \U0001f500 **network**"
    finished = da._ACK_EDITORS[id(first)]._render()
    assert finished.startswith("\U0001f340 **disks** \u00b7 done") and "tap to" not in finished and "||" not in finished


@pytest.mark.asyncio
async def test_a_single_card_per_chat_but_other_chats_are_separate():
    chat = _TgChat()
    first = _opener_card("dj1", "disks", "a disk report", "on it")
    await _dispatch(chat, first, "chat")
    other = _opener_card("dj2", "network", "a network report", "and this")
    await _dispatch(chat, other, "other-chat")
    assert other.parts[0].card is other and len(da._ACK_EDITORS) == 2


@pytest.mark.asyncio
async def test_the_opener_only_message_still_matches_for_a_first_dispatch():
    chat = _TgChat()
    first = _opener_card("dj1", "disks", "a disk report", "on it")
    sent = await chat.send("chat", first.say_text())
    pending = da.bind_ack_card(chat, "chat", first.say_text(), sent)
    assert pending is not None, "the first dispatch still sends its own card message"
    await pending
    await _settle()
    assert id(first) in da._ACK_EDITORS


@pytest.mark.asyncio
async def test_a_joined_multi_subagent_part_keeps_its_own_sections():
    chat = _TgChat()
    first = _opener_card("dj1", "disks", "a disk report", "on it")
    await _dispatch(chat, first)
    multi = _opener_card("dj2", "the sweep", "all clear", "sweeping", goals=("alpha", "beta"))
    await _dispatch(chat, multi)
    for i in (0, 1):
        ac.on_child_event("subagent.start", delegation_id="dj2", task_index=i, task_count=2, title=("alpha", "beta")[i])
    await _settle()
    text = da._ACK_EDITORS[id(first)]._render()
    assert text.splitlines()[0].startswith("**> \U0001f500 **2 jobs** \u00b7 0 of 3 done")
    assert "> \u25a0 **disks**" in text and "> \u25a0 **alpha**" in text and "> \u25a0 **beta**" in text
    assert "*3 running" in text


# -- a finished card sits right above the reply it belongs to ---------------


class _Evt:
    """The shape of the internal event a completion turn runs for."""

    def __init__(self, ids) -> None:
        self.internal = True
        self.metadata = {"completion_delegation_ids": list(ids)}


async def _reply(chat, text, ids, *, silent=False):
    """What the gateway does for a completion turn: mark the turn, then either
    send the reply through the adapter's send wrapper (before_reply) or, for
    [SILENT], end the turn with nothing sent (after_silent_reply)."""
    da.begin_completion_turn(_Evt(ids))
    if silent:
        await da.after_silent_reply(chat, "chat")
    else:
        await da.before_reply(chat, "chat", text)
        await chat.send("chat", text)
    await _settle()
    da._COMPLETION_TURN.set(None)


def _order(chat):
    """Message texts in the order they went out, with the card message ids."""
    return [s["text"] for s in chat.sends]


async def _ended_single(chat, did="dj1", status="completed", work="disks"):
    card = _opener_card(did, work, f"a {work} report", "on it")
    await _dispatch(chat, card)
    ac.on_child_event("subagent.start", delegation_id=did, model="claude-sonnet-5-5")
    ac.on_child_event("subagent.complete", delegation_id=did, status=status, summary="Plain summary: ok.")
    await _settle()
    return card


@pytest.mark.asyncio
async def test_the_finished_card_is_reposted_right_above_the_completion_reply():
    chat = _TgChat()
    card = await _ended_single(chat)
    await chat.send("chat", "something else, posted in between")
    editor = da._ACK_EDITORS[id(card)]
    old_id = editor._message_id
    n = len(chat.sends)
    await _reply(chat, "All done, here are the results.", ["dj1"])
    posted = _order(chat)[n:]
    assert len(posted) == 2, "the one-liner, then the reply"
    assert posted[0].startswith("\U0001f340 **disks** \u00b7 done") and "**>" not in posted[0]
    assert posted[1] == "All done, here are the results."
    assert old_id in chat.deleted and editor._message_id == chat.sends[n]["id"], "the old card message moved"


@pytest.mark.asyncio
async def test_a_card_that_is_already_the_last_message_does_not_move():
    chat = _TgChat()
    card = await _ended_single(chat)
    editor = da._ACK_EDITORS[id(card)]
    old_id, n = editor._message_id, len(chat.sends)
    await _reply(chat, "All done.", ["dj1"])
    assert chat.deleted == [] and editor._message_id == old_id
    assert _order(chat)[n:] == ["All done."], "no re-post: the card already sits right above the reply"


@pytest.mark.asyncio
async def test_a_silent_completion_moves_nothing_for_a_done_card_but_keeps_it():
    chat = _TgChat()
    card = await _ended_single(chat)
    await chat.send("chat", "later message")
    editor = da._ACK_EDITORS[id(card)]
    n = len(chat.sends)
    await _reply(chat, "", ["dj1"], silent=True)
    posted = _order(chat)[n:]
    assert len(posted) == 1 and posted[0].startswith("\U0001f340 **disks** \u00b7 done"), "not lost: posted anyway"
    assert editor._message_id == chat.sends[n]["id"]


@pytest.mark.asyncio
async def test_a_silent_completion_of_a_stopped_card_just_deletes_it():
    chat = _TgChat()
    card = await _ended_single(chat, status="interrupted")
    editor = da._ACK_EDITORS[id(card)]
    old_id, n = editor._message_id, len(chat.sends)
    await _reply(chat, "", ["dj1"], silent=True)
    assert old_id in chat.deleted and len(chat.sends) == n, "the stopped part disappears; nothing is posted"


@pytest.mark.asyncio
async def test_a_stopped_part_above_a_real_reply_keeps_its_stop_line():
    chat = _TgChat()
    card = await _ended_single(chat, status="interrupted")
    await chat.send("chat", "newer")
    n = len(chat.sends)
    await _reply(chat, "Stopped as asked.", ["dj1"])
    posted = _order(chat)[n:]
    assert posted[0].startswith("\U0001f940 **disks** \u00b7 stopped") and posted[1] == "Stopped as asked."


@pytest.mark.asyncio
async def test_each_part_moves_once_only():
    chat = _TgChat()
    await _ended_single(chat)
    await chat.send("chat", "newer")
    await _reply(chat, "first reply", ["dj1"])
    n = len(chat.sends)
    await _reply(chat, "second reply", ["dj1"])
    assert _order(chat)[n:] == ["second reply"], "already placed: no second move"


@pytest.mark.asyncio
async def test_an_ordinary_turn_never_moves_a_finished_card():
    chat = _TgChat()
    await _ended_single(chat)
    await chat.send("chat", "newer")
    n = len(chat.sends)
    da._COMPLETION_TURN.set(None)
    await da.before_reply(chat, "chat", "a normal human-turn reply")
    await chat.send("chat", "a normal human-turn reply")
    assert _order(chat)[n:] == ["a normal human-turn reply"]


@pytest.mark.asyncio
async def test_a_joined_part_posts_its_own_line_before_its_reply_and_the_last_part_moves_the_card():
    chat = _TgChat()
    first = _opener_card("dj1", "disks", "a disk report", "on it")
    await _dispatch(chat, first)
    second = _opener_card("dj2", "network", "a network report", "and this")
    await _dispatch(chat, second)
    editor = da._ACK_EDITORS[id(first)]
    for did in ("dj1", "dj2"):
        ac.on_child_event("subagent.start", delegation_id=did, model="claude-sonnet-5-5")
    ac.on_child_event("subagent.complete", delegation_id="dj1", status="completed", summary="Plain summary: ok.")
    await _settle()
    # part 1 ended while part 2 still runs: the live card dropped it
    assert "disks" not in first.render() and editor._closed is False
    n = len(chat.sends)
    await _reply(chat, "Disks are fine.", ["dj1"])
    posted = _order(chat)[n:]
    assert len(posted) == 2 and posted[0].startswith("\U0001f340 **disks** \u00b7 done"), "its own line, then the reply"
    assert posted[1] == "Disks are fine."
    assert not first.finished, "the card still runs for the other part"
    ac.on_child_event("subagent.complete", delegation_id="dj2", status="completed", summary="Plain summary: ok.")
    await _settle()
    await chat.send("chat", "chatter")
    n = len(chat.sends)
    await _reply(chat, "Network is fine too.", ["dj2"])
    posted = _order(chat)[n:]
    assert posted[0].startswith("\U0001f340 **network** \u00b7 done") and posted[1] == "Network is fine too."
    assert sum(1 for t in _order(chat) if t.startswith("\U0001f340 **disks**")) == 1, "disks line was posted once"


@pytest.mark.asyncio
async def test_a_batched_completion_for_several_parts_posts_each_line_before_the_one_reply():
    chat = _TgChat()
    first = _opener_card("dj1", "disks", "a disk report", "on it")
    await _dispatch(chat, first)
    second = _opener_card("dj2", "network", "a network report", "and this")
    await _dispatch(chat, second)
    for did in ("dj1", "dj2"):
        ac.on_child_event("subagent.start", delegation_id=did, model="claude-sonnet-5-5")
        ac.on_child_event("subagent.complete", delegation_id=did, status="completed", summary="Plain summary: ok.")
    await _settle()
    await chat.send("chat", "chatter")
    n = len(chat.sends)
    await _reply(chat, "Both are fine.", ["dj1", "dj2"])
    posted = _order(chat)[n:]
    assert [p.split(" ")[1] for p in posted[:2]] == ["**disks**", "**network**"] and posted[2] == "Both are fine."


@pytest.mark.asyncio
async def test_the_real_send_wrapper_moves_the_card_before_the_reply_goes_out():
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter

    class _Real(BasePlatformAdapter):
        def __init__(self) -> None:
            super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)
            self.n = 100
            self.sent: List[str] = []
            self.deleted: List[str] = []

        async def connect(self): return True
        async def disconnect(self): pass
        async def send_typing(self, *a, **k): pass
        async def get_chat_info(self, chat_id): return {}

        async def send(self, chat_id, content, reply_to=None, metadata=None, **kw):
            self.n += 1
            self.sent.append(content)
            return SendResult(success=True, message_id=str(self.n))

        async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
            return SendResult(success=True, message_id=message_id)

        async def delete_message(self, chat_id, message_id):
            self.deleted.append(str(message_id))
            return True

    chat = _Real()
    card = _opener_card("dj1", "disks", "a disk report", "on it")
    sent = await chat.send("chat", card.say_text())
    await _settle()
    ac.on_child_event("subagent.start", delegation_id="dj1", model="claude-sonnet-5-5")
    ac.on_child_event("subagent.complete", delegation_id="dj1", status="completed", summary="Plain summary: ok.")
    await _settle()
    await chat.send("chat", "chatter in between")
    n = len(chat.sent)
    da.begin_completion_turn(_Evt(["dj1"]))
    await chat.send("chat", "The disks are fine.")
    await _settle()
    posted = chat.sent[n:]
    assert posted[0].startswith("\U0001f340 **disks** \u00b7 done") and posted[-1] == "The disks are fine.", posted
    da._COMPLETION_TURN.set(None)
