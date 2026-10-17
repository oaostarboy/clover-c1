"""The ack checklist message is edited in place: same message id, no new posts."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl
from gateway import delegation_activity as da
from gateway.platforms.base import SendResult

HEAD = "\U0001f504 fixing the bugs \u2192 test build + PR, ~10 min"
STEPS = ("find", "fix", "test")


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Adapter:
    """Telegram-shaped: send() returns a message id, edit_message edits it."""

    def __init__(self, *, edit_results=None, with_edit=True) -> None:
        self.sends: List[str] = []
        self.edits: List[Dict[str, Any]] = []
        self._results = list(edit_results or [])
        if not with_edit:
            self.edit_message = None  # type: ignore[assignment]

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sends.append(content)
        return SendResult(success=True, message_id="m42")

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        self.edits.append({"id": message_id, "text": content, "finalize": finalize})
        if self._results:
            return self._results.pop(0)
        return SendResult(success=True, message_id=message_id)


@pytest.fixture(autouse=True)
def _clean():
    ac.reset_for_tests()
    da._ACK_EDITORS.clear()
    yield
    ac.reset_for_tests()
    da._ACK_EDITORS.clear()


def _card():
    r = fl.DispatchReceipt(delegation_id="d1", seq=1, goals=("g",), summary=HEAD, steps=STEPS)
    return ac.card_for([r])


def _todos(*s):
    return [{"id": f"s{i}", "status": x} for i, x in enumerate(s, 1)]


async def _bind(adapter, card, clock, interval=4.0):
    sent = await adapter.send("chat", card.initial_text)
    editor = da.AckCardEditor(
        card, adapter, "chat", sent.message_id, asyncio.get_running_loop(),
        min_interval=interval, clock=clock,
    )
    card.target = ("a", "chat", "m42")
    return editor


async def _settle(editor):
    for _ in range(50):
        await asyncio.sleep(0)
        t = editor._task
        if t is None or t.done():
            return


@pytest.mark.asyncio
async def test_ticks_edit_the_same_message_and_never_send_again():
    clock, adapter, card = _Clock(), _Adapter(), _card()
    editor = await _bind(adapter, card, clock)
    clock.now += 10
    ac.on_child_event("subagent.todo", delegation_id="d1", todos=_todos("completed", "in_progress", "pending"))
    await _settle(editor)
    clock.now += 10
    ac.on_child_event("subagent.complete", delegation_id="d1", status="completed")
    await _settle(editor)

    assert len(adapter.sends) == 1, "ticks never post a new message"
    assert [e["id"] for e in adapter.edits] == ["m42"] * len(adapter.edits) and adapter.edits
    assert adapter.edits[0]["text"].splitlines()[1] == "\U0001f340 find"
    last = adapter.edits[-1]
    assert last["text"].splitlines()[0].startswith("\U0001f340 ") and last["finalize"] is True


@pytest.mark.asyncio
async def test_a_burst_of_ticks_inside_the_interval_coalesces_into_one_edit():
    import time

    adapter, card = _Adapter(), _card()
    editor = await _bind(adapter, card, time.monotonic, interval=0.3)
    await asyncio.sleep(0.35)  # gate open
    ac.on_child_event("subagent.todo", delegation_id="d1", todos=_todos("completed", "pending", "pending"))
    await _settle(editor)
    assert len(adapter.edits) == 1
    # three more ticks inside the throttle window: nothing is sent yet ...
    ac.on_child_event("subagent.todo", delegation_id="d1", todos=_todos("completed", "in_progress", "pending"))
    ac.on_child_event("subagent.todo", delegation_id="d1", todos=_todos("completed", "completed", "pending"))
    ac.on_child_event("subagent.todo", delegation_id="d1", todos=_todos("completed", "completed", "in_progress"))
    await asyncio.sleep(0.05)
    assert len(adapter.edits) == 1, "throttled: the burst waits for the gate"
    # ... and when the gate opens exactly one edit carries the latest state
    await asyncio.sleep(0.5)
    await _settle(editor)
    assert len(adapter.edits) == 2
    assert adapter.edits[-1]["text"].splitlines()[1:] == ["\U0001f340 find", "\U0001f340 fix", "\U0001f504 test"]
    assert len(adapter.sends) == 1


@pytest.mark.asyncio
async def test_rate_limit_backs_off_then_retries_the_latest_state_as_an_edit():
    clock = _Clock()
    adapter = _Adapter(edit_results=[SendResult(success=False, error="flood", retry_after=7)])
    card = _card()
    editor = await _bind(adapter, card, clock)
    clock.now += 10
    ac.on_child_event("subagent.todo", delegation_id="d1", todos=_todos("completed", "pending", "pending"))
    await _settle(editor)
    assert len(adapter.edits) == 1 and editor._backoff_until == clock.now + 7
    assert len(adapter.sends) == 1


@pytest.mark.asyncio
async def test_a_deleted_message_stops_editing_and_never_reposts():
    clock = _Clock()
    adapter = _Adapter(edit_results=[SendResult(success=False, error="Message to edit not found")])
    card = _card()
    editor = await _bind(adapter, card, clock)
    clock.now += 10
    ac.on_child_event("subagent.todo", delegation_id="d1", todos=_todos("completed", "pending", "pending"))
    await _settle(editor)
    assert editor.gone and len(adapter.sends) == 1
    clock.now += 10
    ac.on_child_event("subagent.complete", delegation_id="d1", status="completed")
    await _settle(editor)
    assert len(adapter.edits) == 1 and len(adapter.sends) == 1


@pytest.mark.asyncio
async def test_no_edit_transport_leaves_the_static_text_and_does_not_raise():
    clock, adapter, card = _Clock(), _Adapter(with_edit=False), _card()
    editor = await _bind(adapter, card, clock)
    clock.now += 10
    ac.on_child_event("subagent.complete", delegation_id="d1", status="completed")
    await _settle(editor)
    assert editor.gone and adapter.sends == [card.initial_text]


@pytest.mark.asyncio
async def test_bind_ack_card_matches_by_the_text_the_adapter_sent():
    adapter, card = _Adapter(), _card()
    sent = await adapter.send("chat", card.initial_text + " \u258c")  # streaming cursor
    da.bind_ack_card(adapter, "chat", card.initial_text + " \u258c", sent)
    assert card.target is not None and id(card) in da._ACK_EDITORS
    other = await adapter.send("chat", "some unrelated reply")
    da.bind_ack_card(adapter, "chat", "some unrelated reply", other)
    assert len(da._ACK_EDITORS) == 1, "only the ack text is bound"


@pytest.mark.asyncio
async def test_adapter_send_path_binds_the_ack_message():
    """The real BasePlatformAdapter.send wrapper binds a sent ack to its card."""
    from gateway.platforms.base import BasePlatformAdapter

    class Sub(BasePlatformAdapter):  # noqa: D401 - minimal concrete adapter
        async def connect(self): return True
        async def disconnect(self): return None
        async def get_chat_info(self, chat_id): return {}
        async def send_typing(self, *a, **k): return None
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return SendResult(success=True, message_id="m9")

    card = _card()
    adapter = Sub.__new__(Sub)
    await adapter.send("chat", content=card.initial_text)
    assert card.target is not None
