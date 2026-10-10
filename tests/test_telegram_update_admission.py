"""Telegram update-ID admission across PTB dispatch, adapter rebuilds and restarts.

Telegram redelivers an update whose acknowledgement never landed (crash, reconnect,
adapter rebuild). The admission gate must answer it once. Ported in spirit from
NousResearch/hermes-agent tests/plugins/test_telegram_update_admission.py; the real
PTB Application and real Clover handler registration are used. Only the network
request is stubbed.
"""

import asyncio
import json
import threading

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
        # A successfully handled message: the event is built, then handed off.
        from gateway.platforms.base import (
            MessageType, complete_inbound_handoff, mark_inbound_durable,
        )

        seen.append(update.update_id)
        event = adapter._build_message_event(
            update.message, MessageType.TEXT, update_id=update.update_id)
        mark_inbound_durable(event)
        complete_inbound_handoff(event)

    adapter._handle_text_message = text_handler
    adapter._register_handlers(app)
    return app


async def _process(app, update):
    await app.initialize()
    try:
        await app.process_update(update)
    finally:
        await app.shutdown()


def _adapter(tmp_path, durable=True):
    """Durable receipts are opt-in; these tests exercise the opted-in mode."""
    extra = {"durable_update_receipts": True} if durable else {}
    return TelegramAdapter(PlatformConfig(enabled=True, token="111:offline-test", extra=extra))


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
async def test_update_that_builds_no_event_is_released_by_the_final_group(tmp_path):
    # An update no handler turns into an event (ignored, unauthorized, ...) has
    # nothing to hand off or answer twice: its claim is released and nothing is
    # recorded, so a replay is admitted (C1.3 review 7).
    first = _adapter(tmp_path)
    app1 = _build_handing_off(first, 111, [])

    async def builds_nothing(update, context):
        pass

    first._handle_text_message = builds_nothing
    app1.handlers.clear()
    first._register_handlers(app1)
    await _process(app1, _text_update(app1.bot, 1300))
    assert first._inflight_update_ids == {} and not first._seen_update_ids
    after = []
    app2 = _build_handing_off(_adapter(tmp_path), 111, after)
    await _process(app2, _text_update(app2.bot, 1300))
    assert [e.platform_update_id for e in after] == [1300]


def test_receipt_callbacks_survive_dataclasses_replace_and_run_once():
    import dataclasses

    from gateway.platforms.base import MessageEvent, complete_inbound_handoff

    calls = []
    event = MessageEvent(text="hi", inbound_receipts=[lambda: calls.append(1)])
    clone = dataclasses.replace(event, text="hi there")
    complete_inbound_handoff(clone)
    assert calls == [1]


def _marker_runner():
    from gateway.run import GatewayRunner

    class _Store:
        async def mark_turn_active(self, key, **kwargs):
            return "token"

    runner = object.__new__(GatewayRunner)
    runner._async_session_store = _Store()
    runner.session_store = _Store()
    runner.__class__ = type("R", (GatewayRunner,), {"async_session_store": property(lambda self: self._async_session_store)})
    return runner


@pytest.mark.asyncio
async def test_turn_marker_alone_does_not_complete_the_receipt(tmp_path):
    # The marker holds no text or media: until the user message is persisted a
    # crash must let Telegram's replay through (C13-ASTRA-01).
    events = []
    app = _build_handing_off(_adapter(tmp_path), 111, events)
    await _process(app, _text_update(app.bot, 1400))

    runner = _marker_runner()
    assert await runner._mark_durable_active_turn(events[0], "sk")

    replay = []
    app2 = _build_handing_off(_adapter(tmp_path), 111, replay)
    await _process(app2, _text_update(app2.bot, 1400))
    assert [e.platform_update_id for e in replay] == [1400]


@pytest.mark.asyncio
async def test_persisted_user_message_completes_the_receipt(tmp_path):
    events = []
    app = _build_handing_off(_adapter(tmp_path), 111, events)
    await _process(app, _text_update(app.bot, 1401))

    runner = _marker_runner()
    assert await runner._mark_durable_active_turn(events[0], "sk")
    # The agent's turn-start persist committed the user row.
    runner._on_inbound_persisted("sk")

    after = []
    app2 = _build_handing_off(_adapter(tmp_path), 111, after)
    await _process(app2, _text_update(app2.bot, 1401))
    assert after == []


