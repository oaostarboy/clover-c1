"""edit_message()/delete_message() on a replaced Telegram adapter.

When the gateway rebuilds the Telegram adapter after a network failure, the
old instance is disconnected (``_bot = None``) and the new one is installed in
``runner.adapters``. Long-lived callbacks (a delegated worker's progress card,
stream editors) keep the old instance. ``send()`` already forwards to the
replacement; edits and deletes must do the same, otherwise those callbacks
silently write to a dead adapter.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._rich_send_disabled = True
    adapter._RECONNECT_WAIT_SECONDS = 30.0  # a hang would blow the test timeout
    return adapter


def _connected_bot() -> MagicMock:
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=7))
    bot.delete_message = AsyncMock(return_value=True)
    return bot


def _replaced_pair():
    old = _make_adapter()
    old._bot = None
    live = _make_adapter()
    live._bot = _connected_bot()
    runner = MagicMock()
    runner.adapters = {old.platform: live}
    old.gateway_runner = runner
    live.gateway_runner = runner
    return old, live


@pytest.mark.asyncio
async def test_edit_message_forwards_to_replacement_adapter():
    old, live = _replaced_pair()

    result = await asyncio.wait_for(old.edit_message("123", "7", "new text"), 5)

    assert result.success is True
    live._bot.edit_message_text.assert_awaited()


@pytest.mark.asyncio
async def test_edit_message_forwards_kwargs_to_replacement():
    old, live = _replaced_pair()
    live.edit_message = AsyncMock(return_value=MagicMock(success=True))

    await old.edit_message("123", "7", "txt", finalize=True, metadata={"thread_id": "9"})

    live.edit_message.assert_awaited_once_with(
        "123", "7", "txt", finalize=True, metadata={"thread_id": "9"},
    )


@pytest.mark.asyncio
async def test_delete_message_forwards_to_replacement_adapter():
    old, live = _replaced_pair()

    ok = await asyncio.wait_for(old.delete_message("123", "7"), 5)

    assert ok is True
    live._bot.delete_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_edit_message_without_replacement_fails_cleanly_without_waiting():
    adapter = _make_adapter()
    adapter._bot = None
    adapter.gateway_runner = MagicMock(adapters={})

    result = await asyncio.wait_for(adapter.edit_message("123", "7", "x"), 2)

    assert result.success is False
    assert result.error == "Not connected"


@pytest.mark.asyncio
async def test_delete_message_without_replacement_fails_cleanly_without_waiting():
    adapter = _make_adapter()
    adapter._bot = None
    adapter.gateway_runner = MagicMock(adapters={})

    assert await asyncio.wait_for(adapter.delete_message("123", "7"), 2) is False


@pytest.mark.asyncio
async def test_no_gateway_runner_fails_cleanly():
    adapter = _make_adapter()
    adapter._bot = None
    adapter.gateway_runner = None

    result = await asyncio.wait_for(adapter.edit_message("123", "7", "x"), 2)
    assert result.success is False
    assert await asyncio.wait_for(adapter.delete_message("123", "7"), 2) is False


@pytest.mark.asyncio
async def test_replacement_that_is_also_disconnected_is_not_used():
    """A replacement with no bot must not be forwarded to (no recursion)."""
    old, live = _replaced_pair()
    live._bot = None

    result = await asyncio.wait_for(old.edit_message("123", "7", "x"), 2)

    assert result.success is False
    assert result.error == "Not connected"
    assert await asyncio.wait_for(old.delete_message("123", "7"), 2) is False


@pytest.mark.asyncio
async def test_connected_adapter_does_not_forward():
    """The normal path is untouched: a connected adapter edits itself."""
    old, live = _replaced_pair()
    old._bot = _connected_bot()

    result = await old.edit_message("123", "7", "txt")

    assert result.success is True
    old._bot.edit_message_text.assert_awaited()
    live._bot.edit_message_text.assert_not_awaited()
