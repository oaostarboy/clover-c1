"""Busy-session notice truthfulness for Telegram photo follow-ups.

The busy acknowledgment must describe what actually happened to the running
turn, not what ``busy_input_mode`` would have done to a live agent.  A photo
that lands while the turn is still being prepared (pending sentinel) cannot
interrupt anything: it is queued, and the notice must say so.  A photo that
lands on a live agent still interrupts it and still says so.
"""
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key


INTERRUPT_HEAD = "Interrupting current task"
INTERRUPT_TIP = "I just interrupted my current task"
QUEUED_HEAD = "Queued for the next turn"
FIRST_TIME_TIP = "First-time tip"


@pytest.fixture(autouse=True)
def _stock_wording(monkeypatch, tmp_path):
    """Pin the stock ack wording and keep the onboarding flag in tmp_path."""
    import gateway.run as gateway_run
    from clover_cli import skin_engine

    monkeypatch.setattr(skin_engine, "_active_skin", None)
    monkeypatch.setattr(skin_engine, "_active_skin_name", "default")
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.delenv("CLOVER_GATEWAY_BUSY_ACK_ENABLED", raising=False)


def _make_runner(mode="interrupt"):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._queued_events = {}
    runner._draining = False
    runner._busy_input_mode = mode
    runner._busy_text_mode = "interrupt"
    runner.adapters = {}
    runner.config = MagicMock()
    runner.config.group_sessions_per_user = True
    runner.config.thread_sessions_per_user = False
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda _source: True
    return runner


def _make_adapter():
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter._text_debounce = {}
    adapter._busy_text_debounce_seconds = 0.6
    return adapter


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm", user_id="u1"
    )


def _photo_event(path="/tmp/photo-a.jpg", caption="", message_id="m1"):
    return MessageEvent(
        text=caption,
        message_type=MessageType.PHOTO,
        source=_source(),
        message_id=message_id,
        media_urls=[path],
        media_types=["image/jpeg"],
    )


def _live_agent():
    agent = MagicMock()
    agent.get_activity_summary.return_value = {
        "api_call_count": 2,
        "max_iterations": 60,
        "current_tool": None,
        "last_activity_ts": time.time(),
        "last_activity_desc": "api",
        "seconds_since_activity": 0.1,
    }
    return agent


def _ack_text(adapter):
    assert adapter._send_with_retry.await_count == 1
    return adapter._send_with_retry.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_photo_during_pending_sentinel_is_acked_as_queued_not_interrupted():
    """No live agent exists yet, so nothing was interrupted and the ack says queued."""
    from gateway.run import _AGENT_PENDING_SENTINEL

    runner = _make_runner("interrupt")
    adapter = _make_adapter()
    event = _photo_event()
    session_key = build_session_key(event.source)
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._running_agents[session_key] = _AGENT_PENDING_SENTINEL
    runner._running_agents_ts[session_key] = time.time()

    handled = await runner._handle_active_session_busy_message(event, session_key)

    assert handled is True
    # The input is preserved for the next turn.
    assert adapter._pending_messages[session_key] is event
    content = _ack_text(adapter)
    assert INTERRUPT_HEAD not in content
    assert INTERRUPT_TIP not in content
    assert QUEUED_HEAD in content
    # The one-time /busy tip describes the configured mode in action; it is
    # not spent on a follow-up that was queued only because of timing.
    assert FIRST_TIME_TIP not in content


@pytest.mark.asyncio
async def test_photo_on_live_agent_still_interrupts_and_says_so():
    """A real running agent is interrupted, and only then does the ack say so."""
    runner = _make_runner("interrupt")
    adapter = _make_adapter()
    event = _photo_event(caption="what is this")
    session_key = build_session_key(event.source)
    agent = _live_agent()
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._running_agents[session_key] = agent
    runner._running_agents_ts[session_key] = time.time() - 5

    handled = await runner._handle_active_session_busy_message(event, session_key)

    assert handled is True
    agent.interrupt.assert_called_once_with("what is this")
    assert adapter._pending_messages[session_key] is event
    content = _ack_text(adapter)
    assert INTERRUPT_HEAD in content
    assert QUEUED_HEAD not in content


@pytest.mark.asyncio
async def test_photo_whose_interrupt_fails_is_acked_as_queued():
    """An interrupt that raised stopped nothing; the input is still queued."""
    runner = _make_runner("interrupt")
    adapter = _make_adapter()
    event = _photo_event(caption="what is this")
    session_key = build_session_key(event.source)
    agent = _live_agent()
    agent.interrupt.side_effect = RuntimeError("agent already torn down")
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._running_agents[session_key] = agent
    runner._running_agents_ts[session_key] = time.time() - 5

    handled = await runner._handle_active_session_busy_message(event, session_key)

    assert handled is True
    assert adapter._pending_messages[session_key] is event
    content = _ack_text(adapter)
    assert INTERRUPT_HEAD not in content
    assert INTERRUPT_TIP not in content
    assert QUEUED_HEAD in content


