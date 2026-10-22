"""A restart must drain the outer final-send carrier, not just its agent.

The transport ACK and ledger commit happen after the handler releases the
runner's agent slot. Exercise the real base producer and its subclass send
wrapper, including an acknowledged send held in a post-send card hook.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway import delivery_ledger as dl
from gateway.config import Platform
from gateway.platforms.base import SendResult
from tests.gateway.test_delivery_ledger_producer import _Adapter, _event, _rows
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.mark.asyncio
@pytest.mark.parametrize("barrier", ["transport", "after_reply"])
async def test_restart_waits_for_outer_reply_ack_and_ledger(tmp_path, monkeypatch, barrier):
    monkeypatch.setattr(dl, "_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(dl, "ledger_enabled", lambda: True)
    adapter = _Adapter()
    runner, _ = make_restart_runner(adapter)
    runner.adapters = {Platform.SLACK: adapter}
    runner._active_cron_job_count = lambda: 0
    runner._active_api_run_count = lambda: 0
    runner._restart_after_turn_timeout = 5
    runner.stop = AsyncMock()
    key = "agent:main:slack:channel:C1"
    entered, release = asyncio.Event(), asyncio.Event()

    if barrier == "transport":
        # Like IRC: bytes reach the peer before rate limiting returns the ACK.
        async def send(chat_id, content, reply_to=None, metadata=None):
            adapter.sent.append(content)
            entered.set()
            await release.wait()
            return SendResult(success=True, message_id="accepted-1")
        adapter.send = send
    else:
        # The real __init_subclass__ wrapper awaits this AFTER transport success.
        async def after_reply(*args, **kwargs):
            entered.set()
            await release.wait()
        monkeypatch.setattr("gateway.delegation_activity.after_reply", after_reply)

    async def handler(event):
        runner._running_agents.pop(key, None)
        return "final answer"

    adapter._message_handler = handler
    runner._running_agents[key] = object()
    adapter._active_sessions[key] = asyncio.Event()
    carrier = asyncio.create_task(adapter._process_message_background(_event(), key))
    adapter._session_tasks[key] = carrier
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not runner._running_agents
        assert adapter.sent == ["final answer"]
        assert _rows()[0][1] == "attempting"
        assert runner._active_work_count() == 1, "outer reply carrier is still live"
        assert runner.request_restart(detached=False, via_service=True)
        # Give the restart coroutine a turn, without depending on wall time.
        await asyncio.sleep(0)
        runner.stop.assert_not_awaited()
        release.set()
        await asyncio.wait_for(carrier, 5)
        assert runner._restart_task is not None
        await asyncio.wait_for(runner._restart_task, 5)
        assert _rows()[0][1] == "delivered"
        runner.stop.assert_awaited_once()
        assert adapter.sent == ["final answer"]
    finally:
        release.set()
        await asyncio.gather(carrier, return_exceptions=True)
        task = getattr(runner, "_restart_task", None)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_forced_restart_retains_uncertain_reply_recovery(tmp_path, monkeypatch):
    """A safety-cap shutdown must NOT invent a successful transport ACK."""
    monkeypatch.setattr(dl, "_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(dl, "ledger_enabled", lambda: True)
    adapter = _Adapter()
    runner, _ = make_restart_runner(adapter)
    runner.adapters = {Platform.SLACK: adapter}
    runner._active_cron_job_count = lambda: 0
    runner._active_api_run_count = lambda: 0
    runner._restart_after_turn_timeout = 0
    key = "agent:main:slack:channel:C1"
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_send(chat_id, content, reply_to=None, metadata=None):
        adapter.sent.append(content)
        entered.set()
        await release.wait()  # No completed SendResult: ACK is still uncertain.
        return SendResult(success=True, message_id="accepted-1")

    adapter.send = blocked_send
    adapter._message_handler = AsyncMock(return_value="final answer")
    adapter._active_sessions[key] = asyncio.Event()
    carrier = asyncio.create_task(adapter._process_message_background(_event(), key))
    adapter._session_tasks[key] = carrier

    async def forced_stop(**kwargs):
        carrier.cancel()
        await asyncio.gather(carrier, return_exceptions=True)

    runner.stop = AsyncMock(side_effect=forced_stop)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert runner.request_restart(detached=False, via_service=True)
        assert runner._restart_task is not None
        await asyncio.wait_for(runner._restart_task, 5)
        assert _rows()[0][1] == "attempting"
        monkeypatch.setattr(dl, "_owner_alive", lambda *_: False)
        claimed = dl.sweep_recoverable()
        assert len(claimed) == 1 and claimed[0]["needs_marker"] is True
        assert adapter.sent == ["final answer"]
    finally:
        release.set()
        await asyncio.gather(carrier, return_exceptions=True)


@pytest.mark.asyncio
async def test_direct_shutdown_drains_outer_reply_carrier():
    adapter = _Adapter()
    runner, _ = make_restart_runner(adapter)
    runner._active_cron_job_count = lambda: 0
    runner._active_api_run_count = lambda: 0
    release, polled = asyncio.Event(), asyncio.Event()
    carrier = asyncio.create_task(release.wait())
    adapter._session_tasks["final-reply"] = carrier
    runner._update_runtime_status.side_effect = lambda *_: polled.set()
    drain = asyncio.create_task(runner._drain_active_agents(timeout=5))
    try:
        await asyncio.wait_for(polled.wait(), 5)
        assert not drain.done(), "shutdown ignored a still-live final reply"
        release.set()
        await carrier
        _, timed_out = await asyncio.wait_for(drain, 5)
        assert timed_out is False
    finally:
        release.set()
        await asyncio.gather(carrier, drain, return_exceptions=True)


@pytest.mark.asyncio
async def test_adapter_carriers_are_deduplicated_and_finished_tasks_do_not_pin_restart():
    adapter = _Adapter()
    runner, _ = make_restart_runner(adapter)
    runner._active_cron_job_count = lambda: 0
    runner._active_api_run_count = lambda: 0
    runner._profile_adapters = {"secondary": {Platform.SLACK: adapter}}
    release = asyncio.Event()
    carrier = asyncio.create_task(release.wait())
    key = "agent:main:slack:channel:C1"
    adapter._session_tasks[key] = carrier
    runner._running_agents[key] = object()
    try:
        assert runner._active_work_count() == 1
        runner._running_agents.pop(key)
        assert runner._active_work_count() == 1
        release.set()
        await carrier
        assert runner._active_work_count() == 0
    finally:
        release.set()
        await asyncio.gather(carrier, return_exceptions=True)
