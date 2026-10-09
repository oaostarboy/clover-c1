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


@pytest.mark.asyncio
async def test_stuck_teardown_task_is_recancelled_and_tracked():
    """Released-but-stuck helpers get a second cancel and are tracked, not parked."""
    from gateway import bounded_await

    entered_handler = asyncio.Event()
    second = asyncio.Event()

    async def stubborn():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            entered_handler.set()
            try:
                await asyncio.sleep(3600)  # I/O inside the cancel handler
            except asyncio.CancelledError:
                second.set()
                raise

    task = asyncio.ensure_future(stubborn())
    await asyncio.sleep(0)
    before = bounded_await.stuck_task_count()
    finished = await bounded_await.reap_task(task, grace=0.05, label="stubborn")
    assert finished is False
    assert entered_handler.is_set()
    await asyncio.wait({task}, timeout=1.0)
    assert task.done() and second.is_set()
    assert bounded_await.stuck_task_count() == before


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_from_drain_then_cancel():
    from gateway import bounded_await

    inner = asyncio.ensure_future(asyncio.sleep(3600))
    outer = asyncio.ensure_future(
        bounded_await.drain_then_cancel(inner, drain=30, grace=30, label="x")
    )
    await asyncio.sleep(0.05)
    outer.cancel()
    done, _ = await asyncio.wait({outer}, timeout=1.0)
    assert done
    with pytest.raises(asyncio.CancelledError):
        outer.result()
    await asyncio.wait({inner}, timeout=1.0)
    assert inner.cancelled()


@pytest.mark.asyncio
async def test_abandoned_final_edit_cannot_resume_delivery_late(monkeypatch):
    """A timed-out cancel-time edit that wakes late (cancellation suppressed by
    platform I/O) must not run fallback/continuation sends or further edits."""
    monkeypatch.setattr(GatewayStreamConsumer, "_CANCEL_FINAL_EDIT_TIMEOUT", 0.05, raising=False)
    release = asyncio.Event()
    entered = asyncio.Event()
    events = []
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.REQUIRES_EDIT_FINALIZE = True

    async def send(*a, **k):
        events.append("send")
        return SimpleNamespace(success=True, message_id="late")

    async def edit(*a, **k):
        events.append("edit")
        entered.set()
        while not release.is_set():  # I/O that swallows cancellation
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return SimpleNamespace(success=False, error="timeout", retryable=True)

    adapter.send = AsyncMock(side_effect=send)
    adapter.edit_message = AsyncMock(side_effect=edit)
    consumer = GatewayStreamConsumer(adapter, "synthetic-chat")
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.06)
    consumer._accumulated = "synthetic completed handoff"
    consumer._message_id = "probe"
    consumer._last_sent_content = "previous partial text"
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=1.0)
    assert done and entered.is_set()
    assert consumer._abandoned
    before = list(events)
    release.set()  # stalled edit wakes up now, fails, and would fall back
    await asyncio.sleep(0.3)
    assert events == before, f"abandoned consumer touched the platform late: {events}"
    assert not consumer._final_response_sent
    assert not consumer._final_content_delivered


@pytest.mark.asyncio
async def test_caller_cancel_during_grace_still_escalates_helper():
    """Stop landing while teardown waits on a stuck helper must not leave the
    helper parked in its first cancellation handler."""
    from gateway import bounded_await

    second = asyncio.Event()

    async def stubborn():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                second.set()
                raise

    helper = asyncio.ensure_future(stubborn())
    await asyncio.sleep(0)
    outer = asyncio.ensure_future(
        bounded_await.reap_tasks([helper], grace=30.0, labels=["stubborn"])
    )
    helper.cancel()
    await asyncio.sleep(0.05)  # helper now parked inside its cancel handler
    outer.cancel()  # Stop arrives mid-grace
    done, _ = await asyncio.wait({outer}, timeout=1.0)
    assert done
    with pytest.raises(asyncio.CancelledError):
        outer.result()
    await asyncio.wait({helper}, timeout=1.0)
    assert helper.done() and second.is_set()
    assert bounded_await.stuck_task_count() == 0


@pytest.mark.asyncio
async def test_await_bounded_timeout_tracks_and_recancels_suppressing_task():
    from gateway import bounded_await

    stage = []

    async def swallow_once():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            stage.append("first")
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                stage.append("second")
                raise

    ok, _ = await bounded_await.await_bounded(swallow_once(), 0.05)
    assert ok is False
    await asyncio.sleep(0.2)
    assert stage == ["first", "second"]
    assert bounded_await.stuck_task_count() == 0