@pytest.mark.asyncio
async def test_turn_end_without_persist_leaves_the_update_replayable(tmp_path):
    events = []
    app = _build_handing_off(_adapter(tmp_path), 111, events)
    await _process(app, _text_update(app.bot, 1402))

    runner = _marker_runner()
    assert await runner._mark_durable_active_turn(events[0], "sk")
    runner._forget_inbound_handoff(events[0])
    runner._on_inbound_persisted("sk")  # nothing left to release

    replay = []
    app2 = _build_handing_off(_adapter(tmp_path), 111, replay)
    await _process(app2, _text_update(app2.bot, 1402))
    assert [e.platform_update_id for e in replay] == [1402]


@pytest.mark.asyncio
async def test_abandoned_event_claim_bookkeeping_is_bounded(tmp_path):
    from types import SimpleNamespace

    from plugins.platforms.telegram import update_admission as adm

    adapter = _adapter(tmp_path)
    admit = adm.make_admission_handler(adapter, 111)
    # Dropped/cancelled pre-handoff events: no event object is retained here.
    for uid in range(adm._SEEN_CAP + 12):
        await admit(SimpleNamespace(update_id=uid), None)
        adm.attach_receipt(adapter, 111, uid, SimpleNamespace(inbound_receipts=[]))
    assert len(adapter._inflight_update_ids) <= adm._SEEN_CAP
    assert len(adapter._inflight_with_event) <= adm._SEEN_CAP
    # A set key never outlives its claim.
    assert adapter._inflight_with_event <= set(adapter._inflight_update_ids)


@pytest.mark.asyncio
async def test_completing_an_evicted_event_claim_discards_its_marker(tmp_path):
    from types import SimpleNamespace

    from plugins.platforms.telegram import update_admission as adm

    adapter = _adapter(tmp_path)
    admit = adm.make_admission_handler(adapter, 111)
    await admit(SimpleNamespace(update_id=1), None)
    event = SimpleNamespace(inbound_receipts=[])
    adm.attach_receipt(adapter, 111, 1, event)
    adapter._inflight_update_ids.clear()  # evicted by trim / TTL
    for receipt in event.inbound_receipts:
        receipt()
    assert "111:1" not in adapter._inflight_with_event


@pytest.mark.asyncio
async def test_marker_only_crash_leaves_update_replayable_or_text_durable(tmp_path, monkeypatch):
    """C13-ASTRA-01 on a real SQLite store: after a crash between the turn
    marker and the user-message write, the input is either replayed by
    Telegram or already in the transcript."""
    from gateway.run import GatewayRunner
    from gateway.session import AsyncSessionStore
    from tests.gateway.test_active_turn_recovery import _close_store_db, _make_db_store

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    events = []
    app = _build_handing_off(_adapter(tmp_path), 111, events)
    await _process(app, _text_update(app.bot, 1515))
    event = events[0]
    store = _make_db_store(home)
    entry = store.get_or_create_session(event.source)
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    assert await runner._mark_durable_active_turn(event, entry.session_key)
    _close_store_db(store)
    del runner, store, event, events

    store = _make_db_store(home)
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    try:
        await runner._recover_unclean_sessions()
        history = store.load_transcript(entry.session_id)
        replay = []
        app = _build_handing_off(_adapter(tmp_path), 111, replay)
        await _process(app, _text_update(app.bot, 1515))
        assert replay or any(
            m.get("role") == "user" and m.get("content") == "hello" for m in history
        )
    finally:
        _close_store_db(store)


@pytest.mark.asyncio
async def test_steered_input_is_replayable_until_persisted(tmp_path, monkeypatch):
    from gateway.platforms.base import build_session_key
    from tests.gateway.test_busy_session_ack import _make_runner
    from tests.gateway.test_active_turn_recovery import _make_db_store, _close_store_db
    from run_agent import AIAgent
    import gateway.run as gr

    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('CLOVER_HOME', str(home))
    monkeypatch.setenv('CLOVER_GATEWAY_BUSY_ACK_ENABLED', 'false')
    monkeypatch.setattr(gr, '_load_gateway_config', lambda: {})
    events = []
    adapter = _adapter(tmp_path)
    app = _build_handing_off(adapter, 111, events)
    await _process(app, _text_update(app.bot, 8000))
    event = events[0]
    event.text = 'please preserve this steering instruction'
    store = _make_db_store(home)
    entry = store.get_or_create_session(event.source)
    runner, _ = _make_runner()
    runner.session_store = store
    runner._busy_input_mode = 'steer'
    key = build_session_key(event.source)
    runner.adapters[event.source.platform] = adapter
    agent = object.__new__(AIAgent)
    agent._pending_steer = None
    agent._pending_steer_lock = threading.Lock()
    runner._running_agents[key] = agent
    adapter._active_sessions[key] = asyncio.Event()
    adapter._session_tasks[key] = asyncio.current_task()
    adapter._busy_session_handler = runner._handle_active_session_busy_message

    async def unexpected_turn(event):
        raise AssertionError('busy event must steer, not create another turn')

    adapter._message_handler = unexpected_turn
    try:
        await adapter.handle_message(event)
        assert agent._pending_steer == event.text
        assert store.load_transcript(entry.session_id) == []
        replay = []
        # Fresh adapter has no old in-memory agent/steer queue.
        app2 = _build_handing_off(_adapter(tmp_path), 111, replay)
        await _process(app2, _text_update(app2.bot, 8000))
        assert replay, ('steer only exists in memory, but durable receipt suppressed replay', adapter._seen_update_ids)
    finally:
        _close_store_db(store)


