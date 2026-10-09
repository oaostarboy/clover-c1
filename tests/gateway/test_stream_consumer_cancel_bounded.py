"""GatewayStreamConsumer cancellation must not wait on stalled platform I/O.

Counterexample reproduced on base 1ce10be2 (see also the parent's unchanged
probe ``parent_stream_waitfor_probe.py``): ``run()``'s ``CancelledError``
handler awaits ``_send_or_edit`` (a best-effort final edit) with no bound, so
``asyncio.wait_for(task, timeout)`` -- the gateway's own cleanup primitive --
waits for the stalled edit instead of enforcing its deadline.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.stream_consumer import GatewayStreamConsumer


def _stalled_adapter():
    release = asyncio.Event()
    entered = asyncio.Event()
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.REQUIRES_EDIT_FINALIZE = True
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="probe"))

    async def edit(*args, **kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(success=True, message_id="probe")

    adapter.edit_message = AsyncMock(side_effect=edit)
    return adapter, release, entered


@pytest.mark.asyncio
async def test_cancel_with_stalled_final_edit_is_bounded(monkeypatch):
    monkeypatch.setattr(GatewayStreamConsumer, "_CANCEL_FINAL_EDIT_TIMEOUT", 0.05, raising=False)
    adapter, release, entered = _stalled_adapter()
    consumer = GatewayStreamConsumer(adapter, "synthetic-chat")
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.06)
    consumer._accumulated = "synthetic completed handoff"
    consumer._message_id = "probe"
    consumer._last_sent_content = "previous partial text"

    waiter = asyncio.create_task(asyncio.wait_for(task, timeout=0.01))
    done, _ = await asyncio.wait({waiter}, timeout=0.5)
    try:
        assert entered.is_set(), "did not exercise the stalled edit seam"
        assert done, (
            "cleanup deadline exceeded: wait_for waits for the stalled "
            "cancel-time final edit"
        )
        assert task.done()
        # A timed-out / never-confirmed edit must not claim delivery.
        assert not consumer._final_response_sent
        assert not consumer._final_content_delivered
    finally:
        release.set()
        for t in (waiter, task):
            if not t.done():
                t.cancel()
        await asyncio.gather(waiter, task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_with_healthy_final_edit_still_finalizes(monkeypatch):
    """Control: a responsive platform still gets the best-effort final edit."""
    monkeypatch.setattr(GatewayStreamConsumer, "_CANCEL_FINAL_EDIT_TIMEOUT", 0.5, raising=False)
    adapter, release, entered = _stalled_adapter()
    release.set()  # edits succeed immediately
    consumer = GatewayStreamConsumer(adapter, "synthetic-chat")
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.06)
    consumer._accumulated = "synthetic completed handoff"
    consumer._message_id = "probe"
    consumer._last_sent_content = "previous partial text"
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)
    assert entered.is_set()
