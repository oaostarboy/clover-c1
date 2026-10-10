"""Telegram update-ID admission across PTB dispatch, adapter rebuilds and restarts.

Telegram redelivers an update whose acknowledgement never landed (crash, reconnect,
adapter rebuild). The admission gate must answer it once. Ported in spirit from
NousResearch/hermes-agent tests/plugins/test_telegram_update_admission.py; the real
PTB Application and real Clover handler registration are used. Only the network
request is stubbed.
"""

import json

import pytest

pytest.importorskip("telegram")
from telegram import Update
from telegram.ext import Application
from telegram.request import BaseRequest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


class OfflineRequest(BaseRequest):
    def __init__(self, bot_id=111):
        self.bot_id = bot_id

    @property
    def read_timeout(self):
        return 1

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, *args, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        assert endpoint == "getMe", "no Telegram traffic allowed in tests"
        return 200, json.dumps({"ok": True, "result": {
            "id": self.bot_id, "is_bot": True, "first_name": "Offline", "username": "offline_bot",
        }}).encode()


def _text_update(bot, uid):
    return Update.de_json({
        "update_id": uid,
        "message": {
            "message_id": 7, "date": 1800000000,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 88, "is_bot": False, "first_name": "Human"},
            "text": "hello",
        },
    }, bot)


def _build(adapter, bot_id, seen):
    """A fresh PTB app wired exactly as Clover's connect()/rebuild path wires it."""
    app = (Application.builder().token(f"{bot_id}:offline-test")
           .request(OfflineRequest(bot_id)).get_updates_request(OfflineRequest(bot_id)).build())

    async def text_handler(update, context):
        seen.append(update.update_id)

    adapter._handle_text_message = text_handler
    adapter._register_handlers(app)
    return app


async def _process(app, update):
    await app.initialize()
    try:
        await app.process_update(update)
    finally:
        await app.shutdown()


def _adapter(tmp_path):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="111:offline-test", extra={}))
    return adapter


@pytest.mark.asyncio
async def test_same_update_twice_in_one_app_is_answered_once(tmp_path):
    adapter = _adapter(tmp_path)
    seen = []
    app = _build(adapter, 111, seen)
    await app.initialize()
    try:
        update = _text_update(app.bot, 500)
        await app.process_update(update)
        await app.process_update(update)
    finally:
        await app.shutdown()
    assert seen == [500]


@pytest.mark.asyncio
async def test_redelivery_after_adapter_rebuild_is_not_answered_again(tmp_path):
    # Reconnect watcher: a brand-new TelegramAdapter + Application for the same bot.
    first_seen, rebuilt_seen = [], []
    first = _adapter(tmp_path)
    app1 = _build(first, 111, first_seen)
    await _process(app1, _text_update(app1.bot, 501))
    assert first_seen == [501]

    rebuilt = _adapter(tmp_path)
    app2 = _build(rebuilt, 111, rebuilt_seen)
    await app2.initialize()
    try:
        # Telegram re-sends 501 because its acknowledgement never landed.
        await app2.process_update(_text_update(app2.bot, 501))
        await app2.process_update(_text_update(app2.bot, 502))
    finally:
        await app2.shutdown()
    assert rebuilt_seen == [502]


@pytest.mark.asyncio
async def test_redelivery_after_restart_reads_the_receipt_file(tmp_path):
    from plugins.platforms.telegram.adapter import _update_receipt_dir

    first_seen = []
    first = _adapter(tmp_path)
    app1 = _build(first, 111, first_seen)
    await _process(app1, _text_update(app1.bot, 900))
    assert first_seen == [900]

    receipts = list(_update_receipt_dir().glob("telegram_update_receipts_111*.json"))
    assert receipts, "completed update must be persisted to a per-bot receipt file"
    assert "900" in json.loads(receipts[0].read_text())["update_ids"]

    # Process restart: nothing in memory survives, only the receipt file does.
    restarted_seen = []
    restarted = _adapter(tmp_path)
    app2 = _build(restarted, 111, restarted_seen)
    await _process(app2, _text_update(app2.bot, 900))
    assert restarted_seen == []


@pytest.mark.asyncio
async def test_receipts_are_scoped_per_bot(tmp_path):
    # Same update_id from a different bot token is a different update stream.
    seen_a, seen_b = [], []
    a = _adapter(tmp_path)
    app_a = _build(a, 111, seen_a)
    await _process(app_a, _text_update(app_a.bot, 700))

    b = TelegramAdapter(PlatformConfig(enabled=True, token="222:offline-test", extra={}))
    app_b = _build(b, 222, seen_b)
    await _process(app_b, _text_update(app_b.bot, 700))
    assert seen_a == [700]
    assert seen_b == [700]


@pytest.mark.asyncio
async def test_new_telegram_update_after_week_idle_is_not_duplicate(tmp_path, monkeypatch):
    # Astra C13-ASTRA-05: the 24h receipt TTL must also bound the in-memory
    # lookup, or a recycled update_id after Telegram's week-idle reset is dropped.
    import plugins.platforms.telegram.update_admission as admission

    now = [1800000000.0]
    monkeypatch.setattr(admission.time, "time", lambda: now[0])
    adapter = _adapter(tmp_path)
    received = []
    app = _build(adapter, 111, received)
    await app.initialize()
    try:
        await app.process_update(_text_update(app.bot, 500))
        assert received == [500]
        # Telegram may choose a random starting update_id after a week idle.
        # This is a NEW message, with a colliding recycled update_id.
        now[0] += 8 * 24 * 60 * 60
        fresh = _text_update(app.bot, 500).to_dict()
        fresh["message"]["message_id"] = 999
        fresh["message"]["text"] = "a genuinely new question"
        fresh["message"]["date"] = int(now[0])
        await app.process_update(Update.de_json(fresh, app.bot))
        assert received == [500, 500], f"new message silently dropped: {received}"
    finally:
        await app.shutdown()
