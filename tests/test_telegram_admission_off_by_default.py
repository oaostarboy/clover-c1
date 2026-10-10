"""Telegram update admission is OFF by default: update handling is C1.2's.

platforms.telegram.extra.update_admission defaults to false. Off, nothing is
claimed, remembered or written, so a redelivered update is handled again
(exactly like C1.2), whether the adapter was rebuilt, the process restarted, a
dispatch was cancelled, or a late observer failed (Astra round-8 scenarios).
"""

import asyncio

import pytest

pytest.importorskip("telegram")

# Imported first: later imports may stub ``telegram`` if it is not loaded.
from tests.test_telegram_update_admission import (
    OfflineRequest,
    _text_update,
)  # isort: skip

from telegram.ext import Application

from gateway.config import PlatformConfig
from gateway.platforms.base import (
    MessageType,
    complete_inbound_handoff,
    mark_inbound_durable,
    release_inbound_handoff,
    settle_inbound_handoff,
)
from plugins.platforms.telegram import update_admission
from plugins.platforms.telegram.adapter import TelegramAdapter, _update_receipt_dir


def _adapter(extra=None):
    return TelegramAdapter(PlatformConfig(enabled=True, token="111:offline-test", extra=extra or {}))


def _app(adapter, handler):
    app = (Application.builder().token("111:offline-test")
           .request(OfflineRequest(111)).get_updates_request(OfflineRequest(111)).build())
    adapter._handle_text_message = handler
    adapter._register_handlers(app)
    return app


def _handing_off(adapter, handled):
    async def handler(update, context):
        handled.append(update.update_id)
        event = adapter._build_message_event(
            update.message, MessageType.TEXT, update_id=update.update_id)
        mark_inbound_durable(event)
        complete_inbound_handoff(event)
        settle_inbound_handoff(event)
        release_inbound_handoff(event)

    return handler


async def _process(app, uid):
    await app.initialize()
    try:
        await asyncio.wait_for(app.process_update(_text_update(app.bot, uid)), 5)
    finally:
        await app.shutdown()


def _forget_process():
    update_admission._PROCESS_SEEN.clear()


@pytest.fixture(autouse=True)
def _clean():
    _forget_process()
    yield
    _forget_process()


def _receipt_files():
    d = _update_receipt_dir()
    return list(d.glob("telegram_update_receipts_*.json")) if d.exists() else []


def test_flag_defaults_to_off_and_durable_receipts_need_it():
    assert _adapter()._update_admission is False
    assert _adapter({"durable_update_receipts": True})._durable_update_receipts is False
    on = _adapter({"update_admission": True, "durable_update_receipts": True})
    assert on._update_admission and on._durable_update_receipts


@pytest.mark.asyncio
async def test_same_update_twice_in_one_app_is_handled_twice():
    a, handled = _adapter(), []
    app = _app(a, _handing_off(a, handled))
    await app.initialize()
    try:
        for _ in range(2):
            await app.process_update(_text_update(app.bot, 95000))
    finally:
        await app.shutdown()
    assert handled == [95000, 95000]


@pytest.mark.asyncio
async def test_redelivery_after_adapter_rebuild_is_handled_again():
    handled = []
    first = _adapter()
    await _process(_app(first, _handing_off(first, handled)), 95001)
    rebuilt = _adapter()
    await _process(_app(rebuilt, _handing_off(rebuilt, handled)), 95001)
    assert handled == [95001, 95001]


@pytest.mark.asyncio
async def test_redelivery_after_full_restart_is_handled_again():
    handled = []
    first = _adapter()
    await _process(_app(first, _handing_off(first, handled)), 95002)
    _forget_process()
    restarted = _adapter()
    await _process(_app(restarted, _handing_off(restarted, handled)), 95002)
    assert handled == [95002, 95002]


@pytest.mark.asyncio
async def test_existing_receipt_file_is_ignored():
    import json
    import time

    d = _update_receipt_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "telegram_update_receipts_111.json").write_text(
        json.dumps({"update_ids": {"95003": time.time()}}))
    a, handled = _adapter(), []
    await _process(_app(a, _handing_off(a, handled)), 95003)
    assert handled == [95003]


@pytest.mark.asyncio
async def test_no_admission_state_or_receipt_file_is_created():
    a, handled = _adapter(), []
    app = _app(a, _handing_off(a, handled))
    # No admission, finalizer or error handler is installed at all.
    assert -1 not in app.handlers and update_admission.FINALIZE_GROUP not in app.handlers
    assert not app.error_handlers
    await _process(app, 95004)
    assert handled == [95004]
    assert a._inflight_update_ids == {} and a._seen_update_ids == {}
    assert a._inflight_with_event == set() and a._inflight_failed == set()
    assert update_admission._PROCESS_SEEN == {}
    assert _receipt_files() == []


