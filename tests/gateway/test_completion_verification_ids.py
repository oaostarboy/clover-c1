"""Trusted completion ids reach the agent turn (C1.5 B2).

A helper's completion re-enters the gateway as ``MessageEvent(internal=True)``
built from the producer's event.  The ids the verification episode keys on
(``delegation_id`` / ``batch_delegation_id``, plus coalesced siblings) must ride
that event's metadata into the turn setup and out as the ``_run_agent``
``completion_delegation_ids`` argument -- and only for internal events: a human
message can never carry them, whatever its metadata says.
"""

import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource

SESSION_KEY = "agent:main:telegram:group:-1001:12345"


class _Adapter:
    """Minimal accepting adapter that records what the gateway injects."""

    def __init__(self):
        self.events = []

    async def handle_message(self, event):
        self.events.append(event)


def _bootstrap(monkeypatch, tmp_path):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    runner = gateway_run.GatewayRunner(GatewayConfig())
    adapter = _Adapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._begin_session_run_generation = lambda _key: 1
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key=SESSION_KEY,
        session_id="sess-b2",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store.update_session = MagicMock()
    runner._run_agent = AsyncMock(return_value={
        "final_response": "ack", "messages": [], "tools": [],
        "history_offset": 0, "last_prompt_tokens": 0,
    })

    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    return runner, adapter


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group", user_id="12345",
    )


def _completion(**over):
    evt = {
        "type": "async_delegation", "delegation_id": "deleg_a1",
        "platform": "telegram", "chat_type": "group", "chat_id": "-1001",
        "goal": "check the numbers", "status": "completed", "summary": "all good",
    }
    evt.update(over)
    return evt


async def _inject_then_run(runner, adapter, evt):
    accepted = await runner._inject_watch_notification("[helper finished]", evt)
    assert accepted is True
    (event,) = adapter.events
    runner.adapters = {}  # the turn itself needs no platform adapter
    await runner._handle_message_with_agent(event, _source(), SESSION_KEY, 1)
    return event, runner._run_agent.call_args.kwargs


@pytest.mark.asyncio
async def test_single_dispatch_completion_carries_its_id_into_the_turn(monkeypatch, tmp_path):
    runner, adapter = _bootstrap(monkeypatch, tmp_path)

    event, kwargs = await _inject_then_run(runner, adapter, _completion())

    assert event.internal is True
    assert kwargs["persist_user_display_kind"] == "internal_notification"
    assert list(kwargs["completion_delegation_ids"]) == ["deleg_a1"]


@pytest.mark.asyncio
async def test_batch_child_completion_carries_child_and_batch_ids(monkeypatch, tmp_path):
    runner, adapter = _bootstrap(monkeypatch, tmp_path)
    evt = _completion(delegation_id="deleg_b1:child:0", batch_delegation_id="deleg_b1")

    _event, kwargs = await _inject_then_run(runner, adapter, evt)

    assert set(kwargs["completion_delegation_ids"]) == {"deleg_b1:child:0", "deleg_b1"}


@pytest.mark.asyncio
async def test_coalesced_siblings_ids_ride_along(monkeypatch, tmp_path):
    runner, adapter = _bootstrap(monkeypatch, tmp_path)
    evt = _completion(_coalesced_siblings=[
        ("deleg:deleg_a2", "delegation", "second job"),
        ("proc:proc_9", "process", "make test"),
    ])

    _event, kwargs = await _inject_then_run(runner, adapter, evt)

    assert set(kwargs["completion_delegation_ids"]) == {"deleg_a1", "deleg_a2"}


@pytest.mark.asyncio
async def test_notification_text_is_never_parsed_for_ids(monkeypatch, tmp_path):
    runner, adapter = _bootstrap(monkeypatch, tmp_path)
    # A process completion: no delegation id anywhere but the free text.
    evt = {
        "type": "completion", "session_id": "proc_1", "command": "delegation_id=deleg_in_text",
        "platform": "telegram", "chat_type": "group", "chat_id": "-1001",
    }

    _event, kwargs = await _inject_then_run(runner, adapter, evt)

    assert not kwargs.get("completion_delegation_ids")


@pytest.mark.asyncio
async def test_a_human_event_cannot_carry_ids(monkeypatch, tmp_path):
    runner, _adapter = _bootstrap(monkeypatch, tmp_path)
    runner.adapters = {}
    human = MessageEvent(
        text="hello", source=_source(), message_id="m1", internal=False,
        metadata={"completion_delegation_ids": ["deleg_a1"]},
    )

    await runner._handle_message_with_agent(human, _source(), SESSION_KEY, 1)

    assert not runner._run_agent.call_args.kwargs.get("completion_delegation_ids")
