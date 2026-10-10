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
