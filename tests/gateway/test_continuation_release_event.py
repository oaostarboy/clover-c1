"""The gateway delivers a continuation_release as a trusted internal turn."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.gateway.test_trusted_human_origin_ingress import Harness, network_guard  # noqa: F401


@pytest.fixture
def harness(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, proactive=False)
    yield h
    h.close()


def _evt(**extra):
    base = {
        "type": "continuation_release", "delegation_id": "deleg_abc123",
        "workstream_id": "ws_0123456789abcdef", "job_id": "job1",
        "occurrence_key": "k1", "session_key": "agent:main:telegram:dm:12345",
        "parent_session_id": "sess-1",
    }
    base.update(extra)
    return base


def test_release_event_is_injected_as_internal_turn_with_trusted_completion_ids(harness):
    from tools.process_registry import process_registry

    harness.adapter.handle_message = AsyncMock()
    harness.runner._running = True

    async def _run():
        process_registry.completion_queue.put(_evt())
        task = asyncio.create_task(harness.runner._async_delegation_watcher(interval=0.01))
        deadline = asyncio.get_running_loop().time() + 10
        while not harness.adapter.handle_message.await_count:
            assert asyncio.get_running_loop().time() < deadline, "release never injected"
            await asyncio.sleep(0.02)
        harness.runner._running = False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())
    (event,), _ = harness.adapter.handle_message.await_args
    assert event.internal is True
    assert event.metadata["completion_delegation_ids"] == ["deleg_abc123"]
    # The text is a notice, never an instruction to start work or a grant.
    assert "do not start any stage yourself" in event.text
    assert process_registry.completion_queue.empty()


def test_the_post_turn_drain_leaves_release_events_to_their_owner(harness):
    from gateway.run import _drain_gateway_watch_events
    import queue

    q = queue.Queue()
    q.put(_evt())
    assert _drain_gateway_watch_events(q) == []
    assert q.get_nowait()["type"] == "continuation_release"
