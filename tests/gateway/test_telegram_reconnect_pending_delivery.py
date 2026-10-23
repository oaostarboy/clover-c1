"""Reconnect recovery must reclaim replies rejected before transport dispatch."""
from contextlib import closing

import pytest

from gateway import delivery_ledger as ledger


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, '_db_path', lambda: tmp_path / 'state.db')
    monkeypatch.setattr(ledger, '_owner_stamp', lambda: (42, 123))
    return ledger


def record(dl, oid='reply', platform='telegram'):
    dl.record_obligation(obligation_id=oid, session_key='synthetic-session',
                         platform=platform, chat_id='123', thread_id=None,
                         content='pending reply', adapter_profile='default')
    dl.mark_attempting(oid)


def test_reconnect_claims_not_connected_reply_once(isolated_ledger):
    dl = isolated_ledger
    record(dl)
    dl.mark_failed('reply', 'Not connected')
    rows = dl.sweep_failed_for_runtime('telegram')
    assert len(rows) == 1, 'a reply rejected during adapter replacement remains stranded'
    assert rows[0]['content'] == 'pending reply'
    assert rows[0]['needs_marker']  # honest conservative recovery, not exactly-once
    assert dl.sweep_failed_for_runtime('telegram') == []
    dl.mark_delivered('reply')
    assert dl.sweep_failed_for_runtime('telegram') == []


@pytest.mark.parametrize('error', ['Timed out', 'NetworkError', 'Forbidden', 'invalid token',
                                  'remote acknowledgement lost', 'Not connected: unknown'])
def test_uncertain_or_permanent_rejections_are_not_runtime_replayed(isolated_ledger, error):
    dl = isolated_ledger
    record(dl)
    dl.mark_failed('reply', error)
    assert dl.sweep_failed_for_runtime('telegram') == []
    with closing(dl._connect()) as conn:
        assert conn.execute('SELECT content,state FROM delivery_obligations').fetchone() == (
            'pending reply', 'failed')


def test_non_telegram_not_connected_contract_is_not_assumed(isolated_ledger):
    dl = isolated_ledger
    record(dl, platform='slack')
    dl.mark_failed('reply', 'Not connected')
    assert dl.sweep_failed_for_runtime('slack') == []


@pytest.mark.asyncio
async def test_hung_stop_handoff_recovers_pending_reply_without_reusing_lock(
    isolated_ledger, monkeypatch, tmp_path,
):
    import asyncio
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.run import GatewayRunner
    from plugins.platforms.telegram import adapter as tg

    config = PlatformConfig(enabled=True, token='synthetic-token')
    runner = GatewayRunner(GatewayConfig(
        platforms={Platform.TELEGRAM: config}, sessions_dir=tmp_path / 'sessions'))
    old = tg.TelegramAdapter(config)
    release, entered = asyncio.Event(), asyncio.Event()
    lock = asyncio.Lock()
    stops = []

    async def stop():
        stops.append(asyncio.current_task())
        async with lock:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()  # cancellation-resistant SDK cleanup

    updater = SimpleNamespace(running=True, stop=stop, start_polling=AsyncMock())
    old._app = SimpleNamespace(updater=updater, running=False, shutdown=AsyncMock())
    old.set_fatal_error_handler(runner._handle_adapter_fatal_error)
    runner.adapters = {Platform.TELEGRAM: old}
    runner.delivery_router.adapters = runner.adapters
    monkeypatch.setattr(tg, '_UPDATER_STOP_TIMEOUT', 0.01)
    # Only elide the deterministic recovery backoff, not timeout/cancellation.
    sleep = asyncio.sleep
    async def fast_sleep(delay):
        await sleep(0)
    monkeypatch.setattr(tg.asyncio, 'sleep', fast_sleep)
    try:
        await asyncio.wait_for(old._handle_polling_network_error(OSError('offline')), 1)
        assert entered.is_set() and lock.locked()
        assert Platform.TELEGRAM in runner._failed_platforms
        assert Platform.TELEGRAM not in runner.adapters
        updater.start_polling.assert_not_awaited()
        # Final response produced during the long reconnect window.
        dl = isolated_ledger
        record(dl)
        dl.mark_failed('reply', 'Not connected')
        rows = dl.sweep_failed_for_runtime('telegram')
        assert len(rows) == 1
        replacement = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True)))
        runner.adapters[Platform.TELEGRAM] = replacement
        assert await runner._redeliver_claimed_obligations(rows) == 1
        replacement.send.assert_awaited_once()
        assert replacement.send.call_args.kwargs['content'].endswith('pending reply')
        assert dl.sweep_failed_for_runtime('telegram') == []
        release.set()
        await asyncio.gather(*stops, return_exceptions=True)
        # Late old stop cannot remove the installed replacement or resend.
        assert runner.adapters[Platform.TELEGRAM] is replacement
        replacement.send.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(*stops, return_exceptions=True)
