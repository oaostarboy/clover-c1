"""Durable Telegram update receipts are off by default (C1.3 review 6).

A receipt on disk can suppress the crash replay of input that only lived in
memory, and the replay is the only recovery for it. So by default the receipt
file is never read or written: after a crash a message may be answered twice
(C1.2 behaviour) but is never lost. Completed IDs are still remembered in
memory for the life of the process, across adapter rebuilds.
"""

import pytest

pytest.importorskip("telegram")

# Imported first: later imports may stub ``telegram`` if it is not loaded.
from tests.test_telegram_update_admission import (
    _adapter,
    _build_handing_off,
    _process,
    _text_update,
)  # isort: skip

from gateway.platforms.base import complete_inbound_handoff, settle_inbound_handoff
from plugins.platforms.telegram import update_admission
from plugins.platforms.telegram.adapter import _update_receipt_dir


def _full_restart():
    """Forget every in-memory admission map, as a new process would."""
    getattr(update_admission, "_PROCESS_SEEN", {}).clear()


async def _admit(tmp_path, uid, *, durable, events=None):
    events = [] if events is None else events
    adapter = _adapter(tmp_path, durable=durable)
    app = _build_handing_off(adapter, 111, events)
    await _process(app, _text_update(app.bot, uid))
    return events


async def _replay_after_restart(tmp_path, uid, *, durable=False):
    _full_restart()
    return await _admit(tmp_path, uid, durable=durable)


def _receipt_files():
    d = _update_receipt_dir()
    return list(d.glob("telegram_update_receipts_*.json")) if d.exists() else []


@pytest.mark.asyncio
async def test_completed_update_is_admitted_again_after_full_restart(tmp_path):
    events = await _admit(tmp_path, 91000, durable=False)
    from gateway.platforms.base import mark_inbound_durable

    mark_inbound_durable(events[0])
    complete_inbound_handoff(events[0])
    assert await _replay_after_restart(tmp_path, 91000), "C1.2: replay is admitted"


@pytest.mark.asyncio
async def test_no_receipt_file_is_created_by_default(tmp_path):
    from gateway.platforms.base import mark_inbound_durable

    events = await _admit(tmp_path, 91001, durable=False)
    mark_inbound_durable(events[0])
    complete_inbound_handoff(events[0])
    settle_inbound_handoff(events[0])
    assert _receipt_files() == []
    assert not _update_receipt_dir().exists() or not any(_update_receipt_dir().iterdir())


@pytest.mark.asyncio
async def test_existing_receipt_file_is_not_read_by_default(tmp_path):
    d = _update_receipt_dir()
    d.mkdir(parents=True, exist_ok=True)
    import json
    import time

    (d / "telegram_update_receipts_111.json").write_text(
        json.dumps({"update_ids": {"91002": time.time()}})
    )
    _full_restart()
    assert await _admit(tmp_path, 91002, durable=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["deny", "model", "new", "approve"])
async def test_control_command_crash_replay_is_admitted(tmp_path, command):
    """Astra round 6: /deny <reason>, /model confirm (and friends) mark the event
    handled while the real input only lived in memory. No receipt, so the
    replay after a crash is processed, not lost."""
    from gateway.run import GatewayRunner

    events = await _admit(tmp_path, 91010, durable=False)
    runner = object.__new__(GatewayRunner)
    runner._mark_command_receipt_safe(events[0], command)
    settle_inbound_handoff(events[0])
    assert _receipt_files() == []
    assert await _replay_after_restart(tmp_path, 91010)


@pytest.mark.asyncio
async def test_undo_replay_is_admitted_after_restart(tmp_path):
    """Astra round 6 (/undo).

    With destructive confirm off, a replayed /undo after a crash rewinds a
    second time. That is C1.2 behaviour and is the accepted bar here: a
    message may be handled twice after a crash, never lost. This test only
    documents that the replay is admitted; it does not assert the rewind count.
    """
    from gateway.run import GatewayRunner

    events = await _admit(tmp_path, 91020, durable=False)
    GatewayRunner._mark_command_receipt_safe(object.__new__(GatewayRunner), events[0], "undo")
    settle_inbound_handoff(events[0])
    assert await _replay_after_restart(tmp_path, 91020)


@pytest.mark.asyncio
async def test_same_process_adapter_rebuild_still_drops_completed_update(tmp_path):
    from gateway.platforms.base import mark_inbound_durable

    _full_restart()
    events = await _admit(tmp_path, 91030, durable=False)
    mark_inbound_durable(events[0])
    complete_inbound_handoff(events[0])
    # Reconnect watcher: a brand-new adapter, same process, no restart.
    assert await _admit(tmp_path, 91030, durable=False) == []
    assert _receipt_files() == []


