"""Native Stop subscription through the real ``connect()``: polling, webhook, reconnect.

Reuses the lifecycle doubles of ``test_telegram_polling_progress`` (fake PTB
Application/Updater; everything else in ``TelegramAdapter.connect`` is real).
"""

from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as tg_adapter
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway.test_telegram_polling_progress import (
    _configure_lifecycle_connect, _lifecycle_app,
)

STOP = "stopped_message_generation"


def make_adapter(native):
    return TelegramAdapter(PlatformConfig(
        enabled=True, token="test-token", extra={"rich_messages": True, "native_progress": native},
    ))


def polling_app(adapter):
    app = _lifecycle_app()

    async def start_polling_with_progress(**_kwargs):
        adapter._record_polling_progress(adapter._polling_generation)

    app.updater.start_polling = AsyncMock(side_effect=start_polling_with_progress)
    return app


def handler_groups(app):
    groups = []
    for call in app.add_handler.call_args_list:
        groups.append(call.kwargs.get("group", 0))
    return groups


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_polling_connect_subscribes_only_when_native_is_on(monkeypatch, native):
    adapter = make_adapter(native)
    app = polling_app(adapter)
    _configure_lifecycle_connect(monkeypatch, adapter, [app])
    monkeypatch.delenv("TELEGRAM_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)

    assert await adapter.connect() is True
    sent = app.updater.start_polling.await_args.kwargs["allowed_updates"]
    if native:
        assert list(sent) == list(tg_adapter.Update.ALL_TYPES) + [STOP]
        assert 98 in handler_groups(app) and 99 in handler_groups(app)
    else:
        assert sent is tg_adapter.Update.ALL_TYPES            # byte-for-byte today's subscription
        assert 98 not in handler_groups(app)
    assert adapter._native_stop_ready is native
    await adapter.disconnect()
    assert adapter._native_stop_ready is False


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_webhook_connect_subscribes_only_when_native_is_on(monkeypatch, native):
    adapter = make_adapter(native)
    app = _lifecycle_app()
    _configure_lifecycle_connect(monkeypatch, adapter, [app])
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://example.test/telegram")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-secret")

    assert await adapter.connect() is True
    sent = app.updater.start_webhook.await_args.kwargs["allowed_updates"]
    if native:
        assert list(sent) == list(tg_adapter.Update.ALL_TYPES) + [STOP]
        assert 98 in handler_groups(app)
    else:
        assert sent is tg_adapter.Update.ALL_TYPES
        assert 98 not in handler_groups(app)
    assert adapter._native_stop_ready is native
    await adapter.disconnect()
    assert adapter._native_stop_ready is False


@pytest.mark.asyncio
async def test_reconnect_resubscribes_and_a_failed_start_never_offers_native(monkeypatch):
    adapter = make_adapter(True)
    first, second = polling_app(adapter), polling_app(adapter)
    _configure_lifecycle_connect(monkeypatch, adapter, [first, second])
    monkeypatch.delenv("TELEGRAM_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)

    assert await adapter.connect() is True and adapter._native_stop_ready is True
    await adapter.disconnect()
    assert adapter._native_drafts == {} and adapter._native_stop_ready is False
    assert await adapter.connect(is_reconnect=True) is True
    assert list(second.updater.start_polling.await_args.kwargs["allowed_updates"])[-1] == STOP
    assert adapter._native_stop_ready is True
    await adapter.disconnect()