async def _steer_one(tmp_path, monkeypatch, update_id):
    """Busy-steer one real Telegram event; return (runner, key, agent, store, entry)."""
    from gateway.platforms.base import build_session_key
    from tests.gateway.test_busy_session_ack import _make_runner
    from tests.gateway.test_active_turn_recovery import _make_db_store
    from run_agent import AIAgent
    import gateway.run as gr

    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    monkeypatch.setenv('CLOVER_HOME', str(home))
    monkeypatch.setenv('CLOVER_GATEWAY_BUSY_ACK_ENABLED', 'false')
    monkeypatch.setattr(gr, '_load_gateway_config', lambda: {})
    events = []
    adapter = _adapter(tmp_path)
    app = _build_handing_off(adapter, 111, events)
    await _process(app, _text_update(app.bot, update_id))
    event = events[0]
    event.text = 'steer me'
    store = _make_db_store(home)
    store.get_or_create_session(event.source)
    runner, _ = _make_runner()
    runner.session_store = store
    runner._busy_input_mode = 'steer'
    key = build_session_key(event.source)
    runner.adapters[event.source.platform] = adapter
    agent = object.__new__(AIAgent)
    agent._pending_steer = None
    agent._pending_steer_lock = threading.Lock()
    runner._running_agents[key] = agent
    adapter._active_sessions[key] = asyncio.Event()
    adapter._session_tasks[key] = asyncio.current_task()
    adapter._busy_session_handler = runner._handle_active_session_busy_message

    async def unexpected_turn(event):
        raise AssertionError('busy event must steer, not create another turn')

    adapter._message_handler = unexpected_turn
    await adapter.handle_message(event)
    assert agent._pending_steer == event.text
    return runner, key, agent, store


async def _replayed(tmp_path, update_id):
    replay = []
    app = _build_handing_off(_adapter(tmp_path), 111, replay)
    await _process(app, _text_update(app.bot, update_id))
    return replay


@pytest.mark.asyncio
async def test_steered_input_never_gets_a_receipt_even_after_a_clean_turn(tmp_path, monkeypatch):
    """Busy-path inputs are never receipted, whatever the turn does next."""
    from tests.gateway.test_active_turn_recovery import _close_store_db

    runner, key, agent, store = await _steer_one(tmp_path, monkeypatch, 8100)
    try:
        assert not hasattr(runner, "_steered_inbound_events")
        agent._pending_steer = None  # the loop consumed it into a tool result
        assert await _replayed(tmp_path, 8100)
    finally:
        _close_store_db(store)


@pytest.mark.asyncio
async def test_steer_then_failed_final_persist_leaves_no_receipt(tmp_path, monkeypatch):
    """Accepted steer + final _persist_session failure: no receipt, replay admitted."""
    from agent.prompt_builder import format_steer_marker
    from agent.turn_finalizer import finalize_turn
    from tests.agent.test_turn_finalizer_cleanup_guard import _StubAgent
    from tests.gateway.test_active_turn_recovery import _close_store_db

    runner, key, agent, store = await _steer_one(tmp_path, monkeypatch, 9200)
    try:
        stub = _StubAgent(raise_in=("persist_session",))
        messages = [
            {"role": "user", "content": "initial task"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1",
             "content": "tool output" + format_steer_marker("steer me")},
            {"role": "assistant", "content": "answered"},
        ]
        result = finalize_turn(
            stub, final_response="answered", api_call_count=2, interrupted=False,
            failed=False, messages=messages, conversation_history=None,
            effective_task_id="t", turn_id="u", user_message="initial task",
            original_user_message="initial task", _should_review_memory=False,
            _turn_exit_reason="completed",
        )
        assert result["failed"] is False
        assert any(i.startswith("persist_session:") for i in result["cleanup_errors"])
        assert await _replayed(tmp_path, 9200)
    finally:
        _close_store_db(store)


