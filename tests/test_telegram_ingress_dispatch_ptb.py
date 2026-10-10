"""Real python-telegram-bot coverage for the dispatch counter behind the
"healthy but deaf" watchdog (the gateway tests run against a PTB mock)."""

import pytest

pytest.importorskip("telegram", reason="python-telegram-bot not installed")
from telegram import Update
from telegram.ext import Application

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as tg_adapter
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
async def test_real_ptb_application_counts_every_dispatched_update():
    assert tg_adapter.check_telegram_requirements()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="123456:abc"))
    adapter._begin_polling_generation()
    app = Application.builder().token("123456:abc").build()
    adapter._register_handlers(app)
    app._initialized = True  # initialize() would call getMe over the network
    update = Update.de_json({"update_id": 77}, app.bot)  # no message: no handler matches
    await app.process_update(update)
    assert adapter._updates_dispatched_total == 1
    # A replayed (already admitted) update is still dispatcher progress.
    await app.process_update(update)
    assert adapter._updates_dispatched_total == 2
