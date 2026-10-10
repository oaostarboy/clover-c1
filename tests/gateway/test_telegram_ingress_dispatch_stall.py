"""Telegram 'healthy but deaf' watchdog: updates fetched but never dispatched.

getUpdates can keep succeeding (so ``_check_polling_stall`` stays quiet and
get_me() is healthy) while PTB's dispatcher is wedged and hands nothing to the
handlers. The adapter counts updates fetched on the getUpdates wire and updates
that reach the handler chain; a backlog with no dispatch progress across
``_INGRESS_DISPATCH_STALL_HEARTBEATS`` heartbeats is handed to the supervisor
as a ``_PollingStallError`` (rebuild, not an in-place restart).
"""
import asyncio
import json
import time as _time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import adapter as tg_adapter
from plugins.platforms.telegram.adapter import TelegramAdapter, _PollingStallError


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="123456:abc"))
    adapter._webhook_mode = False
    adapter._app = MagicMock()
    adapter._app.updater.running = True
    adapter._begin_polling_generation()
    return adapter


class _Request:
    @staticmethod
    def parse_json_payload(payload):
        return json.loads(payload.decode("utf-8", "replace"))


def _observe(adapter, generation, updates):
    body = json.dumps({"ok": True, "result": updates}).encode()
    adapter._observe_polling_request_result(_Request(), generation, (200, body))


@pytest.mark.asyncio
async def test_fetched_updates_are_counted_for_the_current_generation_only():
    adapter = _make_adapter()
    gen = adapter._polling_generation
    _observe(adapter, gen, [{"update_id": 1}, {"update_id": 2}])
    assert adapter._updates_received_total == 2
    _observe(adapter, gen, [])  # idle long-poll
    assert adapter._updates_received_total == 2
    # A late response from a fenced (older) generation must not inflate it.
    _observe(adapter, gen - 1, [{"update_id": 3}])
    assert adapter._updates_received_total == 2
    # New generation re-bases the counters.
    adapter._begin_polling_generation()
    assert adapter._updates_received_total == 0
    assert adapter._updates_dispatched_total == 0


@pytest.mark.asyncio
async def test_wedged_dispatcher_escalates_to_supervisor_rebuild():
    adapter = _make_adapter()
    adapter._updates_received_total = 5  # fetched, never dispatched
    recovered = []

    def capture(error, *, reason):
        recovered.append((error, reason))

    with patch.object(adapter, "_schedule_polling_recovery", side_effect=capture):
        for _ in range(tg_adapter._INGRESS_DISPATCH_STALL_HEARTBEATS - 1):
            adapter._check_ingress_dispatch_stall()
        assert recovered == []
        adapter._check_ingress_dispatch_stall()
        assert len(recovered) == 1
        error, _reason = recovered[0]
        assert isinstance(error, _PollingStallError)
        assert "5 received, 0 dispatched" in str(error)
        # Reported once per stall, not every heartbeat.
        adapter._check_ingress_dispatch_stall()
        assert len(recovered) == 1


@pytest.mark.asyncio
async def test_dispatch_progress_rearms_and_idle_never_escalates():
    adapter = _make_adapter()
    with patch.object(adapter, "_schedule_polling_recovery") as rec:
        # Idle: nothing received.
        for _ in range(10):
            adapter._check_ingress_dispatch_stall()
        # Backlog that keeps moving.
        adapter._updates_received_total = 100
        for i in range(10):
            adapter._updates_dispatched_total += 1
            adapter._check_ingress_dispatch_stall()
        # Caught up.
        adapter._updates_dispatched_total = 100
        for _ in range(10):
            adapter._check_ingress_dispatch_stall()
    rec.assert_not_called()


@pytest.mark.asyncio
async def test_stall_check_skips_webhook_and_inflight_recovery():
    adapter = _make_adapter()
    adapter._updates_received_total = 5
    with patch.object(adapter, "_schedule_polling_recovery") as rec:
        adapter._webhook_mode = True
        for _ in range(10):
            adapter._check_ingress_dispatch_stall()
        adapter._webhook_mode = False
        inflight = MagicMock()
        inflight.done.return_value = False
        adapter._polling_error_task = inflight
        for _ in range(10):
            adapter._check_ingress_dispatch_stall()
    rec.assert_not_called()


@pytest.mark.asyncio
async def test_heartbeat_loop_catches_healthy_but_deaf_dispatcher():
    """End-to-end through the lifetime heartbeat: getUpdates is fresh, get_me()
    is healthy, the queue is empty server-side, yet fetched updates never
    reach a handler. The loop must hand the adapter to the supervisor."""
    adapter = _make_adapter()
    bot = MagicMock()
    bot.get_me = AsyncMock()
    bot.get_webhook_info = AsyncMock(return_value=MagicMock(pending_update_count=0))
    adapter._app.bot = bot
    adapter._bot = bot
    adapter._updates_received_total = 3
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **k):
        await real_sleep(0)

    async def keep_polling_fresh():
        while True:
            adapter._polling_last_progress_monotonic = _time.monotonic()
            await real_sleep(0)

    errors = []

    async def fake_network_error(err):
        errors.append(err)

    with patch("asyncio.sleep", new=fast_sleep), \
            patch.object(adapter, "_handle_polling_network_error", new=fake_network_error):
        fresh = asyncio.ensure_future(keep_polling_fresh())
        loop_task = asyncio.ensure_future(adapter._polling_heartbeat_loop())
        try:
            for _ in range(500):
                await real_sleep(0)
                if adapter._polling_error_task is not None:
                    break
            adapter._polling_teardown_started = True
            await asyncio.wait_for(loop_task, 10)
        finally:
            fresh.cancel()
            if not loop_task.done():
                loop_task.cancel()
    assert adapter._polling_error_task is not None, "deaf dispatcher was never escalated"
    await adapter._polling_error_task
    assert len(errors) == 1 and isinstance(errors[0], _PollingStallError)
