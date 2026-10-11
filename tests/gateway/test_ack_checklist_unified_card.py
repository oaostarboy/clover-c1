"""Gateway side of the unified helper card: no 🔀 card for an owned dispatch,
and the ack message's running clock is refreshed while the helper works."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

import pytest

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl
from gateway import delegation_activity as da
from gateway.platforms.base import SendResult
from tests.gateway.test_delegation_activity import (
    FakeTelegramAdapter,
    _child_cb,
    _make_publisher,
    _turn_runner,
)

pytestmark = pytest.mark.unified_card

HEAD = "\U0001f504 fixing the bugs \u2192 test build + PR, ~10 min"
STEPS = ("find", "fix", "test")
OWNED = "deleg_aaaa0001"  # the id _child_cb gives its events


class _Adapter:
    def __init__(self) -> None:
        self.sends: List[str] = []
        self.edits: List[Dict[str, Any]] = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sends.append(content)
        return SendResult(success=True, message_id="m42")

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        self.edits.append({"id": message_id, "text": content, "finalize": finalize})
        return SendResult(success=True, message_id=message_id)


@pytest.fixture(autouse=True)
def _clean():
    ac.reset_for_tests()
    ac.set_style_for_tests("unified")
    da._ACK_EDITORS.clear()
    yield
    ac.reset_for_tests()
    da._ACK_EDITORS.clear()


def _card(did=OWNED):
    r = fl.DispatchReceipt(delegation_id=did, seq=1, goals=("g",), summary=HEAD, steps=STEPS)
    return ac.card_for([r])


async def _bind(adapter, card, *, interval=0.0):
    sent = await adapter.send("chat", card.initial_text)
    editor = da.AckCardEditor(
        card, adapter, "chat", sent.message_id, asyncio.get_running_loop(),
        min_interval=interval, clock=time.monotonic,
    )
    card.target = ("a", "chat", "m42")
    return editor


async def _settle(editor):
    for _ in range(50):
        await asyncio.sleep(0)
        t = editor._task
        if t is None or t.done():
            return


# -- the publisher ----------------------------------------------------------


async def _drive(adapter, *, did=OWNED):
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    cb = _child_cb(runner, delegation_id=did)
    cb("subagent.start", preview="Audit the gateway auth mixin")
    cb("tool.started", "read_file", "x.py", {"path": "x.py"})
    cb("tool.completed", "read_file", None, None, duration=0.2, is_error=False, result="body")
    cb("subagent.complete", status="completed", summary="Plain summary: All good.", duration_seconds=3)
    await pub.drain()
    return pub


@pytest.mark.asyncio
async def test_publisher_posts_no_roster_card_for_an_owned_delegation_under_unified():
    card = _card()
    adapter = FakeTelegramAdapter()
    pub = await _drive(adapter)
    assert adapter.status_calls == [] and adapter.sends == [], "no 🔀 card, no summary"
    assert pub.tracker.group_ids() == [], "nothing tracked for an owned id"
    # ...and the unified card did get the activity and the result
    cl = card.parts[0].checklist
    assert cl.model == "Opus 5.5" and cl.calls >= 1
    assert cl.state == ac.DONE and cl.summary == "All good."
    await pub.aclose()


@pytest.mark.asyncio
async def test_publisher_still_posts_the_roster_card_under_classic():
    _card()
    ac.set_style_for_tests("classic")
    adapter = FakeTelegramAdapter()
    pub = await _drive(adapter)
    assert adapter.status_calls or adapter.sends, "classic keeps the separate 🔀 card"
    assert pub.tracker.group_ids() != []
    await pub.aclose()


@pytest.mark.asyncio
async def test_publisher_still_posts_for_a_delegation_without_a_unified_card():
    # unified is on, but no ack part exists for this id (e.g. a plain
    # delegate_task the router never dispatched): the roster card stays.
    _card("some_other_id")
    adapter = FakeTelegramAdapter()
    pub = await _drive(adapter)
    assert adapter.status_calls or adapter.sends
    await pub.aclose()


@pytest.mark.asyncio
async def test_turn_card_absorb_has_nothing_to_absorb_for_owned_delegations():
    _card()
    adapter = FakeTelegramAdapter()
    from gateway import delegation_activity as mod

    pub = mod.DelegationActivityPublisher(
        adapter=adapter, chat_id="chat-A", loop=asyncio.get_running_loop(),
        min_interval=0.0, auto_heartbeat=False, expandable=True, combined=True,
    )
    runner = _turn_runner(pub)
    cb = _child_cb(runner)
    cb("subagent.start", preview="g")
    cb("subagent.complete", status="completed", summary="Plain summary: Done.", duration_seconds=1)
    await pub.drain()
    workers, ids = pub.absorb_finished()
    assert workers == [] and ids == []
    from agent.delegation_activity import turn_card_body

    assert turn_card_body(workers, 0.0) == [], "no empty worker rows on the turn card"
    pub.end_turn()
    await pub.drain()
    assert adapter.sends == [] and adapter.status_calls == []
    await pub.aclose()


# -- the editor's clock tick -------------------------------------------------


@pytest.mark.asyncio
async def test_editor_schedules_a_tick_while_running_under_unified(monkeypatch):
    adapter, card = _Adapter(), _card()
    editor = await _bind(adapter, card)
    calls: List[float] = []
    loop = asyncio.get_running_loop()
    real = loop.call_later
    monkeypatch.setattr(loop, "call_later", lambda d, fn, *a: calls.append(d) or real(3600, fn, *a))
    editor._tick_later()
    assert calls == [da.ACK_TICK_SECONDS] and editor._ticking is True
    editor._tick_later()
    assert calls == [da.ACK_TICK_SECONDS], "one pending tick at a time"


@pytest.mark.asyncio
async def test_editor_schedules_no_tick_under_classic(monkeypatch):
    adapter, card = _Adapter(), _card()
    editor = await _bind(adapter, card)
    ac.set_style_for_tests("classic")
    calls: List[float] = []
    monkeypatch.setattr(asyncio.get_running_loop(), "call_later", lambda *a: calls.append(a))
    editor._tick_later()
    assert calls == [] and editor._ticking is False


@pytest.mark.asyncio
async def test_editor_schedules_no_tick_once_finished_or_gone(monkeypatch):
    adapter, card = _Adapter(), _card()
    editor = await _bind(adapter, card)
    calls: List[Any] = []
    monkeypatch.setattr(asyncio.get_running_loop(), "call_later", lambda *a: calls.append(a))
    ac.on_child_event("subagent.complete", delegation_id=OWNED, status="completed", summary="Plain summary: ok.")
    assert card.finished
    editor._tick_later()
    assert calls == []
    editor.gone = True
    editor._tick_later()
    assert calls == []


@pytest.mark.asyncio
async def test_run_with_unchanged_text_arms_the_tick(monkeypatch):
    adapter, card = _Adapter(), _card()
    editor = await _bind(adapter, card)
    armed: List[int] = []
    monkeypatch.setattr(editor, "_tick_later", lambda: armed.append(1))
    editor.notify()
    await _settle(editor)
    assert armed == [1] and adapter.edits == [], "same text: no edit, just the next tick"


@pytest.mark.asyncio
async def test_the_tick_refreshes_the_running_clock_with_an_edit(monkeypatch):
    monkeypatch.setattr(da, "ACK_TICK_SECONDS", 0.05)
    adapter, card = _Adapter(), _card()
    ac.on_child_event("subagent.start", delegation_id=OWNED, model="claude-opus-5-5")
    editor = await _bind(adapter, card)
    editor.notify()
    await _settle(editor)
    assert adapter.edits == []
    card.parts[0].checklist.started -= 30  # the helper has been at it a while
    await asyncio.sleep(0.3)
    await _settle(editor)
    assert adapter.edits, "the clock was refreshed with no other change"
    assert "\u23f1 3" in adapter.edits[-1]["text"] or "\u23f1 30s" in adapter.edits[-1]["text"]
    assert len(adapter.sends) == 1, "an edit, never a new message"


@pytest.mark.asyncio
async def test_ticks_stop_when_the_helper_finishes(monkeypatch):
    monkeypatch.setattr(da, "ACK_TICK_SECONDS", 0.05)
    adapter, card = _Adapter(), _card()
    ac.on_child_event("subagent.start", delegation_id=OWNED, model="claude-opus-5-5")
    editor = await _bind(adapter, card)
    editor.notify()
    await _settle(editor)
    ac.on_child_event("subagent.complete", delegation_id=OWNED, status="completed", summary="Plain summary: ok.")
    await asyncio.sleep(0.2)
    await _settle(editor)
    edits_after_finish = len(adapter.edits)
    assert adapter.edits[-1]["finalize"] is True
    assert adapter.edits[-1]["text"].splitlines()[0].startswith("\U0001f340 ")
    await asyncio.sleep(0.3)
    assert len(adapter.edits) == edits_after_finish, "no edits after the final one"
    assert editor._ticking is False


@pytest.mark.asyncio
async def test_publisher_keeps_the_roster_card_for_a_multi_helper_dispatch():
    r = fl.DispatchReceipt(delegation_id=OWNED, seq=1, goals=("g1", "g2"), summary=HEAD, steps=())
    ac.card_for([r])
    assert ac.owns_delegation(OWNED) is False
    adapter = FakeTelegramAdapter()
    pub = await _drive(adapter)
    assert adapter.status_calls or adapter.sends
    await pub.aclose()


@pytest.mark.asyncio
async def test_editor_does_not_tick_a_multi_helper_card(monkeypatch):
    r = fl.DispatchReceipt(delegation_id="dm", seq=1, goals=("g1", "g2"), summary=HEAD, steps=())
    adapter, card = _Adapter(), ac.card_for([r])
    editor = await _bind(adapter, card)
    calls: List[Any] = []
    monkeypatch.setattr(asyncio.get_running_loop(), "call_later", lambda *a: calls.append(a))
    editor._tick_later()
    assert calls == []