@pytest.mark.asyncio
async def test_second_cancel_during_cancel_time_io_fences_consumer():
    """Stop interrupting the cancel-time final send must fence BEFORE propagating:
    a cancellation-suppressing send that then fails must not trigger a fallback edit."""
    events = []
    release = asyncio.Event()
    entered = asyncio.Event()
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.REQUIRES_EDIT_FINALIZE = True

    async def edit(*a, **k):
        events.append("edit")
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue  # I/O layer swallows cancellation
        return SimpleNamespace(success=False, error="boom", retryable=True)

    async def send(*a, **k):
        events.append("send")
        return SimpleNamespace(success=True, message_id="x")

    adapter.edit_message = AsyncMock(side_effect=edit)
    adapter.send = AsyncMock(side_effect=send)
    consumer = GatewayStreamConsumer(adapter, "synthetic-chat")
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.06)
    consumer._accumulated = "synthetic completed handoff"
    consumer._message_id = "probe"
    consumer._last_sent_content = "previous partial text"
    task.cancel()
    await asyncio.wait_for(entered.wait(), 1.0)
    task.cancel()  # Stop arrives while the cancel-time edit is in flight
    await asyncio.wait({task}, timeout=1.0)
    assert consumer._abandoned
    before = list(events)
    release.set()
    await asyncio.sleep(0.3)
    assert events == before, f"platform touched after Stop fenced the consumer: {events}"
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_caller_cancel_during_drain_window_escalates_helper():
    from gateway import bounded_await

    second = asyncio.Event()
    started = asyncio.Event()

    async def stubborn():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            try:
                await asyncio.sleep(3600)  # parked in its first cancel handler
            except asyncio.CancelledError:
                second.set()
                raise

    helper = asyncio.ensure_future(stubborn())
    await started.wait()
    outer = asyncio.ensure_future(
        bounded_await.drain_then_cancel(helper, drain=30.0, grace=30.0, label="stubborn")
    )
    await asyncio.sleep(0.05)
    outer.cancel()  # Stop arrives during the drain window
    await asyncio.wait({outer}, timeout=1.0)
    with pytest.raises(asyncio.CancelledError):
        outer.result()
    await asyncio.sleep(0.1)
    await asyncio.wait({helper}, timeout=1.0)
    assert helper.done() and second.is_set()
    assert bounded_await.stuck_task_count() == 0


@pytest.mark.asyncio
async def test_escalation_cancels_a_task_at_most_once_more():
    from gateway import bounded_await

    cancels = []

    async def cleaner():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancels.append(1)
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancels.append(2)
                try:
                    await asyncio.sleep(0.2)  # async cleanup after 2nd cancel
                    cancels.append("cleanup-complete")
                except asyncio.CancelledError:
                    cancels.append("cleanup-interrupted")
                    raise
                raise

    t = asyncio.ensure_future(cleaner())
    await asyncio.sleep(0)
    t.cancel()
    await asyncio.sleep(0.02)
    for _ in range(3):  # repeated teardown attempts
        bounded_await._escalate(t, "cleaner", 0.0)
    await asyncio.wait({t}, timeout=1.0)
    assert cancels == [1, 2, "cleanup-complete"], cancels


@pytest.mark.asyncio
async def test_repeat_drain_then_cancel_does_not_interrupt_escalated_cleanup():
    from gateway import bounded_await

    log = []

    async def cleaner():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            log.append("first")
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                log.append("second")
                try:
                    await asyncio.sleep(0.3)
                    log.append("cleanup-complete")
                except asyncio.CancelledError:
                    log.append("cleanup-interrupted")
                    raise
                raise

    t = asyncio.ensure_future(cleaner())
    await asyncio.sleep(0)
    first = await bounded_await.drain_then_cancel(t, drain=0.02, grace=0.05, label="c")
    assert first is False  # released, still unwinding
    await asyncio.sleep(0.05)  # now inside its post-second-cancel cleanup
    again = await bounded_await.drain_then_cancel(t, drain=0.0, grace=0.05, label="c")
    assert again is False
    await asyncio.wait({t}, timeout=1.0)
    assert log == ["first", "second", "cleanup-complete"], log