@pytest.mark.asyncio
async def test_opt_in_still_writes_and_reads_receipts(tmp_path):
    from gateway.platforms.base import mark_inbound_durable

    _full_restart()
    events = await _admit(tmp_path, 91040, durable=True)
    mark_inbound_durable(events[0])
    complete_inbound_handoff(events[0])
    assert _receipt_files()
    _full_restart()
    assert await _admit(tmp_path, 91040, durable=True) == []


# --- C1.3 review 7: a failed or event-less update is never recorded as handled


def _real_text_handler(adapter, app):
    """Rewire ``app`` so the adapter's real _handle_text_message runs."""
    adapter._handle_text_message = type(adapter)._handle_text_message.__get__(adapter)
    app.handlers.clear()
    adapter._register_handlers(app)


def _permissive(adapter, enqueued):
    from unittest.mock import AsyncMock

    adapter._is_user_authorized_from_message = lambda msg: True
    adapter._should_process_message = lambda msg: True
    adapter._ensure_forum_commands = AsyncMock()
    adapter._cache_replied_media = AsyncMock()
    adapter._enqueue_text_event = enqueued.append


@pytest.mark.asyncio
async def test_failed_pre_event_handler_replay_survives_rebuild(tmp_path):
    _full_restart()
    first = _adapter(tmp_path, durable=False)
    enqueued, attempted, errors = [], [], []
    _permissive(first, enqueued)

    def transient_event_failure(*a, **kw):
        attempted.append(True)
        raise OSError("test-owned transient event construction failure")

    first._build_message_event = transient_event_failure
    app = _build_handing_off(first, 111, [])
    _real_text_handler(first, app)

    async def on_error(update, context):
        errors.append(context.error)

    app.add_error_handler(on_error)
    await _process(app, _text_update(app.bot, 91050))
    assert attempted == [True] and enqueued == []
    assert len(errors) == 1 and isinstance(errors[0], OSError)

    # Same process, failed adapter replaced; no full-restart reset here.
    fresh = _adapter(tmp_path, durable=False)
    _permissive(fresh, enqueued)
    app2 = _build_handing_off(fresh, 111, [])
    _real_text_handler(fresh, app2)
    await _process(app2, _text_update(app2.bot, 91050))
    assert enqueued, "unhandled input suppressed after rebuild"


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_handler_raising_after_event_marker_is_admitted_after_rebuild(tmp_path, durable):
    from gateway.platforms.base import MessageType

    _full_restart()
    first = _adapter(tmp_path, durable=durable)
    app = _build_handing_off(first, 111, [])

    async def raises_after_event(update, context):
        first._build_message_event(update.message, MessageType.TEXT, update_id=update.update_id)
        raise RuntimeError("fails before handoff")

    first._handle_text_message = raises_after_event
    app.handlers.clear()
    first._register_handlers(app)
    await _process(app, _text_update(app.bot, 91060))
    assert not _receipt_files()
    assert first._inflight_update_ids == {} and first._inflight_failed == set()
    assert await _admit(tmp_path, 91060, durable=durable), "replay must be admitted"


@pytest.mark.asyncio
async def test_update_without_handler_or_event_is_admitted_after_rebuild(tmp_path):
    from telegram import Update

    _full_restart()
    first = _adapter(tmp_path, durable=False)
    app = _build_handing_off(first, 111, [])
    poll = Update.de_json(
        {"update_id": 91070, "message": {
            "message_id": 9, "date": 1800000000,
            "chat": {"id": 42, "type": "group", "title": "g"},
            "from": {"id": 88, "is_bot": False, "first_name": "Human"},
            "new_chat_title": "renamed"}},
        app.bot,
    )
    await _process(app, poll)
    assert first._inflight_update_ids == {}
    fresh = _adapter(tmp_path, durable=False)
    app2 = _build_handing_off(fresh, 111, [])
    await _process(app2, Update.de_json(poll.to_dict(), app2.bot))
    # Admitted again and released: nothing is recorded as handled.
    assert fresh._inflight_update_ids == {}
    assert not any(k.endswith(":91070") for k in fresh._seen_update_ids)


@pytest.mark.asyncio
async def test_successful_text_message_still_deduped_across_rebuild(tmp_path):
    from gateway.platforms.base import mark_inbound_durable

    _full_restart()
    events = await _admit(tmp_path, 91080, durable=False)
    mark_inbound_durable(events[0])
    complete_inbound_handoff(events[0])
    assert await _admit(tmp_path, 91080, durable=False) == []
