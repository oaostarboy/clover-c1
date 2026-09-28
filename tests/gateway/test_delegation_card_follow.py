"""Running subagent cards follow the latest message (re-posted below the reply)."""

from __future__ import annotations

import pytest

from gateway import delegation_activity as da
from gateway.delegation_activity import follow_latest_message
from tests.gateway.test_delegation_activity import (
    FakeTelegramAdapter,
    _child_cb,
    _make_publisher,
    _turn_runner,
)


class _NoDeleteAdapter(FakeTelegramAdapter):
    delete_message = None


@pytest.fixture(autouse=True)
def _clean_registry():
    da._LIVE.clear(); da._LAST_OUT.clear(); da._FOLLOW_TIMERS.clear()
    yield
    for h in list(da._FOLLOW_TIMERS.values()):
        h.cancel()
    da._LIVE.clear(); da._LAST_OUT.clear(); da._FOLLOW_TIMERS.clear()


@pytest.mark.asyncio
async def test_running_card_moves_below_latest_message():
    ad = FakeTelegramAdapter()
    pub = _make_publisher(ad)
    a = _child_cb(_turn_runner(pub))
    a("subagent.start", preview="Audit the gateway auth mixin")
    await pub.drain()
    key = ("chat-A", "delegation:deleg_aaaa0001")
    old_id = ad._status_message_ids[key]

    await ad.send("chat-A", "parent reply")  # the parent's reply lands
    da._LAST_OUT[da._inbox_key(ad, "chat-A")] = "s1"
    moved = await follow_latest_message(ad, "chat-A")

    assert moved == 1
    assert old_id in ad.deleted
    new_id = ad._status_message_ids[key]
    assert new_id == "s2"  # a NEW message, posted after the reply (s1)
    assert "Audit gateway auth" in ad.sends[-1]["content"]

    # Later progress edits the moved card instead of posting a third one.
    a("tool.started", "read_file", "x.py", {"path": "x.py"})
    await pub.drain()
    assert ad.status_calls[-1]["key"] == "delegation:deleg_aaaa0001"
    assert ad._status_message_ids[key] == "s2"
    assert len(ad.sends) == 2
    await pub.aclose()


@pytest.mark.asyncio
async def test_finished_cards_are_not_moved():
    ad = FakeTelegramAdapter()
    pub = _make_publisher(ad)
    a = _child_cb(_turn_runner(pub))
    a("subagent.start", preview="g")
    await pub.drain()
    a("subagent.complete", status="completed", summary="done", duration_seconds=3)
    await pub.drain()
    before = (list(ad.sends), list(ad.deleted))
    assert await follow_latest_message(ad, "chat-A") == 0
    assert (ad.sends, ad.deleted) == before
    await pub.aclose()


@pytest.mark.asyncio
async def test_no_delete_support_means_no_move():
    ad = _NoDeleteAdapter()
    pub = _make_publisher(ad)
    a = _child_cb(_turn_runner(pub))
    a("subagent.start", preview="g")
    await pub.drain()
    assert await follow_latest_message(ad, "chat-A") == 0
    assert ad.sends == []
    await pub.aclose()


@pytest.mark.asyncio
async def test_other_chats_untouched():
    ad = FakeTelegramAdapter()
    pub = _make_publisher(ad)
    a = _child_cb(_turn_runner(pub))
    a("subagent.start", preview="g")
    await pub.drain()
    assert await follow_latest_message(ad, "chat-B") == 0
    assert ad.deleted == []
    await pub.aclose()


@pytest.mark.asyncio
async def test_idle_publisher_drops_out_of_registry():
    ad = FakeTelegramAdapter()
    _make_publisher(ad)  # never started anything
    assert await follow_latest_message(ad, "chat-A") == 0
    assert da._LIVE == {}


@pytest.mark.asyncio
async def test_closed_publisher_unregisters():
    ad = FakeTelegramAdapter()
    pub = _make_publisher(ad)
    await pub.aclose()
    assert da._LIVE == {}


@pytest.mark.asyncio
async def test_base_adapter_moves_cards_after_reply_delivery():
    """The hook lives in the shared message handler, after post-delivery."""
    import inspect

    from gateway.platforms import base

    src = inspect.getsource(base)
    hook = src.index("follow_latest_message(self, event.source.chat_id)")
    post_cb = src.index("_post_cb = self.pop_post_delivery_callback(")
    assert hook > post_cb


class _SeqAdapter(FakeTelegramAdapter):
    """Numeric, increasing message ids like Telegram."""

    def __init__(self):
        super().__init__()
        self._next = 100

    def _id(self):
        self._next += 1
        return str(self._next)

    async def send_or_update_status(self, chat_id, status_key, content, *, metadata=None):
        from gateway.platforms.base import SendResult

        key = (chat_id, status_key)
        mid = self._status_message_ids.get(key) or self._id()
        self._status_message_ids[key] = mid
        self._mid_key[mid] = key
        self.cards[key] = content
        self.status_calls.append({"chat_id": chat_id, "key": status_key, "content": content})
        return SendResult(success=True, message_id=mid)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        from gateway.platforms.base import SendResult

        mid = self._id()
        self.sends.append({"chat_id": chat_id, "content": content, "id": mid})
        da.note_outbound(self, chat_id, mid)  # what BasePlatformAdapter.send does
        return SendResult(success=True, message_id=mid)


@pytest.mark.asyncio
async def test_interim_messages_move_card_once_after_burst(monkeypatch):
    import asyncio

    monkeypatch.setattr(da, "FOLLOW_DEBOUNCE_SECONDS", 0.05)
    ad = _SeqAdapter()
    pub = _make_publisher(ad)
    a = _child_cb(_turn_runner(pub))
    a("subagent.start", preview="g")
    await pub.drain()
    key = ("chat-A", "delegation:deleg_aaaa0001")
    card_id = ad._status_message_ids[key]

    # A burst of mid-turn messages (interim text, tool bubbles...).
    for i in range(5):
        await ad.send("chat-A", f"interim {i}")
    await asyncio.sleep(0.2)

    moved = [d for d in ad.deleted]
    assert moved == [card_id]  # exactly one move for the whole burst
    new_id = ad._status_message_ids[key]
    assert int(new_id) > int(ad.sends[4]["id"])  # below the last message
    await pub.aclose()


@pytest.mark.asyncio
async def test_card_already_newest_is_left_alone(monkeypatch):
    ad = _SeqAdapter()
    pub = _make_publisher(ad)
    await ad.send("chat-A", "earlier message")
    a = _child_cb(_turn_runner(pub))
    a("subagent.start", preview="g")
    await pub.drain()
    assert await follow_latest_message(ad, "chat-A") == 0
    assert ad.deleted == []
    await pub.aclose()


@pytest.mark.asyncio
async def test_card_own_edits_do_not_trigger_a_move(monkeypatch):
    import asyncio

    monkeypatch.setattr(da, "FOLLOW_DEBOUNCE_SECONDS", 0.05)
    ad = _SeqAdapter()
    pub = _make_publisher(ad)
    a = _child_cb(_turn_runner(pub))
    a("subagent.start", preview="g")
    await pub.drain()
    for i in range(3):
        a("tool.started", "read_file", f"f{i}.py", {"path": f"f{i}.py"})
        await pub.drain()
    await asyncio.sleep(0.15)
    assert ad.deleted == []
    assert da._FOLLOW_TIMERS == {}
    await pub.aclose()


def test_every_adapter_send_reports_outbound():
    from gateway.platforms.base import BasePlatformAdapter

    class _A(BasePlatformAdapter):
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return None

    assert getattr(_A.send, "_notes_outbound", False) is True
