"""A worker's progress card must survive a Telegram adapter rebuild.

When the gateway rebuilds the Telegram adapter after a network failure, a
delegated worker's progress publisher keeps the adapter captured at the start
of the turn that dispatched it. The old instance is disconnected (``_bot`` is
None) and ``runner.adapters`` points at the replacement. The card must keep
updating on the live adapter and stay visible, instead of silently freezing.

Production Telegram runs the publisher in board mode (combined=True plus an
adapter that can delete), which refreshes the card by edit/move.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.delegation_activity as da
from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402
from tests.gateway.test_delegation_activity import (
    FakeClock,
    FakeTelegramAdapter,
    _child_cb,
    _turn_runner,
)


@pytest.fixture(autouse=True)
def _clean_card_state(monkeypatch):
    registries = (da._LIVE, da._LAST_OUT, da._FOLLOW_TIMERS, da._BOARDS)
    for registry in registries:
        registry.clear()
    monkeypatch.setattr(da, "BOARD_MIN_EDIT_SECONDS", 0)
    yield
    for registry in registries:
        registry.clear()


def _publisher(adapter):
    return da.DelegationActivityPublisher(
        adapter=adapter,
        chat_id="chat-A",
        metadata=None,
        loop=asyncio.get_running_loop(),
        is_current=lambda: True,
        heartbeat_seconds=60,
        min_interval=0.0,
        clock=FakeClock(),
        auto_heartbeat=False,
        expandable=True,
        combined=True,
    )


async def _settle(pub):
    await pub.drain()
    await asyncio.sleep(0.2)


# -- real TelegramAdapter instances, mocked bots ------------------------------


def _bot(first_id: int) -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=first_id))
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=first_id))
    bot.delete_message = AsyncMock(return_value=True)
    return bot


def _real_adapter(first_id: int) -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._rich_send_disabled = True
    adapter._RECONNECT_WAIT_SECONDS = 0.05
    adapter._RECONNECT_POLL_INTERVAL = 0.01
    adapter._bot = _bot(first_id)
    return adapter


@pytest.mark.asyncio
async def test_card_edits_land_on_live_adapter_after_adapter_rebuild():
    old = _real_adapter(100)
    live = _real_adapter(200)
    runner = MagicMock()
    runner.adapters = {old.platform: old}
    old.gateway_runner = runner
    live.gateway_runner = runner

    pub = _publisher(old)
    assert pub._board_mode, "Telegram production path is board mode"
    cb = _child_cb(_turn_runner(pub), delegation_id="deleg_x")
    cb("subagent.start", preview="Higgsfield edits")
    await _settle(pub)
    old_bot = old._bot
    assert old_bot.send_message.await_count == 1, "card is first posted on the old adapter"

    # Network blip: the gateway rebuilds the adapter and disconnects the old one.
    old._bot = None
    runner.adapters[old.platform] = live

    cb("tool.started", "terminal", "higgsfield job", {})
    cb("tool.started", "terminal", "higgsfield job 2", {})
    await _settle(pub)

    assert live._bot.edit_message_text.await_count >= 1, (
        "worker updates must reach the live adapter"
    )
    assert old_bot.edit_message_text.await_count == 0
    # The card is still one visible board: nothing was deleted without a repost.
    board = next(iter(da._BOARDS.values()))
    assert board.message_id is not None
    await pub.aclose()


# -- adapter that cannot edit and has no replacement to forward to ------------

@pytest.mark.asyncio
async def test_public_runtime_status_reaches_real_telegram_adapter():
    import io
    from clover_cli.activity_events import ActivityEventWriter
    from tools.agent_job_observer import AgentJobObserver
    from tests.agent.test_delegation_checkpoint import _agent
    adapter = _real_adapter(300)
    pub = _publisher(adapter)
    observer = AgentJobObserver(session_id='test-status-worker',sink=pub,
        group_id='test-status-group',index=0,title='Saved build verification',
        model='test-model',parser='clover-activity')
    stream = io.StringIO()
    writer = ActivityEventWriter(stream)
    agent = _agent()
    agent.tool_progress_callback = writer.tool_progress_callback
    observer.start()
    try:
        for desc, expected in [
            ('starting API call #1','requesting provider'),
            ('waiting for provider response (streaming)','waiting for provider'),
            ('retrying provider request','retrying provider'),
        ]:
            stream.seek(0); stream.truncate(0)
            agent._touch_activity(desc)
            observer.feed(stream.getvalue())
            await _settle(pub)
            calls = adapter._bot.send_message.await_args_list + adapter._bot.edit_message_text.await_args_list
            assert any(expected in str(call.kwargs.get('text','')) for call in calls)
        stream.seek(0); stream.truncate(0)
        writer.result('actual conversation ended','completed')
        observer.feed(stream.getvalue()); observer.finish(0)
        await _settle(pub)
        assert pub.tracker.snapshot('test-status-group')[0]['state']=='completed'
        before=adapter._bot.send_message.await_count
        observer.finish(0)
        observer.feed(stream.getvalue())
        await _settle(pub)
        assert adapter._bot.send_message.await_count==before
    finally:
        await pub.aclose()



class _DeadEditAdapter(FakeTelegramAdapter):
    """edit fails with 'Not connected' and cannot be forwarded; send reaches a
    live adapter (as TelegramAdapter.send does on a replaced adapter)."""

    def __init__(self, live: FakeTelegramAdapter) -> None:
        super().__init__()
        self.live = live
        self.dead = False
        self.edit_attempts = 0

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if self.dead:
            return await self.live.send(chat_id, content, reply_to, metadata)
        return await super().send(chat_id, content, reply_to, metadata)

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        self.edit_attempts += 1
        if self.dead:
            return SendResult(success=False, error="Not connected")
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id, message_id):
        return False if self.dead else await super().delete_message(chat_id, message_id)


@pytest.mark.asyncio
async def test_dead_adapter_edit_failure_reposts_card_on_live_adapter():
    live = FakeTelegramAdapter()
    old = _DeadEditAdapter(live)
    pub = _publisher(old)
    assert pub._board_mode
    cb = _child_cb(_turn_runner(pub), delegation_id="deleg_x")
    cb("subagent.start", preview="Higgsfield edits")
    await _settle(pub)
    assert len(old.sends) == 1 and not live.sends

    old.dead = True
    cb("tool.started", "terminal", "higgsfield job", {})
    await _settle(pub)

    assert old.edit_attempts >= 1
    assert live.sends, "the card must be re-posted where the user can see it"
    assert "higgsfield job" in live.sends[-1]["content"]
    await pub.aclose()


def test_not_connected_is_a_gone_error_but_flood_is_not():
    assert da._message_gone(SendResult(success=False, error="Not connected"))
    assert not da._message_gone(SendResult(success=False, error="Flood control: retry in 5"))
    assert not da._message_gone(SendResult(success=True, message_id="1"))
