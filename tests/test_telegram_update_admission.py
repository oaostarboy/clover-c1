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


@pytest.mark.asyncio
async def test_crash_before_business_handler_does_not_poison_replay(tmp_path, monkeypatch):
    # Astra C13-ASTRA-01: admission must not be a completed receipt before the
    # update is durably handed off. Kill the process inside the business
    # handler; Telegram's replay to the restarted gateway must get through.
    import os
    import subprocess
    import sys
    from pathlib import Path

    home = tmp_path / "private-home"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    child = r'''
import asyncio, os
from pathlib import Path
from tests.test_telegram_update_admission import _adapter, _build, _text_update
async def main():
    adapter = _adapter(Path('/tmp'))
    app = _build(adapter, 111, [])
    async def die_before_business_logic(update, context):
        os._exit(73)
    app.handlers[0][0].callback = die_before_business_logic
    await app.initialize()
    await app.process_update(_text_update(app.bot, 1000))
asyncio.run(main())
'''
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(repo), os.environ.get("PYTHONPATH")])))
    proc = subprocess.run([sys.executable, "-c", child], env=env, cwd=repo,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 73, (proc.returncode, proc.stderr)
    # (Astra's original also asserted a receipt file existed here: that was the
    # bug's precondition. The fixed invariant is the replay outcome below.)
    for receipt in (home / "telegram").glob("telegram_update_receipts_*.json"):
        assert "1000" not in json.loads(receipt.read_text())["update_ids"]
    # There was no gateway turn, transcript, marker or reply. Telegram's
    # unacknowledged update is delivered to the replacement application.
    received = []
    app = _build(_adapter(tmp_path), 111, received)
    await app.initialize()
    try:
        await app.process_update(_text_update(app.bot, 1000))
        assert received == [1000], f"unanswered update discarded after restart: {received}"
    finally:
        await app.shutdown()


def _build_handing_off(adapter, bot_id, events):
    """Like _build, but the text handler builds the real MessageEvent and parks
    it (queued / not yet durable), as the real handlers do before the runner."""
    from gateway.platforms.base import MessageType

    app = (Application.builder().token(f"{bot_id}:offline-test")
           .request(OfflineRequest(bot_id)).get_updates_request(OfflineRequest(bot_id)).build())

    async def text_handler(update, context):
        events.append(adapter._build_message_event(
            update.message, MessageType.TEXT, update_id=update.update_id))

    adapter._handle_text_message = text_handler
    adapter._register_handlers(app)
    return app


@pytest.mark.asyncio
async def test_receipt_completes_only_on_durable_handoff(tmp_path):
    from gateway.platforms.base import complete_inbound_handoff

    events = []
    first = _adapter(tmp_path)
    app1 = _build_handing_off(first, 111, events)
    await app1.initialize()
    try:
        await app1.process_update(_text_update(app1.bot, 1100))
        # Same-process replay while the event is still in flight: dropped.
        await app1.process_update(_text_update(app1.bot, 1100))
    finally:
        await app1.shutdown()
    assert [e.platform_update_id for e in events] == [1100]

    # Restart before the handoff: the update was never processed -> replayed.
    replay = []
    app2 = _build_handing_off(_adapter(tmp_path), 111, replay)
    await _process(app2, _text_update(app2.bot, 1100))
    assert [e.platform_update_id for e in replay] == [1100]

    # Durable handoff (the runner's turn marker) completes the receipt...
    complete_inbound_handoff(replay[0])
    # ...so a later replay after another restart is dropped.
    after = []
    app3 = _build_handing_off(_adapter(tmp_path), 111, after)
    await _process(app3, _text_update(app3.bot, 1100))
    assert after == []


@pytest.mark.asyncio
async def test_merged_follow_up_completes_with_the_turn_it_joined(tmp_path):
    from gateway.platforms.base import (
        complete_inbound_handoff,
        merge_pending_message_event,
    )

    events = []
    adapter = _adapter(tmp_path)
    app = _build_handing_off(adapter, 111, events)
    await app.initialize()
    try:
        await app.process_update(_text_update(app.bot, 1200))
        await app.process_update(_text_update(app.bot, 1201))
    finally:
        await app.shutdown()
    pending = {}
    merge_pending_message_event(pending, "s", events[0], merge_text=True)
    merge_pending_message_event(pending, "s", events[1], merge_text=True)
    complete_inbound_handoff(pending["s"])
    later = []
    app2 = _build_handing_off(_adapter(tmp_path), 111, later)
    await app2.initialize()
    try:
        await app2.process_update(_text_update(app2.bot, 1200))
        await app2.process_update(_text_update(app2.bot, 1201))
    finally:
        await app2.shutdown()
    assert later == []


@pytest.mark.asyncio
async def test_update_that_builds_no_event_is_completed_by_the_final_group(tmp_path):
    # An update no handler turns into an event (ignored, unauthorized, ...) has
    # nothing to hand off, so its replay is dropped after a restart.
    first = _adapter(tmp_path)
    app1 = _build(first, 111, [])
    await _process(app1, _text_update(app1.bot, 1300))
    after = []
    app2 = _build_handing_off(_adapter(tmp_path), 111, after)
    await _process(app2, _text_update(app2.bot, 1300))
    # _build's handler built no event, so 1300 completed; the replay is dropped.
    assert after == []


def test_receipt_callbacks_survive_dataclasses_replace_and_run_once():
    import dataclasses

    from gateway.platforms.base import MessageEvent, complete_inbound_handoff

    calls = []
    event = MessageEvent(text="hi", inbound_receipts=[lambda: calls.append(1)])
    clone = dataclasses.replace(event, text="hi there")
    complete_inbound_handoff(clone)
    assert calls == [1]


@pytest.mark.asyncio
async def test_runner_turn_marker_completes_the_receipt(tmp_path):
    from gateway.run import GatewayRunner

    events = []
    app = _build_handing_off(_adapter(tmp_path), 111, events)
    await _process(app, _text_update(app.bot, 1400))

    class _Store:
        async def mark_turn_active(self, key):
            return "token"

    runner = object.__new__(GatewayRunner)
    runner._async_session_store = _Store()
    runner.session_store = _Store()
    runner.__class__ = type("R", (GatewayRunner,), {"async_session_store": property(lambda self: self._async_session_store)})
    assert await runner._mark_durable_active_turn(events[0], "sk")

    after = []
    app2 = _build_handing_off(_adapter(tmp_path), 111, after)
    await _process(app2, _text_update(app2.bot, 1400))
    assert after == []
