"""Regression tests for Discord ``delete_message``.

The base adapter's ``delete_message`` stub always returns ``False`` — Discord
never overrode it, so the stream consumer's fresh-final cleanup path
(``cleanup_progress``) could not collect progress-bubble ids for Discord even
though the adapter already implements ``edit_message`` via the same
``get_partial_message`` channel-resolution path. This adds the override,
mirroring ``edit_message``'s channel resolution and the Telegram/Slack
``delete_message`` convention (best-effort, debug-log on failure, plain
``bool`` return).
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


def _make_adapter():
    return DiscordAdapter(PlatformConfig(enabled=True, token="***"))


class TestDeleteMessageHappyPath:
    @pytest.mark.asyncio
    async def test_deletes_via_partial_message(self):
        adapter = _make_adapter()
        deleted = []
        partial = SimpleNamespace(delete=AsyncMock(side_effect=lambda: deleted.append(True)))
        channel = SimpleNamespace(
            get_partial_message=MagicMock(return_value=partial),
        )
        adapter._client = SimpleNamespace(
            get_channel=lambda _cid: channel,
            fetch_channel=AsyncMock(),
        )

        result = await adapter.delete_message("555", "42")

        assert result is True
        assert deleted == [True]
        channel.get_partial_message.assert_called_once_with(42)

    @pytest.mark.asyncio
    async def test_falls_back_to_fetch_channel_when_uncached(self):
        adapter = _make_adapter()
        partial = SimpleNamespace(delete=AsyncMock())
        channel = SimpleNamespace(get_partial_message=MagicMock(return_value=partial))
        adapter._client = SimpleNamespace(
            get_channel=lambda _cid: None,
            fetch_channel=AsyncMock(return_value=channel),
        )

        result = await adapter.delete_message("555", "42")

        assert result is True
        adapter._client.fetch_channel.assert_awaited_once_with(555)


class TestDeleteMessageFailureIsNonFatal:
    @pytest.mark.asyncio
    async def test_not_connected_returns_false(self):
        adapter = _make_adapter()
        adapter._client = None

        assert await adapter.delete_message("555", "42") is False

    @pytest.mark.asyncio
    async def test_already_deleted_returns_false_not_raises(self):
        adapter = _make_adapter()
        partial = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("404 Not Found")))
        channel = SimpleNamespace(get_partial_message=MagicMock(return_value=partial))
        adapter._client = SimpleNamespace(
            get_channel=lambda _cid: channel,
            fetch_channel=AsyncMock(),
        )

        assert await adapter.delete_message("555", "42") is False

    @pytest.mark.asyncio
    async def test_missing_permission_returns_false_not_raises(self):
        adapter = _make_adapter()
        partial = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("403 Forbidden")))
        channel = SimpleNamespace(get_partial_message=MagicMock(return_value=partial))
        adapter._client = SimpleNamespace(
            get_channel=lambda _cid: channel,
            fetch_channel=AsyncMock(),
        )

        assert await adapter.delete_message("555", "42") is False

    @pytest.mark.asyncio
    async def test_channel_not_found_returns_false(self):
        adapter = _make_adapter()
        adapter._client = SimpleNamespace(
            get_channel=lambda _cid: None,
            fetch_channel=AsyncMock(side_effect=RuntimeError("Unknown Channel")),
        )

        assert await adapter.delete_message("555", "42") is False