@pytest.mark.asyncio
async def test_photo_with_no_agent_behind_the_adapter_guard_is_acked_as_queued():
    """The adapter guard is still held but no agent is registered: nothing to stop."""
    runner = _make_runner("interrupt")
    adapter = _make_adapter()
    event = _photo_event()
    session_key = build_session_key(event.source)
    runner.adapters[Platform.TELEGRAM] = adapter

    handled = await runner._handle_active_session_busy_message(event, session_key)

    assert handled is True
    assert adapter._pending_messages[session_key] is event
    content = _ack_text(adapter)
    assert INTERRUPT_HEAD not in content
    assert QUEUED_HEAD in content


@pytest.mark.asyncio
async def test_photo_in_queue_mode_is_queued_without_touching_the_live_agent():
    """Queue mode never interrupts, with or without a live agent."""
    runner = _make_runner("queue")
    adapter = _make_adapter()
    event = _photo_event()
    session_key = build_session_key(event.source)
    agent = _live_agent()
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._running_agents[session_key] = agent
    runner._running_agents_ts[session_key] = time.time() - 5

    handled = await runner._handle_active_session_busy_message(event, session_key)

    assert handled is True
    agent.interrupt.assert_not_called()
    assert adapter._pending_messages[session_key] is event
    content = _ack_text(adapter)
    assert INTERRUPT_HEAD not in content
    assert QUEUED_HEAD in content


def _queued_media(runner, adapter, session_key):
    """Every media path waiting for the next turn(s), wherever it is parked."""
    events = []
    head = adapter._pending_messages.get(session_key)
    if head is not None:
        events.append(head)
    events.extend(runner._queued_events.get(session_key, []))
    return [url for queued in events for url in queued.media_urls]


@pytest.mark.asyncio
async def test_repeated_photo_followups_keep_every_input_and_ack_once():
    """A second follow-up inside the ack cooldown adds its input, not a second notice."""
    from gateway.run import _AGENT_PENDING_SENTINEL

    runner = _make_runner("interrupt")
    adapter = _make_adapter()
    first = _photo_event("/tmp/photo-a.jpg", message_id="m1")
    second = _photo_event("/tmp/photo-b.jpg", message_id="m2")
    session_key = build_session_key(first.source)
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._running_agents[session_key] = _AGENT_PENDING_SENTINEL
    runner._running_agents_ts[session_key] = time.time()

    assert await runner._handle_active_session_busy_message(first, session_key) is True
    assert await runner._handle_active_session_busy_message(second, session_key) is True

    assert sorted(_queued_media(runner, adapter, session_key)) == [
        "/tmp/photo-a.jpg",
        "/tmp/photo-b.jpg",
    ]
    assert adapter._send_with_retry.await_count == 1


def _outcome_records(caplog):
    return [r.getMessage() for r in caplog.records if "Busy follow-up" in r.getMessage()]


@pytest.mark.asyncio
async def test_busy_outcome_is_logged_with_ids_and_state_only(caplog):
    """The outcome record correlates by id/state and never carries the caption."""
    import logging

    from gateway.run import _AGENT_PENDING_SENTINEL

    runner = _make_runner("interrupt")
    adapter = _make_adapter()
    event = _photo_event(caption="PRIVATE-CAPTION-TEXT", message_id="m7")
    event.metadata["telegram_media_group_id"] = "album-77"
    session_key = build_session_key(event.source)
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._running_agents[session_key] = _AGENT_PENDING_SENTINEL

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        await runner._handle_active_session_busy_message(event, session_key)

    records = _outcome_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert "outcome=queued" in record
    assert "agent=pending" in record
    assert "message_id=m7" in record
    assert "album=album-77" in record
    assert "PRIVATE-CAPTION-TEXT" not in record
    assert "/tmp/photo-a.jpg" not in record


@pytest.mark.asyncio
async def test_busy_outcome_log_names_a_real_interrupt(caplog):
    import logging

    runner = _make_runner("interrupt")
    adapter = _make_adapter()
    event = _photo_event(caption="PRIVATE-CAPTION-TEXT")
    session_key = build_session_key(event.source)
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._running_agents[session_key] = _live_agent()
    runner._running_agents_ts[session_key] = time.time() - 5

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        await runner._handle_active_session_busy_message(event, session_key)

    records = _outcome_records(caplog)
    assert len(records) == 1
    assert "outcome=interrupted" in records[0]
    assert "agent=live" in records[0]
    assert "album=-" in records[0]
    assert "PRIVATE-CAPTION-TEXT" not in records[0]
