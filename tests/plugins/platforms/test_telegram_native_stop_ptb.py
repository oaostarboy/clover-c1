"""Native Stop intake against the REAL python-telegram-bot (22.x) types.

``tests/gateway`` mocks the ``telegram`` package; this directory does not, so
the raw ``stopped_message_generation`` update is deserialized by the real
``Update.de_json`` exactly as the polling/webhook loops would.
"""

import asyncio
from types import SimpleNamespace

import pytest

import telegram  # noqa: E402,F401  (the real library; this directory does not mock it)
from telegram import Update  # noqa: E402
from telegram.ext import Application, TypeHandler  # noqa: E402

from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402

RAW = {
    "update_id": 7,
    "stopped_message_generation": {"chat": {"id": 12345, "type": "private", "first_name": "x"}, "draft_id": 4242},
}


def make_adapter(native):
    return TelegramAdapter(PlatformConfig(
        enabled=True, token="123456:ABCdef", extra={"rich_messages": True, "native_progress": native},
    ))


def test_real_ptb_delivers_the_stop_event_only_in_api_kwargs():
    assert "stopped_message_generation" not in Update.ALL_TYPES
    update = Update.de_json(RAW, None)
    assert update.effective_chat is None and update.effective_user is None
    assert update.api_kwargs["stopped_message_generation"]["draft_id"] == 4242
    parsed = make_adapter(True)._raw_stopped_generation(update)
    assert parsed == {"chat_id": 12345, "chat_type": "private", "thread_id": None, "draft_id": 4242}


def test_allowed_updates_off_is_untouched_and_on_appends_exactly_the_stop_update():
    off = make_adapter(False)._allowed_update_types()
    assert off is Update.ALL_TYPES or list(off) == list(Update.ALL_TYPES)
    on = make_adapter(True)._allowed_update_types()
    assert list(on) == list(Update.ALL_TYPES) + ["stopped_message_generation"]


def test_dedicated_handler_group_only_when_native_progress_is_on():
    for native in (False, True):
        adapter = make_adapter(native)
        app = Application.builder().token("123456:ABCdef").build()
        adapter._register_handlers(app)
        assert 99 in app.handlers                       # existing catch-all untouched
        assert 98 in app.handlers if native else 98 not in app.handlers
        if native:
            [handler] = app.handlers[98]
            assert isinstance(handler, TypeHandler)
            assert handler.callback == adapter._on_stopped_message_generation


class _Updater:
    def __init__(self, fail=False):
        self.kwargs = None
        self.fail = fail

    async def start_polling(self, **kwargs):
        self.kwargs = kwargs
        if self.fail:
            raise RuntimeError("start failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_polling_subscribes_to_the_stop_update_and_only_then_offers_native(native):
    adapter = make_adapter(native)
    app = SimpleNamespace(updater=_Updater())
    await adapter._start_polling_once(
        app, drop_pending_updates=False, error_callback=lambda e: None, schedule_verifier=False,
    )
    sent = list(app.updater.kwargs["allowed_updates"])
    if native:
        assert sent == list(Update.ALL_TYPES) + ["stopped_message_generation"]
    else:
        assert sent == list(Update.ALL_TYPES)
    assert adapter._native_stop_ready is native


@pytest.mark.asyncio
async def test_failed_subscription_never_offers_native():
    adapter = make_adapter(True)
    app = SimpleNamespace(updater=_Updater(fail=True))
    with pytest.raises(Exception):
        await adapter._start_polling_once(
            app, drop_pending_updates=False, error_callback=lambda e: None, schedule_verifier=False,
        )
    assert adapter._native_stop_ready is False


def test_icon_selection_works_with_real_ptb_sticker_objects():
    from telegram import Sticker, StickerSet

    def sticker(emoji, cid, **kw):
        return Sticker(
            file_id=f"f{cid}", file_unique_id=f"u{cid}", width=1, height=1, is_animated=True,
            is_video=False, type="custom_emoji", emoji=emoji, custom_emoji_id=cid, **kw,
        )

    adapter = make_adapter(True)
    icons = adapter._select_native_icons(StickerSet(
        name="AIActions", title="t", sticker_type="custom_emoji",
        stickers=[sticker("\U0001F9E0", "7700000000000001"), sticker("⚙️", "7700000000000002")],
    ))
    assert icons["thinking"].custom_emoji_id == "7700000000000001"
    assert icons["running"].custom_emoji_id == "7700000000000002"
