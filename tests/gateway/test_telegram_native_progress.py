"""Native Telegram activity display (opt-in ``platforms.telegram.extra.native_progress``).

These tests drive the production ``TelegramAdapter`` over an in-memory Bot API
connector (``FakeTelegramApi``).  Nothing about the adapter, stream consumer or
gateway runner is replaced.
"""

import logging
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway._telegram_fake_api import FakeTelegramApi


def make_adapter(**extra):
    """Real TelegramAdapter wired to the in-memory connector."""
    extra.setdefault("rich_messages", True)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra=extra))
    api = FakeTelegramApi()
    adapter._bot = api
    return adapter, api


def row(text, *, kind="tool", tool="web_search", state="running", started_at=0.0,
        duration=None, repeat=1):
    """Duck-typed activity row (the adapter renderer reads attributes only)."""
    return SimpleNamespace(
        text=text, kind=kind, tool=tool, state=state,
        started_at=started_at, duration=duration, repeat=repeat,
    )


# ── Config / option matrix ──────────────────────────────────────────────────


def test_native_progress_is_off_by_default():
    adapter, _ = make_adapter()
    assert adapter._native_progress_enabled is False
    adapter._native_stop_ready = True
    assert adapter.supports_native_progress(chat_type="dm") is False


def test_native_progress_default_is_documented_in_config_defaults():
    from clover_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["telegram"]["extra"]["native_progress"] is False


@pytest.mark.parametrize(
    "native, rich_messages, rich_drafts, expected",
    [
        (False, False, False, False),
        (False, True, False, False),
        (False, True, True, False),
        (True, False, False, False),
        (True, False, True, False),
        (True, True, False, True),
        (True, True, True, True),
    ],
)
def test_option_matrix_never_mutates_existing_rich_flags(native, rich_messages, rich_drafts, expected):
    adapter, _ = make_adapter(
        native_progress=native, rich_messages=rich_messages, rich_drafts=rich_drafts,
    )
    adapter._native_stop_ready = True
    assert adapter.supports_native_progress(chat_type="dm") is expected
    assert adapter._rich_messages_enabled is rich_messages
    assert adapter._rich_drafts_enabled is rich_drafts


def test_native_progress_inert_without_rich_messages_logs_once(caplog):
    adapter, _ = make_adapter(native_progress=True, rich_messages=False)
    adapter._native_stop_ready = True
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            assert adapter.supports_native_progress(chat_type="dm") is False
    notices = [r for r in caplog.records if "native_progress" in r.getMessage()]
    assert len(notices) == 1


def test_native_progress_requires_working_stop_path():
    adapter, _ = make_adapter(native_progress=True)
    # Stop subscription/auth not wired -> no native composer at all.
    assert adapter._native_stop_ready is False
    assert adapter.supports_native_progress(chat_type="dm") is False
    adapter._native_stop_ready = True
    assert adapter.supports_native_progress(chat_type="dm") is True


@pytest.mark.parametrize(
    "chat_type, metadata",
    [
        ("group", None),
        ("supergroup", None),
        ("forum", {"thread_id": "7"}),
        ("dm", {"thread_id": "7"}),
        ("dm", {"direct_messages_topic_id": "9"}),
        ("dm", {"telegram_dm_topic_reply_fallback": True, "thread_id": "5"}),
        ("channel", None),
        (None, None),
    ],
)
def test_native_progress_only_for_plain_private_chats(chat_type, metadata):
    adapter, _ = make_adapter(native_progress=True)
    adapter._native_stop_ready = True
    assert adapter.supports_native_progress(chat_type=chat_type, metadata=metadata) is False


def test_native_progress_requires_rich_capable_bot():
    adapter, _ = make_adapter(native_progress=True)
    adapter._native_stop_ready = True
    adapter._bot = SimpleNamespace(send_message_draft=lambda **kw: None)  # no async do_api_request
    assert adapter.supports_native_progress(chat_type="dm") is False


