"""The finished subagent card goes under the reply of the turn that reports it,
also when the completion arrived while the agent was busy and ran as the
in-band queued follow-up of that turn (``_run_agent`` recursing for the queued
internal event) instead of as a turn of its own.

Replay: human turn A running -> completion queued -> A's reply sent -> the
follow-up completion turn's reply sent => the finished card is re-posted right
after the FOLLOW-UP's reply, not after A's.
"""

from __future__ import annotations

import pytest

from gateway import delegation_activity as da
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from tests.gateway import test_ack_card_follow as ack
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter

pytestmark = pytest.mark.unified_card

CHAT = "chat"


class _CardChat(ProgressCaptureAdapter):
    """Sequential message ids, delete + finalize-aware edit, like a real chat."""

    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(platform=platform)
        self.n = 100
        self.deleted = []
        self.texts = []  # every message in send order: (id, text)

    async def send(self, chat_id, content, reply_to=None, metadata=None, **kw):
        self.n += 1
        mid = str(self.n)
        self.sent.append({"chat_id": chat_id, "content": content, "reply_to": reply_to, "metadata": metadata})
        self.texts.append((mid, content))
        return SendResult(success=True, message_id=mid)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "content": content})
        self.texts = [(i, content if i == str(message_id) else t) for i, t in self.texts]
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(str(message_id))
        self.texts = [(i, t) for i, t in self.texts if i != str(message_id)]
        return True

    def live(self):
        return [t for _, t in self.texts]


def _is_card(t: str) -> bool:
    return t.startswith("> \U0001f340 **disks** \u00b7 done")


class _TwoTurnAgent:
    """Turn 1 is the human turn A, turn 2 the queued follow-up."""

    calls = 0
    second = "both done in about 30 seconds"

    def __init__(self, **kwargs):
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None, **kw):
        type(self).calls += 1
        return {
            "final_response": "on it" if type(self).calls == 1 else type(self).second,
            "messages": [],
            "api_calls": 1,
        }


def _completion_event(ids=("dj1",)):
    # chat_id/thread_id are filled in below (source has to match the chat).
    return MessageEvent(
        text="[IMPORTANT: a helper finished]",
        message_type=MessageType.TEXT,
        source=None,
        internal=True,
        metadata={"completion_delegation_ids": list(ids)},
    )


def _human_event():
    return MessageEvent(text="and one more thing", message_type=MessageType.TEXT, source=None)


async def _setup(event):
    """A chat with a finished card, and ``event`` queued behind the running turn."""
    from gateway.session import SessionSource

    ack.ac.reset_for_tests()
    ack.ac.set_style_for_tests("unified")
    da._ACK_EDITORS.clear()
    da._LIVE.clear()
    da._LAST_OUT.clear()
    da._LAST_PLAIN.clear()
    chat = _CardChat()
    await ack._finished_card_on_real_chat(chat)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=CHAT, chat_type="dm", thread_id=None)
    event.source = source
    session_key = f"agent:main:telegram:dm:{CHAT}"
    chat._pending_messages[session_key] = event
    return chat, session_key, source


async def _drive(monkeypatch, tmp_path, event, *, second="both done in about 30 seconds", silent=False):
    _TwoTurnAgent.calls = 0
    _TwoTurnAgent.second = second
    chat, session_key, source = await _setup(event)
    import importlib
    import sys
    import types

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _TwoTurnAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    from tests.gateway.test_run_progress_topics import _make_runner

    gateway_run = importlib.import_module("gateway.run")
    runner = _make_runner(chat)
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    # The outer turn: what base.py does at turn start (a human message).
    da.begin_completion_turn(MessageEvent(text="Looks good.", message_type=MessageType.TEXT, source=source))
    result = await runner._run_agent(
        message="Looks good.", context_prompt="", history=[], source=source,
        session_id="sess-finished-card-queued", session_key=session_key,
    )
    # base.py then sends the final response of the whole chain in this task.
    final = result.get("final_response")
    if silent:
        await da.after_silent_reply(chat, CHAT)
    elif final:
        await chat.send(CHAT, final)
    await ack._settle()
    da._COMPLETION_TURN.set(None)
    return chat, result


@pytest.mark.asyncio
async def test_queued_completion_puts_the_finished_card_under_the_followups_reply(monkeypatch, tmp_path):
    chat, result = await _drive(monkeypatch, tmp_path, _completion_event())
    assert result["final_response"] == "both done in about 30 seconds"
    live = chat.live()
    assert "on it" in live and "both done in about 30 seconds" in live
    i_a, i_b = live.index("on it"), live.index("both done in about 30 seconds")
    cards = [i for i, t in enumerate(live) if _is_card(t)]
    assert cards == [i_b + 1], f"finished card right under the follow-up's reply, once: {live}"
    assert i_a < i_b


@pytest.mark.asyncio
async def test_queued_completion_card_is_not_fired_by_the_running_turns_reply(monkeypatch, tmp_path):
    chat, result = await _drive(monkeypatch, tmp_path, _completion_event())
    # Turn A's reply was delivered first: nothing was re-posted under it.
    texts = [t for _, t in chat.texts]
    a = texts.index("on it")
    assert not _is_card(texts[a + 1]), "A's reply must not carry the follow-up's card"


@pytest.mark.asyncio
async def test_silent_queued_completion_still_posts_the_done_line(monkeypatch, tmp_path):
    chat, _ = await _drive(monkeypatch, tmp_path, _completion_event(), second="[SILENT]", silent=True)
    live = chat.live()
    assert sum(1 for t in live if _is_card(t)) == 1 and _is_card(live[-1]), live


@pytest.mark.asyncio
async def test_a_human_queued_followup_never_reposts_the_card(monkeypatch, tmp_path):
    chat, _ = await _drive(monkeypatch, tmp_path, _human_event())
    live = chat.live()
    assert live.index("on it") < live.index("both done in about 30 seconds")
    assert "disks" in live[1], "the card message stays where it was, above both replies"
    assert sum(1 for t in live if "disks" in t) == 1 and chat.deleted == [], "nothing was moved or re-posted"


@pytest.mark.asyncio
async def test_hand_over_settles_an_unfired_outer_completion_turn_silently():
    ack.ac.reset_for_tests()
    ack.ac.set_style_for_tests("unified")
    da._ACK_EDITORS.clear()
    chat = _CardChat()
    await ack._finished_card_on_real_chat(chat)
    await chat.send(CHAT, "something newer")
    da.begin_completion_turn(ack._Evt(["dj1"]))
    turn = await da.hand_over_completion_turn(chat, CHAT, None)
    assert turn is None and da._COMPLETION_TURN.get() is None, "a human follow-up is not a completion turn"
    assert _is_card(chat.live()[-1]), "the outer turn ended silent: its done line was posted"
    da._COMPLETION_TURN.set(None)
