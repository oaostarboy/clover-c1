"""Behavioral acceptance for Sol's isolated lifecycle scope (fake network only)."""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.native_progress import NativeProgressScope
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway._telegram_fake_api import FakeTelegramApi


def consumer():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={
        "rich_messages": True, "native_progress": True,
    }))
    adapter._bot = FakeTelegramApi()
    adapter._native_stop_ready = True
    c = GatewayStreamConsumer(adapter, "12345", StreamConsumerConfig(
        transport="draft", chat_type="dm", edit_interval=0.01, buffer_threshold=1,
    ), native_scope=NativeProgressScope("test", 1), on_native_history=lambda *args: None)
    assert c.native_activity_active
    return c


def test_identical_native_previews_keep_distinct_calls():
    c = consumer()
    c.note_tool("terminal")
    c.route_progress_item("same clipped preview")
    c.note_tool("terminal")
    c.route_progress_item(("__dedup__", "same clipped preview", 1))
    c._np_apply_events()
    assert len(c._np_ledger.rows) == 2
    assert all(r.repeat == 1 for r in c._np_ledger.rows)


def test_legacy_overlap_keeps_visible_aggregate_failure_not_invented_success():
    c = consumer()
    for text in ["command one", "command two"]:
        c.on_tool_progress(text, tool="terminal")
    c.on_tool_complete("terminal", duration=1.25, is_error=True)
    c.on_tool_complete("terminal", duration=2.5, is_error=False)
    c._np_apply_events()
    rows = c._np_ledger.rows
    assert len(rows) == 2
    assert any(r.state == "failed" for r in rows), "overlap must not erase a real error"
    assert not any(r.state == "succeeded" for r in rows), "unknown pairing cannot claim success"
    assert all(r.duration is None for r in rows)


def test_gateway_id_events_correlate_raw_arguments_and_durations():
    c = consumer()
    starts = [
        {"type": "tool.started", "tool_name": "terminal", "tool_call_id": "a",
         "preview": "same preview", "arguments": {"command": "first\n  || <tag>"}},
        {"type": "tool.started", "tool_name": "terminal", "tool_call_id": "b",
         "preview": "same preview", "arguments": {"command": "second"}},
    ]
    for item in starts:
        c.route_progress_item(item)
    c.route_progress_item({"type": "tool.completed", "tool_name": "terminal",
                           "tool_call_id": "b", "duration": 0.5, "is_error": False})
    c.route_progress_item({"type": "tool.completed", "tool_name": "terminal",
                           "tool_call_id": "a", "duration": 1.25, "is_error": True,
                           "result": "SECRET-RESULT-BODY"})
    c._np_apply_events()
    assert len(c._np_ledger.rows) == 2, "real gateway ID-bearing frames must enter native ledger"
    a, b = c._np_ledger.rows
    assert (a.call_id, a.state, a.duration) == ("a", "failed", 1.25)
    assert (b.call_id, b.state, b.duration) == ("b", "succeeded", 0.5)
    assert a.raw_detail == "first\n  || <tag>"
    assert "SECRET-RESULT-BODY" not in repr(c._np_ledger.snapshot())


@pytest.mark.asyncio
async def test_real_runner_enables_existing_id_producer_before_clipping(monkeypatch, tmp_path):
    from tests.gateway.test_telegram_native_progress_runner import run_turn, ScriptedAgent, FINAL
    def run(self, *a, **kw):
        progress = self.tool_progress_callback
        start = getattr(self, "tool_start_callback", None)
        complete = getattr(self, "tool_complete_callback", None)
        for cid, command in [("a", "same prefix " * 10 + "one\n  || <tag>"), ("b", "same prefix " * 10 + "two")]:
            args = {"command": command}
            progress("tool.started", "terminal", command, args)
            if start:
                start(cid, "terminal", args)
        for cid, error, duration in [("b", False, 0.25), ("a", True, 1.25)]:
            progress("tool.completed", "terminal", duration=duration, is_error=error, result="SECRET-RESULT")
            if complete:
                complete(cid, "terminal", {}, {"error": "failed"} if error else {"success": True})
        return {"final_response": FINAL, "messages": [], "api_calls": 1}
    monkeypatch.setattr(ScriptedAgent, "run_conversation", run)
    captured = []
    original = GatewayStreamConsumer._np_persist
    async def persist(c, reason):
        captured.extend(c._np_ledger.snapshot())
        await original(c, reason)
    monkeypatch.setattr(GatewayStreamConsumer, "_np_persist", persist)
    await run_turn(monkeypatch, tmp_path, [], native=True, cleanup=True, session="id-producer")
    assert len(captured) == 2
    assert [r.call_id for r in captured] == ["a", "b"], "real ID hooks must reach Telegram"
    assert [(r.state, r.duration) for r in captured] == [("failed", 1.25), ("succeeded", 0.25)]
    assert captured[0].raw_detail.endswith("one\n  || <tag>")


@pytest.mark.asyncio
async def test_clock_from_real_context_reaches_real_adapter_compose_seam(monkeypatch, tmp_path):
    from tests.gateway.test_telegram_native_progress_runner import run_turn, T, NAP
    from plugins.platforms.telegram import native_progress as renderer
    from gateway.run import TurnRunner
    expected = []
    observed = []
    original_init = TurnRunner.__init__
    def init(self, runner, ctx):
        expected.append(ctx._summary_t0)
        original_init(self, runner, ctx)
    monkeypatch.setattr(TurnRunner, "__init__", init)
    original_compose = renderer.compose_markdown
    def boundary(rows, answer="", *, turn_started_at=None, **kwargs):
        # Boundary shim only accepts Opus's upcoming keyword; base formatting remains real.
        observed.append((turn_started_at, kwargs["now"]))
        return original_compose(rows, answer, **kwargs)
    monkeypatch.setattr(renderer, "compose_markdown", boundary)
    turn = await run_turn(monkeypatch, tmp_path, [T, NAP], native=True, cleanup=True)
    assert turn.api.rich_drafts()
    assert observed and all(start == expected[0] and start <= now for start, now in observed)
