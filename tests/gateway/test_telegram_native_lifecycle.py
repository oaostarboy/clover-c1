"""Lifecycle / concurrency guarantees of the native display (real gateway pieces).

Reuses the Stop test environment: real adapter + base session loop + real
``_handle_message_with_agent``/``_run_agent`` + stream consumer, in-memory Bot API.
"""

import asyncio
import time

import pytest

from gateway.native_progress import NativeProgressScope
from tests.gateway.test_telegram_native_progress import (
    History, make_consumer, native_adapter,
)
from tests.gateway.test_telegram_native_stop import (
    FOLLOWUP, BlockingAgent, Env, stop_update, until,
)


@pytest.mark.asyncio
async def test_stalled_draft_network_and_event_burst_do_not_hold_up_other_chats_or_stop(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path)
    env.api.gate["sendRichMessageDraft"] = asyncio.Event()       # draft network never answers
    await env.start("12345", text="hello burst")
    assert await until(lambda: env.api.rich_drafts())             # first frame is in flight (stuck)

    # the loop stays responsive while 300 events pile up behind the stalled send
    worst, last = 0.0, time.monotonic()
    for _ in range(30):
        await asyncio.sleep(0.01)
        now = time.monotonic()
        worst, last = max(worst, now - last), now
    assert worst < 0.25

    # a different chat's inbound message is dispatched and answered meanwhile
    await env.start("67890", text="hello fast")
    assert await until(lambda: env.replies(67890, FOLLOWUP), timeout=5)
    # and a Stop for the stuck chat is accepted instantly, never awaiting the stalled send
    draft = env.draft_id(12345)
    started = time.monotonic()
    await env.adapter._on_stopped_message_generation(stop_update(12345, draft), None)
    assert time.monotonic() - started < 0.25
    await env.idle(12345)
    assert env.agent_of(12345).is_interrupted is True
    assert len(env.confirmations()) == 1
    env.api.gate["sendRichMessageDraft"].set()


@pytest.mark.asyncio
async def test_disconnect_forgets_every_draft_and_disables_native(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path)
    await env.start("12345")
    assert await until(lambda: env.adapter._native_drafts)
    assert env.adapter._native_stop_ready is True
    await env.adapter.disconnect()
    assert env.adapter._native_drafts == {}
    assert env.adapter._native_stop_ready is False
    assert env.adapter.supports_native_progress(chat_type="dm") is False
    BlockingAgent.release.set()
    await until(lambda: not env.active(12345), timeout=10)


@pytest.mark.asyncio
async def test_one_draft_id_of_a_turn_is_registered_and_dropped_at_terminal(monkeypatch):
    adapter, api = native_adapter()
    GatewayConsumer = type(make_consumer(adapter))
    monkeypatch.setattr(GatewayConsumer, "NATIVE_MIN_SEND_INTERVAL", 0.0)
    scope = NativeProgressScope(session_key="sess-reg", run_generation=3, source=None)
    consumer = make_consumer(adapter, native_kwargs={"native_scope": scope})
    task = asyncio.create_task(consumer.run())
    consumer.on_tool_progress("🔍 one", tool="web_search")
    consumer.on_delta("first segment")
    assert await until(lambda: adapter._native_drafts)
    first = {k[2] for k in adapter._native_drafts}
    consumer.on_segment_break()
    consumer.on_delta("second segment")
    assert await until(lambda: any("second segment" in api.rich_text(f) for f in api.rich_drafts()))
    ids = {k[2] for k in adapter._native_drafts}
    assert first == ids and all(v.scope is scope for v in adapter._native_drafts.values())
    assert all(k[1] is None for k in adapter._native_drafts)      # DM only: never a thread route
    consumer.finish("second segment")
    await asyncio.wait_for(task, 3)
    assert adapter._native_drafts == {}                           # terminal drops them all
    assert consumer._np_task is None


@pytest.mark.asyncio
async def test_cancelled_consumer_task_fences_unregisters_and_keeps_history(monkeypatch):
    adapter, api = native_adapter()
    history = History(api)
    GatewayConsumer = type(make_consumer(adapter))
    monkeypatch.setattr(GatewayConsumer, "NATIVE_MIN_SEND_INTERVAL", 0.0)
    consumer = make_consumer(adapter, history=history)
    task = asyncio.create_task(consumer.run())
    consumer.on_tool_progress("🔍 shutdown lookup", tool="web_search")
    assert await until(lambda: adapter._native_drafts)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert adapter._native_drafts == {}
    assert consumer.native_activity_active is False
    assert history.calls and history.calls[0][0] == ["🔍 shutdown lookup"]
    frames = len(api.rich_drafts())
    await asyncio.sleep(0.3)
    assert len(api.rich_drafts()) == frames                       # nothing keeps writing