@pytest.mark.asyncio
async def test_early_steer_late_steer_and_queued_media_are_all_unreceipted(tmp_path, monkeypatch):
    """Answered early steer + leftover steer + queued image: no busy event is receipted,
    and an ordinary (non-busy) event still receipts exactly as before."""
    from gateway.platforms.base import MessageType, build_session_key, complete_inbound_handoff
    from tests.gateway.test_active_turn_recovery import _close_store_db, _make_db_store
    from tests.gateway.test_busy_session_ack import _make_runner
    from run_agent import AIAgent
    import gateway.run as gr

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    monkeypatch.setenv("CLOVER_GATEWAY_BUSY_ACK_ENABLED", "false")
    monkeypatch.setattr(gr, "_load_gateway_config", lambda: {})
    adapter = _adapter(tmp_path)
    events = []
    app = _build_handing_off(adapter, 111, events)
    for uid in (9100, 9101, 9102, 9103):
        await _process(app, _text_update(app.bot, uid))
    early, late, media, normal = events
    early.text, late.text, media.text = "early", "late", "queued"
    media.message_type = MessageType.PHOTO
    media.media_urls, media.media_types = ["/nonexistent.png"], ["image/png"]
    store = _make_db_store(home)
    store.get_or_create_session(early.source)
    runner, _ = _make_runner()
    runner.session_store = store
    key = build_session_key(early.source)
    runner.adapters[early.source.platform] = adapter
    agent = object.__new__(AIAgent)
    agent._pending_steer = None
    agent._pending_steer_lock = threading.Lock()
    runner._running_agents[key] = agent
    adapter._active_sessions[key] = asyncio.Event()
    adapter._session_tasks[key] = asyncio.current_task()
    adapter._busy_session_handler = runner._handle_active_session_busy_message
    try:
        runner._busy_input_mode = "steer"
        for ev in (early, late, media):
            assert await runner._handle_active_session_busy_message(ev, key)
        # Settling the turn (answered / leftover / queued) must not receipt anything.
        for uid in (9100, 9101, 9102):
            assert not adapter._inflight_update_ids.get(f"111:{uid}")
            assert f"111:{uid}" not in adapter._seen_update_ids
            assert await _replayed(tmp_path, uid)
        # Normal path: the ordinary event is receipted by its handoff, as before.
        assert f"111:9103" in adapter._inflight_update_ids
        complete_inbound_handoff(normal)
        assert "111:9103" in adapter._seen_update_ids
        assert await _replayed(tmp_path, 9103) == []
    finally:
        _close_store_db(store)


@pytest.mark.asyncio
async def test_busy_steers_release_their_claims_and_hold_nothing(tmp_path):
    """5000 busy steers in one turn: no claim left behind, no held list anywhere."""
    from gateway.platforms.base import release_inbound_handoff
    from gateway.run import GatewayRunner

    adapter = _adapter(tmp_path)
    events = []
    app = _build_handing_off(adapter, 111, events)
    runner = object.__new__(GatewayRunner)
    peak = 0
    for offset in range(5000):
        await _process(app, _text_update(app.bot, 30000 + offset))
        peak = max(peak, len(adapter._inflight_update_ids))
        release_inbound_handoff(events[-1])
        events.clear()
    from plugins.platforms.telegram.update_admission import _SEEN_CAP
    assert peak <= _SEEN_CAP
    assert adapter._inflight_update_ids == {}
    assert adapter._inflight_with_event == set()
    assert adapter._seen_update_ids == {}
    assert not any(n.startswith("_steered") for n in runner.__dict__)
    assert not hasattr(GatewayRunner, "_hold_inbound_for_active_turn")
    # And the telegram receipt file was never written for any of them.
    assert not list(adapter._update_receipt_dir.glob("*.json")) or not json.loads(
        next(adapter._update_receipt_dir.glob("*.json")).read_text())["update_ids"]


@pytest.mark.asyncio
async def test_handler_that_parks_the_event_in_memory_leaves_it_replayable(tmp_path):
    """PRIORITY busy path: the runner returns after only queueing the event."""
    events = []
    adapter = _adapter(tmp_path)
    app = _build_handing_off(adapter, 111, events)
    await _process(app, _text_update(app.bot, 8300))
    parked = events[0]

    async def handler(event):
        from gateway.platforms.base import release_inbound_handoff
        release_inbound_handoff(event)  # what the runner does for a queued busy event
        return None

    adapter._message_handler = handler
    await adapter._process_message_background(parked, "k")
    assert await _replayed(tmp_path, 8300)
