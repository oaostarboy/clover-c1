"""A delegate_task the MODEL calls with NO ``handoff`` at all (tasks only, no
``say``) still gets the agent's own opener line -- one real voice-line call
that reads the user's message -- never a canned "on it" string.

Live bug (Suni, GPT-6.1 Sol, 17794652): "Also use a subagent to list every
file in ~/Projects/gopi bigger than 5 MB. Btw, what model are you on right
now?"  The model dispatched ``tasks:[{goal, title}]`` with no handoff;
``_voice`` needed a handoff summary, so ``_ack_text`` fell to
``_fallback_ack``'s canned line and the question was dropped until the job
finished.  The canned lines are now only the floor when the voice-line call
fails or times out.

End to end: a real ``AIAgent.run_conversation`` (scripted local provider for
the root's model, faked router model), the real ``delegate_task`` handler and
child pool, the real ack checklist, the gateway's real publisher (which places
the card of a dispatch without a handoff) and ``bind_ack_card`` / ``join`` /
the follow-the-latest-message move, against a fake Telegram chat.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl
from agent import handoff_notes as hn
from agent import router_backstop as rb
from agent.delegation_activity import bind_activity_sink
from gateway import delegation_activity as da
from tests.agent.test_delegation_router import aux, verdict_json  # noqa: F401
from tests.agent.test_foreground_time_limit_loop import (  # noqa: F401
    make_agent,
    provider,
    text,
    tool,
)
from tests.agent.test_model_dispatch_opener import _TgChat, _deliver, _running_card, _settle
from tests.agent.test_router_backstop import _clean_registry, dispatches  # noqa: F401
from tests.agent.test_router_e2e import _unbind_session_context, children  # noqa: F401

pytestmark = [pytest.mark.real_delegation_router, pytest.mark.unified_card]

MESSAGE = ("Also use a subagent to list every file in ~/Projects/gopi bigger than 5 MB. "
           "Btw, what model are you on right now?")
TASK = {"goal": "List every file under ~/Projects/gopi larger than 5 MB with its size. Read-only.",
        "title": "Finding big files in gopi"}
VOICE = "I'm on GPT-6.1 Sol right now. A subagent is listing the big files in gopi."


@pytest.fixture(autouse=True)
def _unified(monkeypatch):
    ac.reset_for_tests()
    ac.set_style_for_tests("unified")
    for d in (da._ACK_EDITORS, da._LIVE, da._LAST_OUT, da._FOLLOW_TIMERS, da._LAST_PLAIN):
        d.clear()
    monkeypatch.setattr(da, "FOLLOW_DEBOUNCE_SECONDS", 0.0)
    monkeypatch.setattr(da, "ACK_REPOST_MIN_SECONDS", 0.0, raising=False)
    yield
    bind_activity_sink(None)
    ac.set_style_for_tests(None)
    ac.reset_for_tests()
    for d in (da._ACK_EDITORS, da._LIVE, da._LAST_OUT, da._FOLLOW_TIMERS, da._LAST_PLAIN):
        d.clear()


@pytest.fixture
def voice_calls(monkeypatch):
    """Every voice-line call, with what it was given (the real call runs)."""
    calls: list = []
    real = hn.voice_line

    def spy(agent, messages, title, *, message="", timeout=hn.VOICE_TIMEOUT_SECONDS):
        line = real(agent, messages, title, message=message, timeout=timeout)
        calls.append({"title": title, "message": message, "timeout": timeout, "line": line,
                      "transcript": json.dumps(list(messages or []), default=str)})
        return line

    monkeypatch.setattr(hn, "voice_line", spy)
    return calls


def _publisher(chat):
    from gateway.delegation_activity import DelegationActivityPublisher

    return DelegationActivityPublisher(
        adapter=chat, chat_id="chat", loop=asyncio.get_running_loop(),
        is_current=lambda: True, min_interval=0.0, auto_heartbeat=False, expandable=True,
    )


def _no_handoff_turn(provider, aux, *, voice=VOICE, spoken="", session=""):
    """One real turn: the model calls delegate_task with tasks only -- no
    handoff, no say -- then (chat platforms) one tools-off turn for its line."""
    aux.replies[:] = [verdict_json("direct", reason="one quick dispatch")]
    call = tool("delegate_task", {"tasks": [dict(TASK)]}, "d1")
    if spoken:
        call["choices"][0]["message"]["content"] = spoken
    provider.queue[:] = [call, text(voice)]
    agent = make_agent(provider, session_id=session or f"no-handoff-{time.monotonic_ns()}")
    result = agent.run_conversation(MESSAGE, conversation_history=[], task_id="nh")
    return agent, result


async def _run_turn(chat, provider, aux, **kw):
    """The turn runs in a worker thread with the chat's publisher bound, the
    way the gateway runs it; the card of a dispatch without a handoff is
    placed by that publisher."""
    pub = _publisher(chat)
    tok = bind_activity_sink(pub)
    try:
        agent, result = await asyncio.to_thread(_no_handoff_turn, provider, aux, **kw)
    finally:
        bind_activity_sink(None)
    await _settle()
    return pub, agent, result


def _visible(chat):
    """What the chat shows now: sends minus deleted messages, latest edit wins."""
    edits = {e["id"]: e["text"] for e in chat.edits}
    return [(s["id"], edits.get(s["id"], s["text"])) for s in chat.sends
            if s["id"] not in chat.deleted]


def _is_card(t: str) -> bool:
    return t.startswith("**>")


def _canned():
    return set(rb._FALLBACK_ONE) | {m.format(n=n) for m in rb._FALLBACK_MANY for n in range(2, 6)}


@pytest.mark.asyncio
async def test_no_handoff_question_and_running_card_opener_is_voice_line_and_joins(
        provider, aux, children, dispatches, voice_calls):
    chat = _TgChat()
    host = await _running_card(chat)

    pub, agent, result = await _run_turn(chat, provider, aux)

    receipts = fl.own_dispatch_receipts(agent)
    assert len(receipts) == 1 and not receipts[0].summary and not receipts[0].say, \
        "the model's own dispatch, with no handoff at all"
    # the opener came from ONE real voice-line call that read the message
    assert len(voice_calls) == 1
    call = voice_calls[0]
    assert call["message"] == MESSAGE
    assert "what model are you on" in call["transcript"]
    assert call["title"] == "Finding big files in gopi", "the task's title, never the raw goal"
    assert TASK["goal"] not in call["title"]
    assert call["timeout"] == hn.MODEL_DISPATCH_VOICE_TIMEOUT_SECONDS
    assert len(provider.captured) == 2
    assert "A subagent is now handling this" in json.dumps(provider.captured[1]["messages"][-1])
    assert "what model are you on" in json.dumps(provider.captured[1]["messages"])
    reply = result["final_response"]
    assert reply == VOICE and reply not in _canned()
    assert result["turn_exit_reason"] == "delegation_handoff"

    # the dispatch's card JOINED the running one (no second card)
    assert len(host.parts) == 2 and any(p.delegation_id != "deleg_running1" for p in host.parts)
    assert len(da._ACK_EDITORS) == 1

    await _deliver(chat, reply)
    await _settle(200)
    shown = _visible(chat)
    texts = [t for _, t in shown]
    assert texts.count(VOICE) == 1
    cards = [t for t in texts if _is_card(t)]
    assert len(cards) == 1, f"one card in the chat, got {texts}"
    assert "2 subagents" in cards[0]
    assert texts.index(VOICE) < texts.index(cards[0]), "the opener, then the card under it"
    assert not any(t in _canned() for t in texts)
    await pub.aclose()


@pytest.mark.asyncio
async def test_no_handoff_without_a_running_card_is_opener_and_one_card(
        provider, aux, children, dispatches, voice_calls):
    chat = _TgChat()
    pub, agent, result = await _run_turn(chat, provider, aux)
    reply = result["final_response"]
    assert reply == VOICE and len(voice_calls) == 1 and voice_calls[0]["message"] == MESSAGE

    await _deliver(chat, reply)
    await _settle(200)
    texts = [t for _, t in _visible(chat)]
    cards = [t for t in texts if _is_card(t)]
    assert len(cards) == 1, f"exactly one card, got {texts}"
    assert "Finding big files in gopi" in cards[0]
    assert VOICE not in cards[0], "the card is its own message"
    assert texts.index(VOICE) < texts.index(cards[0]), "opener first, card under it"
    assert len(da._ACK_EDITORS) == 1
    await pub.aclose()


@pytest.mark.asyncio
async def test_voice_line_timeout_gives_the_canned_floor_and_the_card_still_joins(
        provider, aux, children, dispatches, voice_calls, monkeypatch):
    monkeypatch.setattr(hn, "MODEL_DISPATCH_VOICE_TIMEOUT_SECONDS", 0.3)

    def slow(n, req):
        if n == 2:
            time.sleep(1.5)

    provider.before = slow
    chat = _TgChat()
    host = await _running_card(chat)
    started = time.monotonic()
    pub, agent, result = await _run_turn(chat, provider, aux)
    assert time.monotonic() - started < 8
    assert len(voice_calls) == 1 and voice_calls[0]["line"] == hn.FALLBACK_LINE
    reply = result["final_response"]
    assert reply in rb._FALLBACK_ONE, "the canned floor, only because the call timed out"
    assert len(host.parts) == 2, "and the card still joined"

    await _deliver(chat, reply)
    await _settle(200)
    texts = [t for _, t in _visible(chat)]
    assert len([t for t in texts if _is_card(t)]) == 1
    assert texts.index(reply) < max(i for i, t in enumerate(texts) if _is_card(t))
    await pub.aclose()


@pytest.mark.asyncio
async def test_voice_line_error_gives_the_canned_floor_and_one_card(
        provider, aux, children, dispatches, voice_calls):
    def boom(n, req):
        if n == 2:
            raise RuntimeError("provider down")

    provider.before = boom
    chat = _TgChat()
    pub, agent, result = await _run_turn(chat, provider, aux)
    reply = result["final_response"]
    assert len(voice_calls) == 1 and voice_calls[0]["line"] == hn.FALLBACK_LINE
    assert reply in rb._FALLBACK_ONE
    await _deliver(chat, reply)
    await _settle(200)
    texts = [t for _, t in _visible(chat)]
    assert len([t for t in texts if _is_card(t)]) == 1
    await pub.aclose()


@pytest.mark.asyncio
async def test_a_model_written_say_means_no_voice_call_and_no_duplicate(
        provider, aux, children, dispatches, voice_calls):
    """With a handoff carrying ``say``: the say is the only opener."""
    chat = _TgChat()
    host = await _running_card(chat)
    say = "I'm on GPT-6.1 Sol. Listing the big gopi files now."
    aux.replies[:] = [verdict_json("direct")]
    provider.queue[:] = [tool("delegate_task", {
        "tasks": [dict(TASK)],
        "handoff": {"work": "finding big files in gopi", "outcome": "a list", "say": say,
                    "estimated_minutes_min": 1, "estimated_minutes_max": 2}}, "d1"), text(VOICE)]
    agent = make_agent(provider, session_id=f"nh-say-{time.monotonic_ns()}")
    result = await asyncio.to_thread(
        agent.run_conversation, MESSAGE, conversation_history=[], task_id="nh-say")
    assert result["final_response"] == say
    assert voice_calls == [] and len(provider.captured) == 1, "no extra model call"
    await _deliver(chat, result["final_response"])
    texts = [s["text"] for s in chat.sends]
    assert texts.count(say) == 1 and VOICE not in texts
    assert len(host.parts) == 2


def test_no_handoff_with_text_beside_the_call_gets_no_second_line(
        provider, aux, children, dispatches, voice_calls):
    agent, result = _no_handoff_turn(provider, aux, spoken="I'm on GPT-6.1 Sol. Starting that now.")
    assert voice_calls == [] and len(provider.captured) == 1
    assert VOICE not in result["final_response"]


def test_cli_no_handoff_keeps_the_canned_line_and_makes_no_call(
        provider, aux, children, dispatches, voice_calls):
    aux.replies[:] = [verdict_json("direct")]
    provider.queue[:] = [tool("delegate_task", {"tasks": [dict(TASK)]}, "d1"), text(VOICE)]
    agent = make_agent(provider, platform="cli", session_id=f"nh-cli-{time.monotonic_ns()}")
    result = agent.run_conversation(MESSAGE, conversation_history=[], task_id="nh-cli")
    assert voice_calls == [] and len(provider.captured) == 1
    assert result["final_response"] in rb._FALLBACK_ONE


def test_two_untitled_tasks_are_named_from_their_goals_never_the_raw_goal():
    goals = ("Count to 200 slowly. Use the terminal tool with sleep 2 between calls.",
             "Summarise the logs of the gateway for the last hour.")
    r = fl.DispatchReceipt(delegation_id="d", seq=1, goals=goals, titles=("", ""))
    work = rb._receipts_work([r])
    assert "counting to 200 slowly" in work
    assert goals[0] not in work and goals[1] not in work
