"""Exercise the real thought wrapper and Telegram send formatting; no network."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.stream_consumer import format_thought
from plugins.platforms.telegram.adapter import TelegramAdapter, _escape_mdv2

COMMENTARY = (
    "**not ready to push to users yet.** i just checked: the PR still has the "
    "fixture failure, and the real Windows Telegram path is still unverified.\n\n"
    "the fixture correction hasn’t been applied — that’s on me. i’m doing that "
    "now; it changes the test, not the updater’s safety logic."
)


@pytest.mark.asyncio
async def test_exact_commentary_send_preserves_bold_without_literal_delimiters():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)),
        send_chat_action=AsyncMock(),
    )
    adapter._bot = bot
    result = await adapter.send("123", format_thought(COMMENTARY), metadata={"_interim_send": True})
    assert result.success
    payload = bot.send_message.call_args.kwargs
    first, second = COMMENTARY.split("\n\n")
    bold, rest = first[2:].split("**", 1)
    expected = f"💭 _*{_escape_mdv2(bold)}*{_escape_mdv2(rest)}_\n\n_{_escape_mdv2(second)}_"
    assert payload["text"] == expected
    assert payload["parse_mode"] == "MarkdownV2"
    assert bot.send_message.call_count == 1


@pytest.mark.parametrize("text", ["*checking **bold** now*", "_checking **bold** now_"])
def test_existing_nested_emphasis_is_not_double_wrapped(text):
    formatted = TelegramAdapter.format_message(None, format_thought(text))
    assert formatted == "💭 _checking *bold* now_"


def test_identifiers_and_code_keep_literal_underscores():
    text = "**checking** cleanup_progress and `snake_case`"
    formatted = TelegramAdapter.format_message(None, format_thought(text))
    assert formatted == "💭 _*checking* cleanup\\_progress and `snake_case`_"
    assert TelegramAdapter.format_message(None, "cleanup_progress") == "cleanup\\_progress"


@pytest.mark.parametrize("text", ["__that__", "___that___", "____that____"])
def test_repeated_underscore_markers_remain_literal(text):
    assert TelegramAdapter.format_message(None, text) == _escape_mdv2(text)


@pytest.mark.asyncio
async def test_split_commentary_payloads_keep_emphasis():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter.MAX_MESSAGE_LENGTH = 180
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)),
        send_chat_action=AsyncMock(),
    )
    adapter._bot = bot
    text = "\n".join(["**checking** cleanup_progress and `snake_case`"] * 12)
    result = await adapter.send("123", format_thought(text), metadata={"_interim_send": True})
    assert result.success
    assert bot.send_message.call_count > 1
    for call in bot.send_message.call_args_list:
        assert call.kwargs["parse_mode"] == "MarkdownV2"
        assert "_*checking* cleanup\\_progress and `snake_case`_" in call.kwargs["text"]
        assert "\\_*" not in call.kwargs["text"]
