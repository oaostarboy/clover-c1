"""Safety net for platforms.telegram.extra.drop_pending_on_cold_boot=false.

Preserving the Telegram backlog on cold boot (instead of Hermes's always-drop
default) means a control command sitting in that backlog can now be
*delivered* on the next boot where it previously would have been silently
discarded. These tests prove that delivery does not turn into a restart loop
for the three commands that matter: /restart, /update, /stop.
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.platforms.base import MessageEvent, MessageType
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _restart_event(update_id: int) -> MessageEvent:
    return MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m1",
        platform_update_id=update_id,
    )


@pytest.mark.asyncio
async def test_cold_boot_preserved_restart_redelivery_does_not_loop(tmp_path, monkeypatch):
    """The exact same /restart update_id, redelivered because the cold boot
    preserved the backlog instead of dropping it, must be suppressed rather
    than re-triggering another restart (which would re-arm the same
    redelivery on the next boot, an infinite loop — issue #18528)."""
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    marker = tmp_path / ".restart_last_processed.json"
    marker.write_text(json.dumps({
        "platform": "telegram",
        "update_id": 555,
        "requested_at": time.time() - 2,
    }))

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock()

    # Same update_id as the one the previous gateway already processed and
    # recorded — a literal Telegram redelivery, not just an older stray.
    event = _restart_event(update_id=555)
    result = await runner._handle_restart_command(event)

    assert result == ""
    runner.request_restart.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_queued_restart_after_offline_period_is_honored(tmp_path, monkeypatch):
    """A /restart that was genuinely queued while the gateway was offline (no
    prior marker at all) is a legitimate first-time command and must still
    run — preserving the backlog must not accidentally swallow real commands."""
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    event = _restart_event(update_id=42)
    result = await runner._handle_restart_command(event)

    assert "Restarting gateway" in result
    runner.request_restart.assert_called_once()


@pytest.mark.asyncio
async def test_cold_boot_preserved_stop_redelivery_is_idempotent():
    """A backlog-preserved /stop, delivered twice (e.g. Telegram redelivers
    it alongside other preserved updates), is naturally idempotent: it never
    restarts anything and never errors on a repeat call."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session = MagicMock(
        return_value=MagicMock(session_key="agent:main:telegram:dm:123:u1")
    )
    runner._is_user_authorized = lambda source: True
    runner._sibling_thread_run_keys = lambda source, key: []
    runner.adapters = {}
    runner._thread_metadata_for_source = lambda source, reply_to_message_id=None: None
    runner._reply_anchor_for_event = lambda event: None

    event = MessageEvent(
        text="/stop",
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m1",
        platform_update_id=555,
    )

    first = await runner._handle_stop_command(event)
    second = await runner._handle_stop_command(event)

    assert "no active" in str(getattr(first, "text", first)).lower()
    assert "no active" in str(getattr(second, "text", second)).lower()


@pytest.mark.asyncio
async def test_update_command_never_restarts_the_current_process_on_receipt(tmp_path, monkeypatch):
    """/update only spawns a detached updater and returns; it must never flip
    restart/drain state on the *current* process. That decoupling is what
    keeps a backlog-preserved, possibly-redelivered /update from compounding
    into the same self-perpetuating restart loop /restart guards against."""
    import subprocess

    from gateway.run import GatewayRunner
    import gateway.run as gw_run
    import clover_cli.config as clover_config

    monkeypatch.setattr(gw_run, "_clover_home", tmp_path)
    monkeypatch.setattr(gw_run, "_resolve_clover_bin", lambda: ["clover"])
    monkeypatch.setattr(clover_config, "is_managed", lambda: False)
    monkeypatch.setattr(subprocess, "Popen", MagicMock())

    runner = object.__new__(GatewayRunner)
    runner._restart_requested = False
    runner._draining = False
    runner._session_key_for_source = lambda source: "agent:main:telegram:dm:123:u1"
    runner._schedule_update_notification_watch = MagicMock()

    event = MessageEvent(
        text="/update",
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m1",
        platform_update_id=555,
    )

    for _ in range(2):  # simulate a redelivered backlog copy of the same command
        result = await runner._handle_update_command(event)
        assert isinstance(result, str)

    assert runner._restart_requested is False
    assert runner._draining is False
