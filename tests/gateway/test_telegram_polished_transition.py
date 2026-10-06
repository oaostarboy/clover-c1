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


class DocumentApi(FakeTelegramApi):
    async def send_document(self, **kwargs):
        # Consume the actual open file at the wire edge, not a stored DB assertion.
        record = {k: v for k, v in kwargs.items() if k != "document"}
        record["document_bytes"] = kwargs["document"].read()
        record["document_mode"] = __import__("os").fstat(kwargs["document"].fileno()).st_mode & 0o777
        await self._enter("send_document", record)
        record["_message_id"] = next(self._ids)
        return SimpleNamespace(message_id=record["_message_id"])


def document_wire(api):
    api.send_document = DocumentApi.send_document.__get__(api)


@pytest.mark.asyncio
async def test_short_success_expands_diagnostics_in_existing_card_without_document(monkeypatch, tmp_path):
    from tests.gateway.test_telegram_native_progress_runner import run_turn, FINAL, unmd
    turn = await run_turn(monkeypatch, tmp_path, [
        ("tool", "terminal", "pwd", {"command": "pwd"}),
        ("done", "terminal", 0.125, False),
    ], native=True, cleanup=True, api_setup=document_wire)
    cards = [unmd(kw["text"]) for kw in turn.api.methods("send_message") if "tool call" in kw["text"]]
    assert len(cards) == 1
    assert "pwd" in cards[0] and "0.125" in cards[0] and "succeeded" in cards[0]
    assert not turn.api.methods("send_document")
    assert cards[0] in unmd(turn.api.methods("send_message")[0]["text"])


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_document", [False, True])
async def test_lossy_or_large_details_are_retrievable_once_with_truthful_history(monkeypatch, tmp_path, fail_document):
    from tests.gateway.test_telegram_native_progress_runner import run_turn, FINAL, unmd
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    command = "printf '<b> ||'\n  " + "argument " * 700 + "tail"
    def setup(api):
        document_wire(api)
        if fail_document:
            api.fail["send_document"] = RuntimeError("offline")
    turn = await run_turn(monkeypatch, tmp_path, [
        ("tool", "terminal", command[:40], {"command": command}),
        ("done", "terminal", 2.25, True),
    ], native=True, cleanup=True, api_setup=setup, session="doc-lossless")
    docs = turn.api.methods("send_document")
    assert len(docs) == 1, "exactly one upload attempt, never duplicate retry"
    doc = docs[0]
    payload = doc["document_bytes"].decode("utf-8")
    assert command in payload and "failed" in payload and "2.25" in payload
    assert "SECRET-RESULT-PAYLOAD" not in payload
    assert doc["document_mode"] == 0o600
    assert doc["filename"] == "activity-details.txt" and doc["chat_id"] == 12345
    persistent = [unmd(m["text"]) for m in turn.api.persistent_messages()]
    assert sum("tool call" in t for t in persistent) == 1
    assert ("Full details attached" in "\n".join(persistent)) is not fail_document
    assert not any(str(tmp_path) in t for t in persistent)
    files = list((tmp_path / "workspace" / "native-activity").glob("*.txt"))
    assert len(files) == 1 and command in files[0].read_text()
    if fail_document:
        assert "tail" in "\n".join(persistent), "document failure must preserve persistent history, not suppress on count-card success"
    calls = turn.api.calls
    document_i = next(i for i, (m, _) in enumerate(calls) if m == "send_document")
    card_i = next(i for i, (m, kw) in enumerate(calls) if m == "send_message" and "tool call" in kw.get("text", ""))
    final_i = next(i for i, (m, kw) in enumerate(calls) if turn.api.is_answer_call(m, kw, FINAL))
    assert document_i < card_i < final_i
    from tests.gateway.test_telegram_native_progress_history import fire_cleanup
    cb = await fire_cleanup(turn)
    result = cb()
    if __import__("inspect").isawaitable(result):
        await result
    await asyncio.sleep(0.05)
    assert len(turn.api.methods("send_document")) == 1


@pytest.mark.asyncio
async def test_silent_turn_never_delivers_diagnostic_document(monkeypatch, tmp_path):
    from tests.gateway.test_telegram_native_progress_runner import run_turn, ScriptedAgent
    original = ScriptedAgent.run_conversation
    def run(self, *a, **kw):
        original(self, *a, **kw)
        return {"final_response": "[SILENT]", "messages": [], "api_calls": 1}
    monkeypatch.setattr(ScriptedAgent, "run_conversation", run)
    turn = await run_turn(monkeypatch, tmp_path, [
        ("tool", "terminal", "long", {"command": "one\n  two"}),
    ], native=True, cleanup=True, api_setup=document_wire, send_final_delta=False)
    assert not turn.api.methods("send_document")
    assert not any("tool call" in m["text"] for m in turn.api.persistent_messages()), "SILENT must not post a success card"