# ── Official payload ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_native_draft_uses_official_rich_draft_payload_with_can_stop():
    adapter, api = make_adapter(native_progress=True)  # rich_drafts left at its default (off)
    adapter._native_stop_ready = True

    result = await adapter.send_native_progress_draft(
        "12345", 4242,
        [row("🔍 Searching the web for sony reviews", started_at=0.0)],
        "partial **answer**",
        now=6.0,
    )

    assert result.success is True and result.message_id is None
    [frame] = api.rich_drafts()
    assert set(frame) == {"chat_id", "draft_id", "rich_message", "can_stop"}
    assert frame["chat_id"] == 12345
    assert frame["draft_id"] == 4242
    assert frame["can_stop"] is True
    assert set(frame["rich_message"]) == {"markdown"}
    md = frame["rich_message"]["markdown"]
    assert md.startswith("<tg-thinking>") and md.count("<tg-thinking>") == 1
    head, _, tail = md.partition("</tg-thinking>")
    assert "Searching the web for sony reviews" in head
    assert tail.strip() == "partial **answer**"
    # Only the ephemeral composer opted into the draft endpoint; flags untouched.
    assert adapter._rich_drafts_enabled is False
    assert adapter._rich_messages_enabled is True
    # No legacy plain draft or persistent send happened.
    assert api.methods("send_message_draft") == []
    assert api.methods("send_message") == []


@pytest.mark.asyncio
async def test_native_draft_escapes_markup_and_preserves_unicode():
    adapter, api = make_adapter(native_progress=True)
    adapter._native_stop_ready = True
    nasty = '⚙️ run <b>x</b> & "q" \'s\' </tg-thinking><tg-emoji emoji-id="1">x</tg-emoji> 你好 🚀'

    await adapter.send_native_progress_draft("1", 5, [row(nasty)], "", now=1.0)

    md = api.rich_drafts()[0]["rich_message"]["markdown"]
    # User text can never terminate (or open) the real block: one real pair only.
    assert md.startswith("<tg-thinking>")
    assert md.count("<tg-thinking>") == 1 and md.count("</tg-thinking>") == 1
    assert md.endswith("</tg-thinking>")
    assert "&lt;b&gt;x&lt;/b&gt; &amp;" in md
    assert "&lt;/tg-thinking&gt;" in md and "&lt;tg-emoji" in md
    assert "<tg-emoji" not in md and "<b>" not in md
    assert "你好 🚀" in md


@pytest.mark.asyncio
async def test_native_draft_rejects_zero_draft_id_and_oversize_frames_without_latching():
    adapter, api = make_adapter(native_progress=True)
    adapter._native_stop_ready = True

    zero = await adapter.send_native_progress_draft("1", 0, [row("x")], "", now=1.0)
    huge = await adapter.send_native_progress_draft("1", 9, [row("y" * 40000)], "", now=1.0)

    assert zero.success is False and huge.success is False
    assert api.methods("do_api_request:sendRichMessageDraft") == []
    assert adapter._native_progress_disabled is False


@pytest.mark.asyncio
async def test_native_capability_failure_latches_only_native_progress():
    adapter, api = make_adapter(native_progress=True)
    adapter._native_stop_ready = True
    api.fail["sendRichMessageDraft"] = type("EndPointNotFound", (Exception,), {})("no such method")

    result = await adapter.send_native_progress_draft("1", 9, [row("x")], "", now=1.0)

    assert result.success is False
    assert adapter._native_progress_disabled is True
    assert adapter._rich_draft_disabled is False
    assert adapter._rich_send_disabled is False
    assert adapter.supports_native_progress(chat_type="dm") is False


@pytest.mark.asyncio
async def test_native_draft_refuses_when_feature_off_and_old_draft_path_is_unchanged():
    adapter, api = make_adapter()  # native_progress off
    adapter._native_stop_ready = True

    refused = await adapter.send_native_progress_draft("1", 9, [row("x")], "", now=1.0)
    assert refused.success is False
    assert api.calls == []

    # Old path: plain legacy draft, byte-for-byte what it sent before this feature.
    result = await adapter.send_draft("12345", 7, "hello", None)
    assert result.success is True
    [call] = api.methods("send_message_draft")
    assert call["chat_id"] == 12345 and call["draft_id"] == 7
    assert "can_stop" not in call
    assert api.methods("get_sticker_set") == []
