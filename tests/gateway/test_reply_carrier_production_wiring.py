"""Production wiring and forced-cleanup seams for reply-drain ownership."""
import asyncio

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platform_registry import platform_registry
from plugins.platforms.photon.adapter import PhotonAdapter
from tests.gateway.restart_test_helpers import make_restart_runner
from tests.gateway.test_delivery_ledger_producer import _Adapter, _event


def test_builtin_factory_injects_runner_for_reply_tracking(monkeypatch):
    runner, _ = make_restart_runner()
    monkeypatch.setattr(platform_registry, "is_registered", lambda _: False)
    # This is the real built-in factory and actual adapter constructor, not a
    # patched return value. No connection or credential-bearing call occurs.
    adapter = runner._create_adapter(Platform.WHATSAPP_CLOUD, PlatformConfig(enabled=True))
    assert adapter is not None
    assert adapter.gateway_runner is runner


@pytest.mark.asyncio
async def test_photon_actual_retry_override_tracks_inline_send():
    adapter = PhotonAdapter(PlatformConfig(enabled=True))
    runner, _ = make_restart_runner(adapter)
    adapter.gateway_runner = runner
    runner._active_cron_job_count = lambda: 0
    runner._active_api_run_count = lambda: 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        return SendResult(success=True, message_id="photon-ack")

    adapter.send = send
    carrier = asyncio.create_task(adapter._send_with_retry("chat", "restart acknowledgement"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert runner._active_work_count() == 1
        release.set()
        assert (await asyncio.wait_for(carrier, 5)).success
        assert runner._active_work_count() == 0
    finally:
        release.set()
        await asyncio.gather(carrier, return_exceptions=True)


@pytest.mark.asyncio
async def test_forced_cleanup_cancels_retired_runner_owned_carrier():
    adapter = _Adapter()
    runner, _ = make_restart_runner(adapter)
    adapter.gateway_runner = runner
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        return SendResult(success=True, message_id="ack")

    adapter.send = send
    carrier = asyncio.create_task(adapter._send_with_retry("chat", "reply"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        runner.adapters.clear()  # The retired owner is no longer discoverable.
        assert carrier in runner._reply_delivery_tasks
        await runner._cancel_reply_delivery_tasks()
        assert carrier.cancelled()
        assert not runner._reply_delivery_tasks
    finally:
        release.set()
        await asyncio.gather(carrier, return_exceptions=True)


@pytest.mark.asyncio
async def test_forced_cleanup_deadline_retains_cancellation_suppressing_owner():
    adapter = _Adapter()
    runner, _ = make_restart_runner(adapter)
    adapter.gateway_runner = runner
    runner._adapter_disconnect_timeout_secs = lambda: .01
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()  # An uncooperative transport must not pin stop.
        return SendResult(success=True, message_id="late-ack")

    adapter.send = send
    carrier = asyncio.create_task(adapter._send_with_retry("chat", "reply"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        runner.adapters.clear()
        await asyncio.wait_for(runner._cancel_reply_delivery_tasks(), .5)
        assert not carrier.done()
        assert carrier in runner._reply_delivery_tasks
        release.set()
        await asyncio.wait_for(carrier, 5)
        assert not runner._reply_delivery_tasks
    finally:
        release.set()
        await asyncio.gather(carrier, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_carrier_after_cleanup_inherits_cancellation():
    """Cleanup cannot reopen admission when the initial snapshot was empty."""
    adapter = _Adapter()
    runner, _ = make_restart_runner(adapter)
    adapter.gateway_runner = runner
    await runner._cancel_reply_delivery_tasks()
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        return SendResult(success=True, message_id="ack")

    adapter.send = send
    carrier = asyncio.create_task(adapter._send_with_retry("chat", "late reply"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.sleep(0)
        assert carrier.cancelled(), "late carrier escaped forced cleanup admission"
        assert not runner._reply_delivery_tasks
    finally:
        release.set()
        await asyncio.gather(carrier, return_exceptions=True)


@pytest.mark.asyncio
async def test_forced_retired_cleanup_does_not_leave_a_spawned_followup(tmp_path, monkeypatch):
    """Independent-review counterexample: cancelled finally spawns a new owner."""
    from gateway import delivery_ledger as dl
    monkeypatch.setattr(dl, "_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(dl, "ledger_enabled", lambda: True)
    adapter = _Adapter()
    runner, _ = make_restart_runner(adapter)
    adapter.gateway_runner = runner
    runner._draining = True
    first_send, next_handler, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def handler(event):
        nonlocal calls
        calls += 1
        if calls > 1:
            next_handler.set()
        return "gateway draining" if calls > 1 else "final reply"

    async def send(*args, **kwargs):
        first_send.set()
        await release.wait()
        return SendResult(success=True, message_id="ack")

    adapter.send = send
    adapter._message_handler = handler
    key = "agent:main:slack:channel:C1"
    adapter._active_sessions[key] = asyncio.Event()
    carrier = asyncio.create_task(adapter._process_message_background(_event(), key))
    adapter._session_tasks[key] = carrier
    try:
        await asyncio.wait_for(first_send.wait(), 5)
        adapter._pending_messages[key] = _event("queued followup")
        runner.adapters.clear()
        await runner._cancel_reply_delivery_tasks()
        await asyncio.wait_for(next_handler.wait(), 5)
        survivors = [task for task in runner._reply_delivery_tasks if not task.done()]
        assert not survivors, "cancelled retired carrier spawned an un-drained successor"
    finally:
        release.set()
        tasks = {carrier, *adapter._background_tasks, *runner._reply_delivery_tasks}
        await asyncio.gather(*tasks, return_exceptions=True)
