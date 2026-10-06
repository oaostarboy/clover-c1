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