@pytest.mark.asyncio
async def test_events_carry_no_receipt_callbacks_and_handoff_calls_are_noops():
    a = _adapter()
    seen = []

    async def handler(update, context):
        seen.append(a._build_message_event(
            update.message, MessageType.TEXT, update_id=update.update_id))

    await _process(_app(a, handler), 95005)
    (event,) = seen
    assert not event.inbound_receipts
    for fn in (mark_inbound_durable, complete_inbound_handoff,
               settle_inbound_handoff, release_inbound_handoff):
        fn(event)
        fn(event)
    assert a._seen_update_ids == {} and update_admission._PROCESS_SEEN == {}
    assert _receipt_files() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before_event", "after_event"])
@pytest.mark.parametrize("rebuild", [False, True])
async def test_cancelled_dispatch_replay_is_handled(boundary, rebuild):
    """Astra round 8: a cancelled dispatch must not suppress the replay."""
    a, handled = _adapter(), []
    reached = asyncio.Event()

    replay_handler = []

    async def suspend_then_handle(update, context):
        if replay_handler:
            return await replay_handler[0](update, context)
        if boundary == "after_event":
            a._build_message_event(update.message, MessageType.TEXT, update_id=update.update_id)
        reached.set()
        await asyncio.Event().wait()

    app = _app(a, suspend_then_handle)
    await app.initialize()
    task = asyncio.create_task(app.process_update(_text_update(app.bot, 95006)))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert a._inflight_update_ids == {} and a._seen_update_ids == {}
        if rebuild:
            fresh = _adapter()
            await _process(_app(fresh, _handing_off(fresh, handled)), 95006)
        else:
            replay_handler.append(_handing_off(a, handled))
            await asyncio.wait_for(app.process_update(_text_update(app.bot, 95006)), 5)
        assert handled == [95006]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await app.shutdown()


@pytest.mark.asyncio
async def test_late_observer_error_cannot_suppress_replay():
    """Astra round 8: a group-99 failure after a completed handoff records nothing."""
    a, handled, errors = _adapter(), [], []
    app = _app(a, _handing_off(a, handled))

    async def observer(update, context):
        raise RuntimeError("test-owned group-99 observer failure")

    async def record(update, context):
        errors.append(context.error)

    app.handlers[99][0].callback = observer
    app.add_error_handler(record)
    await app.initialize()
    try:
        await app.process_update(_text_update(app.bot, 95007))
        await app.process_update(_text_update(app.bot, 95007))
    finally:
        await app.shutdown()
    assert len(errors) == 2 and all(isinstance(e, RuntimeError) for e in errors)
    assert handled == [95007, 95007]
    fresh = _adapter()
    await _process(_app(fresh, _handing_off(fresh, handled)), 95007)
    assert handled == [95007, 95007, 95007]
    assert a._seen_update_ids == {} and update_admission._PROCESS_SEEN == {}


@pytest.mark.asyncio
async def test_failing_handler_error_path_records_nothing():
    a, errors = _adapter(), []

    async def boom(update, context):
        raise OSError("test-owned handler failure")

    app = _app(a, boom)

    async def record(update, context):
        errors.append(context.error)

    app.add_error_handler(record)
    await _process(app, 95008)
    assert len(errors) == 1
    assert a._inflight_failed == set() and a._seen_update_ids == {}


@pytest.mark.asyncio
async def test_r07_watchdog_counts_dispatch_with_admission_off():
    a, handled = _adapter(), []
    assert a._update_admission is False
    app = _app(a, _handing_off(a, handled))
    before = getattr(a, "_updates_dispatched_total", 0)
    await app.initialize()
    try:
        await app.process_update(_text_update(app.bot, 95009))
        await app.process_update(_text_update(app.bot, 95009))  # a replay counts too
    finally:
        await app.shutdown()
    assert a._updates_dispatched_total == before + 2


@pytest.mark.asyncio
async def test_opt_in_still_drops_the_replay():
    """Control: the flag on restores the admission behaviour."""
    a, handled = _adapter({"update_admission": True}), []
    app = _app(a, _handing_off(a, handled))
    await app.initialize()
    try:
        for _ in range(2):
            await app.process_update(_text_update(app.bot, 95010))
    finally:
        await app.shutdown()
    assert handled == [95010]
