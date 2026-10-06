"""Article lifecycle over real consumer/adapter and fake network, not client proof."""
import asyncio

import pytest

from tests.gateway.test_telegram_native_progress import (
    Clock, History, make_consumer, native_adapter, until,
)


def progress_text(frame):
    rich = frame["rich_message"]
    return rich.get("html", rich.get("markdown", ""))


@pytest.mark.asyncio
async def test_private_request_opens_nonempty_stoppable_html_draft_before_any_model_event():
    adapter, api = native_adapter()
    history = History(api)
    consumer = make_consumer(adapter, history=history)
    task = asyncio.create_task(consumer.run())
    try:
        assert await until(lambda: api.rich_drafts(), timeout=0.5), "request must seed before tools/tokens"
        [frame] = api.rich_drafts()
        assert frame["draft_id"] != 0 and frame["can_stop"] is True
        assert set(frame["rich_message"]) == {"html"}
        assert "<tg-thinking>" in progress_text(frame) and "Thinking" in progress_text(frame)
        assert api.methods("send_message_draft") == []
        assert api.methods("send_message") == []
        assert api.methods("edit_message_text") == []
        assert history.calls == [], "seed status is not a fabricated history/tool row"
    finally:
        consumer.finish("")
        await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_tool_boundary_updates_same_preview_without_persistent_interim_replacement():
    adapter, api = native_adapter()
    consumer = make_consumer(adapter)
    task = asyncio.create_task(consumer.run())
    try:
        consumer.on_tool_progress("Reading the first source", tool="read_file")
        consumer.on_delta("checking one source")
        assert await until(lambda: any("checking one source" in progress_text(f) for f in api.rich_drafts()))
        draft_id = api.rich_drafts()[0]["draft_id"]
        consumer.on_segment_break(interim=True)
        consumer.on_tool_progress("Searching the next source", tool="web_search")
        consumer.on_delta("checking next source")
        assert await until(lambda: any("checking next source" in progress_text(f) for f in api.rich_drafts()))
        assert api.methods("send_message") == [], "boundary must not replace draft with legacy chat bubble"
        assert api.methods("do_api_request:sendRichMessage") == []
        assert {f["draft_id"] for f in api.rich_drafts()} == {draft_id}
        assert api.methods("send_message_draft") == []
        assert api.methods("edit_message_text") == []
    finally:
        consumer.finish("the final answer")
        await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_healthy_unchanged_draft_refreshes_past_five_minutes_without_expiring():
    adapter, api = native_adapter()
    history = History(api)
    consumer = make_consumer(adapter, history=history)
    clock = Clock()
    consumer._np_clock = clock
    task = asyncio.create_task(consumer.run())
    try:
        assert await until(lambda: api.rich_drafts() and consumer._np_task is None)
        draft_id = api.rich_drafts()[0]["draft_id"]
        for _ in range(24):  # six minutes, no new model status
            before = len(api.rich_drafts())
            clock.t += 15
            await consumer._np_pump()
            assert await until(lambda: len(api.rich_drafts()) > before and consumer._np_task is None)
            assert consumer.native_activity_active, "healthy refresh cannot be capped before task ends"
            assert history.calls == []
        assert {f["draft_id"] for f in api.rich_drafts()} == {draft_id}
        assert all("Thinking" in progress_text(f) for f in api.rich_drafts())
    finally:
        consumer.finish("")
        await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_finish_sends_one_persistent_rich_answer_even_for_plain_text():
    adapter, api = native_adapter()
    consumer = make_consumer(adapter)
    task = asyncio.create_task(consumer.run())
    consumer.on_delta("a plain answer")
    assert await until(lambda: any("a plain answer" in progress_text(f) for f in api.rich_drafts()))
    consumer.finish("a plain answer")
    await asyncio.wait_for(task, 3)
    [sent] = api.methods("do_api_request:sendRichMessage")
    assert sent["api_kwargs"]["rich_message"]["markdown"] == "a plain answer"
    assert "tg-thinking" not in str(sent)
    assert api.methods("send_message") == []
    assert api.methods("edit_message_text") == []
    assert consumer.delivered_final_matches("a plain answer") is True
    before = len(api.rich_drafts())
    await consumer._np_pump()
    assert len(api.rich_drafts()) == before
    assert adapter._native_drafts == {} and consumer._np_task is None


@pytest.mark.asyncio
async def test_native_status_only_turn_delivers_authoritative_nonstreamed_final():
    adapter, api = native_adapter()
    consumer = make_consumer(adapter)
    task = asyncio.create_task(consumer.run())
    assert await until(lambda: api.rich_drafts())
    consumer.on_tool_progress("Reading a source", tool="read_file")
    consumer.finish("completed answer plus verifier footer")
    await asyncio.wait_for(task, 3)
    [sent] = api.methods("do_api_request:sendRichMessage")
    assert sent["api_kwargs"]["rich_message"]["markdown"] == "completed answer plus verifier footer"
    assert consumer.delivered_final_matches("completed answer plus verifier footer") is True


@pytest.mark.asyncio
async def test_stop_retires_inflight_network_writer_before_requesting_agent_cancel():
    from tests.gateway.test_telegram_native_stop import stop_update

    adapter, api = native_adapter()
    api.gate["sendRichMessageDraft"] = asyncio.Event()
    consumer = make_consumer(adapter)
    observations = []

    class Runner:
        def _is_session_run_current(self, key, generation):
            return True

        async def native_stop_current(self, scope, stopped_consumer):
            observations.append((stopped_consumer._np_task, stopped_consumer.native_activity_active))
            await stopped_consumer.native_stop_finish()

        async def handler(self, event):
            pass

    adapter._message_handler = Runner().handler
    adapter._is_callback_user_authorized = lambda *a, **k: True
    task = asyncio.create_task(consumer.run())
    try:
        assert await until(lambda: api.rich_drafts())
        writer = consumer._np_task
        draft_id = api.rich_drafts()[0]["draft_id"]
        await adapter._on_stopped_message_generation(stop_update(12345, draft_id))
        assert await until(lambda: observations)
        assert observations == [(None, False)], "network writer must retire before cancellation hook"
        assert writer.cancelled()
        api.gate["sendRichMessageDraft"].set()
        assert api.accepted_draft_frames == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_claimed_stop_cannot_emit_late_answer_or_legacy_preview():
    adapter, api = native_adapter()
    consumer = make_consumer(adapter)
    task = asyncio.create_task(consumer.run())
    assert await until(lambda: api.rich_drafts() and consumer._np_task is None)
    assert consumer.native_stop_claim()
    consumer.on_delta("late answer from cancelled model")
    consumer.on_commentary("late commentary")
    consumer.finish("late final")
    await asyncio.wait_for(task, 3)
    assert api.methods("send_message_draft") == []
    assert api.methods("send_message") == []
    assert api.methods("do_api_request:sendRichMessage") == []
    assert consumer.final_response_sent is False
    assert adapter._native_drafts == {}
