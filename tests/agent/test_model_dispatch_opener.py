"""A delegate_task the MODEL calls with a ``handoff`` but no ``say`` still gets
the agent's own opener line, so the card joins a card already running in the
chat and the user always gets a reply.

Live bug (Suni deleg_12bec373 on GPT-6.1 Sol, Clover deleg_cc25abdc on Opus
5.5): the turn's reply was the card ALONE.  ``bind_ack_card`` bound it as a
brand-new card through ``match_unbound`` and never tried ``join`` (that only
runs for an opener line), so the chat got a second card and no text.

End to end: a real ``AIAgent.run_conversation`` (scripted local provider for
the root's model, faked router model), the real ``delegate_task`` handler and
child pool, the real ack checklist and the gateway's real ``bind_ack_card`` /
``join`` against a fake Telegram chat.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl
from gateway import delegation_activity as da
from gateway.platforms.base import SendResult
from tests.agent.test_delegation_router import aux, verdict_json  # noqa: F401
from tests.agent.test_foreground_time_limit_loop import (  # noqa: F401
    make_agent,
    provider,
    text,
    tool,
)
from tests.agent.test_router_backstop import _clean_registry, dispatches  # noqa: F401
from tests.agent.test_router_e2e import _unbind_session_context, children  # noqa: F401

pytestmark = [pytest.mark.real_delegation_router, pytest.mark.unified_card]

HANDOFF = {"work": "checking the router logs", "outcome": "why it went direct",
           "estimated_minutes_min": 5, "estimated_minutes_max": 10}
VOICE = "The 2 TB one is the better buy. A subagent is checking the router logs now."


class _TgChat:
    """A Telegram-shaped chat with sequential ids; sends are noted like the
    real adapter wrapper does."""

    def __init__(self) -> None:
        from types import SimpleNamespace

        self.n = 100
        self.sends: list = []
        self.edits: list = []
        self.deleted: list = []
        self.platform = SimpleNamespace(value="telegram")

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


@pytest.fixture(autouse=True)
def _unified(monkeypatch):
    ac.reset_for_tests()
    ac.set_style_for_tests("unified")
    for d in (da._ACK_EDITORS, da._LIVE, da._LAST_OUT, da._FOLLOW_TIMERS, da._LAST_PLAIN):
        d.clear()
    monkeypatch.setattr(da, "FOLLOW_DEBOUNCE_SECONDS", 0.0)
    yield
    ac.set_style_for_tests(None)
    ac.reset_for_tests()
    for d in (da._ACK_EDITORS, da._LIVE, da._LAST_OUT, da._FOLLOW_TIMERS, da._LAST_PLAIN):
        d.clear()


async def _settle(n: int = 60):
    for _ in range(n):
        await asyncio.sleep(0)


async def _deliver(chat, content):
    """What the gateway's send wrapper does with a turn's reply."""
    sent = await chat.send("chat", content)
    pending = da.bind_ack_card(chat, "chat", content, sent)
    if pending is not None:
        await pending
    await _settle()
    return sent


async def _running_card(chat):
    """A card already running in the chat (an earlier dispatch with an opener)."""
    r = fl.DispatchReceipt(delegation_id="deleg_running1", seq=1, goals=("g",),
                           summary="\U0001f504 checking the disks \u2192 a disk report, ~5 min",
                           steps=("find", "fix"), say="on it, disks first")
    host = ac.card_for([r])
    host.split_opener = True
    await _deliver(chat, host.say_text())
    assert id(host) in da._ACK_EDITORS, "the first card is running in the chat"
    return host


def _model_dispatch_turn(provider, aux, *, say=None, message="ok, check the router logs",
                         voice=VOICE, spoken=""):
    """One real turn: the model calls delegate_task itself (handoff, no say
    unless given), then the runtime asks the model for its own line."""
    handoff = dict(HANDOFF)
    if say:
        handoff["say"] = say
    aux.replies[:] = [verdict_json("direct", reason="one quick dispatch")]
    call = tool("delegate_task", {
        "tasks": [{"goal": "Read the router log lines for the last turn and say why it was direct",
                   "context": "read-only"}],
        "handoff": handoff,
    }, "d1")
    if spoken:
        call["choices"][0]["message"]["content"] = spoken
    provider.queue[:] = [call, text(voice)]
    agent = make_agent(provider, session_id=f"model-opener-{time.monotonic_ns()}")
    result = agent.run_conversation(message, conversation_history=[], task_id="mo")
    return agent, result


@pytest.mark.asyncio
async def test_model_dispatch_without_say_gets_an_opener_and_joins_the_running_card(
        provider, aux, children, dispatches):
    chat = _TgChat()
    host = await _running_card(chat)
    card_messages_before = len([s for s in chat.sends if s["text"].startswith("**>")])

    agent, result = await asyncio.to_thread(_model_dispatch_turn, provider, aux)

    receipts = fl.own_dispatch_receipts(agent)
    assert len(receipts) == 1 and receipts[0].summary and not receipts[0].say, \
        "the model's own dispatch, with a handoff and no say"
    reply = result["final_response"]
    assert reply == VOICE, "the turn's reply is the agent's own line, not the card"
    assert "\U0001f504" not in reply
    assert result["turn_exit_reason"] == "delegation_handoff"
    # the tools-off opener turn was a real model call on the same transcript
    assert len(provider.captured) == 2
    assert "A subagent is now handling this" in json.dumps(provider.captured[1]["messages"][-1])

    await _deliver(chat, reply)
    texts = [s["text"] for s in chat.sends]
    assert VOICE in texts, "the opener went out as its own message"
    assert len(da._ACK_EDITORS) == 1, "no second card editor: it joined"
    assert len(host.parts) == 2, "the new dispatch is a part of the running card"
    assert any(p.delegation_id != "deleg_running1" for p in host.parts)
    cards_after = [t for t in texts if t.startswith("**>")]
    # the running card moved under the new opener (one repost), no new card
    assert len(cards_after) == card_messages_before + 1
    assert cards_after[-1].startswith("**> \U0001f500 **2 subagents**")
    assert texts.index(VOICE) < len(texts) - 1


@pytest.mark.asyncio
async def test_model_dispatch_without_say_and_no_running_card_gets_opener_then_one_card(
        provider, aux, children, dispatches):
    chat = _TgChat()
    agent, result = await asyncio.to_thread(_model_dispatch_turn, provider, aux)
    reply = result["final_response"]
    assert reply == VOICE

    await _deliver(chat, reply)
    texts = [s["text"] for s in chat.sends]
    assert texts[0] == VOICE, "the opener first"
    assert len(texts) == 2 and texts[1].startswith("**>"), "then exactly one card"
    assert "checking the router logs" in texts[1]
    assert VOICE not in texts[1], "the card is its own message"
    assert len(da._ACK_EDITORS) == 1


@pytest.mark.asyncio
async def test_a_model_provided_say_is_the_only_opener(provider, aux, children, dispatches):
    chat = _TgChat()
    host = await _running_card(chat)
    say = "Yes, the 2 TB one. Checking the router logs now."

    agent, result = await asyncio.to_thread(_model_dispatch_turn, provider, aux, say=say)

    assert result["final_response"] == say
    assert len(provider.captured) == 1, "no extra opener model call"
    await _deliver(chat, result["final_response"])
    texts = [s["text"] for s in chat.sends]
    assert texts.count(say) == 1 and VOICE not in texts, "never a second opener"
    assert len(host.parts) == 2, "and it joined the running card"


def test_model_text_beside_the_dispatch_is_not_repeated(provider, aux, children, dispatches):
    """The model already wrote its own words in the delegate_task message:
    those went out as a message; the runtime does not add a second line."""
    agent, result = _model_dispatch_turn(provider, aux, spoken="Sure, checking that now.")
    assert len(provider.captured) == 1, "no opener model call"
    assert VOICE not in result["final_response"]


def test_cli_turn_keeps_the_whole_card_text(provider, aux, children, dispatches):
    """A CLI has no second message to edit: no opener call, the card is the reply."""
    aux.replies[:] = [verdict_json("direct")]
    provider.queue[:] = [tool("delegate_task", {
        "tasks": [{"goal": "Read the router log lines", "context": "read-only"}],
        "handoff": dict(HANDOFF)}, "d1"), text(VOICE)]
    agent = make_agent(provider, platform="cli", session_id="model-opener-cli")
    result = agent.run_conversation("check the logs", conversation_history=[], task_id="mo-cli")
    assert len(provider.captured) == 1
    assert "checking the router logs" in result["final_response"]


def test_a_failed_opener_call_falls_back_fast_to_on_it(provider, aux, children, dispatches, monkeypatch):
    """The opener call is bounded: a slow model gives ``on it`` and the card
    still binds under it."""
    from agent import handoff_notes as hn

    monkeypatch.setattr(hn, "MODEL_DISPATCH_VOICE_TIMEOUT_SECONDS", 0.3)

    def slow(n, req):
        if n == 2:
            time.sleep(1.5)

    provider.before = slow
    started = time.monotonic()
    agent, result = _model_dispatch_turn(provider, aux)
    assert result["final_response"] == hn.FALLBACK_LINE
    assert time.monotonic() - started < 8
