"""Inbound receipts are opt-in: only a proven-durable event writes one.

The platform replay after a crash is the only recovery for input that was
accepted into memory (busy steer, /steer, /queue, an interrupt text, a merged
follow-up). So ``BasePlatformAdapter`` must RELEASE an event's receipt by
default and write it only for an event explicitly marked with
``mark_inbound_durable`` (a committed normal turn, or a fully handled
settings/control command). These tests drive real PTB admission, real
``BasePlatformAdapter`` dispatch, the real ``GatewayRunner`` and a real SQLite
session store, and assert on what a freshly built adapter admits after a crash.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("telegram")

# Imported first: the tests.gateway helpers below stub ``telegram`` when it is
# not already loaded, which would hide the real PTB Application.
from tests.test_telegram_update_admission import (
    _adapter,
    _build_handing_off,
    _marker_runner,
    _process,
    _replayed,
    _text_update,
)  # isort: skip

from gateway.platforms.base import (
    MessageType,
    build_session_key,
    complete_inbound_handoff,
    mark_inbound_durable,
    release_inbound_handoff,
    settle_inbound_handoff,
)
from tests.gateway.test_active_turn_recovery import _close_store_db, _make_db_store
from tests.gateway.test_busy_session_ack import _make_runner
from tests.run_agent.test_steer import _bare_agent


def _sandbox(tmp_path, monkeypatch):
    import gateway.run as gr

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    monkeypatch.setenv("CLOVER_GATEWAY_BUSY_ACK_ENABLED", "false")
    monkeypatch.setenv("CLOVER_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "0")
    monkeypatch.setattr(gr, "_clover_home", home)
    monkeypatch.setattr(gr, "_load_gateway_config", lambda: {})
    return home


# --- the default: anything that is not marked releases ---------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["base_busy", "priority"])
@pytest.mark.parametrize(
    "case",
    [
        "steer", "redirect", "queue", "media_merge", "internal", "interrupt",
        "drain", "drain_write_fails", "slash_steer", "slash_queue",
        "text_queue_fallback",
    ],
)
async def test_busy_input_never_suppresses_replay(tmp_path, monkeypatch, entrypoint, case):
    """Astra round-5 matrix: every busy path leaves the replay admissible."""
    import gateway.unhandled_on_restart as unhandled

    home = _sandbox(tmp_path, monkeypatch)
    adapter = _adapter(tmp_path)
    events = []
    app = _build_handing_off(adapter, 111, events)
    for uid in (50100, 50101):
        await _process(app, _text_update(app.bot, uid))
    event = events[0]
    event.text = "preserve this input"
    if case.startswith("slash_"):
        event.text = "/" + case.removeprefix("slash_") + " preserve this input"
    if case == "media_merge":
        for ev in events:
            ev.message_type = MessageType.PHOTO
            ev.media_urls, ev.media_types = ["/test-owned-image.png"], ["image/png"]
    if case == "internal":
        event.internal = True
        event.allow_gateway_control = False
    store = _make_db_store(home)
    entry = store.get_or_create_session(event.source)
    runner, _ = _make_runner()
    runner.session_store = store
    runner._busy_input_mode = (
        "steer" if case in ("steer", "slash_steer")
        else ("interrupt" if case in ("redirect", "interrupt") else "queue")
    )
    runner._restart_requested = case.startswith("drain")
    runner._draining = case.startswith("drain")
    runner._shutdown_recording = case == "drain_write_fails"
    runner._busy_text_mode = "queue" if case == "text_queue_fallback" else "interrupt"
    if case == "drain_write_fails":
        def fail_append(*args, **kwargs):
            raise OSError("test-owned disk failure")
        monkeypatch.setattr(unhandled, "append_rows", fail_append)
    key = build_session_key(event.source)
    runner.adapters[event.source.platform] = adapter
    accepted = []
    agent = SimpleNamespace(
        steer=lambda text: accepted.append(("steer", text)) or True,
        redirect=lambda text: accepted.append(("redirect", text)) or True,
        interrupt=lambda text: accepted.append(("interrupt", text)),
        _supports_active_turn_redirect=case == "redirect",
        _active_children=[],
        get_activity_summary=lambda: {"seconds_since_activity": 0},
    )
    runner._running_agents[key] = agent
    runner._running_agents_ts[key] = time.time() - 10
    adapter._active_sessions[key] = asyncio.Event()
    adapter._session_tasks[key] = asyncio.current_task()
    adapter._busy_session_handler = runner._handle_active_session_busy_message
    adapter._message_handler = runner._handle_message
    adapter._send_with_retry = AsyncMock()
    adapter._busy_text_debounce_seconds = 0
    try:
        for ev in (events if case == "media_merge" else [event]):
            if entrypoint == "base_busy":
                await asyncio.wait_for(adapter.handle_message(ev), 5)
            else:
                await asyncio.wait_for(runner._handle_message(ev), 5)
                # What BasePlatformAdapter does right after the real handler
                # returns, via the same helper it calls.
                settle_inbound_handoff(ev)
        assert store.load_transcript(entry.session_id) == []
        replay = await _replayed(tmp_path, 50100)
        assert replay, (
            "memory-only input suppressed after rebuild", case, entrypoint,
            accepted, adapter._seen_update_ids,
        )
        assert "111:50100" not in adapter._inflight_update_ids, (
            "busy claim not released", case, entrypoint,
        )
        if case == "media_merge":
            settle_inbound_handoff(adapter._pending_messages[key])
            assert await _replayed(tmp_path, 50100)
            assert await _replayed(tmp_path, 50101)
    finally:
        for state in adapter._text_debounce.values():
            if getattr(state, "task", None):
                state.task.cancel()
        _close_store_db(store)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["steer", "queue"])
async def test_real_busy_slash_payload_survives_crash(tmp_path, monkeypatch, command):
    """Real /steer and /queue while busy accept the payload only in memory."""
    home = _sandbox(tmp_path, monkeypatch)
    adapter = _adapter(tmp_path)
    events = []
    app = _build_handing_off(adapter, 111, events)
    await _process(app, _text_update(app.bot, 61000))
    event = events[0]
    event.text = f"/{command} keep this instruction"
    store = _make_db_store(home)
    entry = store.get_or_create_session(event.source)
    runner, _ = _make_runner()
    runner.session_store = store
    key = build_session_key(event.source)
    runner.adapters[event.source.platform] = adapter
    agent = _bare_agent()
    agent.get_activity_summary = lambda: {"seconds_since_activity": 0}
    runner._running_agents[key] = agent
    runner._running_agents_ts[key] = time.time()
    adapter._active_sessions[key] = asyncio.Event()
    owner = asyncio.current_task()
    assert owner is not None
    adapter._session_tasks[key] = owner
    adapter._busy_session_handler = runner._handle_active_session_busy_message
    adapter._message_handler = runner._handle_message
    adapter._send_with_retry = AsyncMock()
    try:
        await asyncio.wait_for(adapter.handle_message(event), timeout=5)
        # The real command did accept the payload, not reject or merely echo it.
        if command == "steer":
            assert agent._drain_pending_steer() == "keep this instruction"
        else:
            assert adapter._pending_messages[key].text == "keep this instruction"
        assert store.load_transcript(entry.session_id) == []
        assert adapter._send_with_retry.await_count == 1
        replay = await _replayed(tmp_path, 61000)
        assert replay, (
            command, "accepted only in memory, replay suppressed", adapter._seen_update_ids,
        )
    finally:
        _close_store_db(store)


@pytest.mark.asyncio
async def test_priority_interrupt_fallback_leaves_the_replay_admissible(tmp_path, monkeypatch):
    """The legacy running_agent.interrupt(text) fallback holds text in memory only."""
    _sandbox(tmp_path, monkeypatch)
    adapter = _adapter(tmp_path)
    events = []
    app = _build_handing_off(adapter, 111, events)
    await _process(app, _text_update(app.bot, 62000))
    event = events[0]
    event.text = "change course"
    runner, _ = _make_runner()
    runner._busy_input_mode = "interrupt"
    key = build_session_key(event.source)
    runner.adapters[event.source.platform] = adapter
    received = []
    runner._running_agents[key] = SimpleNamespace(
        interrupt=lambda text: received.append(text),
        _active_children=[],
        get_activity_summary=lambda: {"seconds_since_activity": 0},
    )
    runner._running_agents_ts[key] = time.time() - 10

    async def priority_then_base_completion(ev):
        result = await runner._handle_message(ev)
        return result

    adapter._message_handler = priority_then_base_completion
    adapter._send_with_retry = AsyncMock()
    await adapter._process_message_background(event, key)
    assert received == ["change course"]
    assert await _replayed(tmp_path, 62000)


# --- the opt-in: a marked event writes its receipt --------------------------


@pytest.mark.asyncio
async def test_unmarked_handler_gets_no_receipt(tmp_path):
    """Guards the default: a new handler that marks nothing never receipts."""
    adapter = _adapter(tmp_path)
    events = []
    app = _build_handing_off(adapter, 111, events)
    await _process(app, _text_update(app.bot, 70000))

    async def brand_new_handler(event):
        return "handled, I promise"

    adapter._message_handler = brand_new_handler
    adapter._send_with_retry = AsyncMock()
    await adapter._process_message_background(events[0], "k")
    assert "111:70000" not in adapter._seen_update_ids
    assert await _replayed(tmp_path, 70000)


@pytest.mark.asyncio
async def test_handler_exception_gets_no_receipt(tmp_path):
    adapter = _adapter(tmp_path)
    events = []
    app = _build_handing_off(adapter, 111, events)
    await _process(app, _text_update(app.bot, 70001))

    async def exploding_handler(event):
        raise RuntimeError("boom")

    adapter._message_handler = exploding_handler
    adapter._send_with_retry = AsyncMock()
    adapter.send = AsyncMock()
    await adapter._process_message_background(events[0], "k")
    assert await _replayed(tmp_path, 70001)


@pytest.mark.asyncio
async def test_marked_handler_writes_its_receipt(tmp_path):
    adapter = _adapter(tmp_path)
    events = []
    app = _build_handing_off(adapter, 111, events)
    await _process(app, _text_update(app.bot, 70002))

    async def marking_handler(event):
        mark_inbound_durable(event)
        return "done"

    adapter._message_handler = marking_handler
    adapter._send_with_retry = AsyncMock()
    await adapter._process_message_background(events[0], "k")
    assert await _replayed(tmp_path, 70002) == []


@pytest.mark.asyncio
async def test_mark_survives_the_dispatch_copy_the_handler_receives(tmp_path):
    """A pre-dispatch rewrite hands the handler a dataclasses.replace copy."""
    import dataclasses

    adapter = _adapter(tmp_path)
    events = []
    app = _build_handing_off(adapter, 111, events)
    await _process(app, _text_update(app.bot, 70003))
    original = events[0]
    clone = dataclasses.replace(original, text="rewritten")
    mark_inbound_durable(clone)
    # Settling the ORIGINAL (what the adapter holds) must still see the mark
    # through the shared callbacks, or a rewritten command would never receipt.
    settle_inbound_handoff(original)
    assert await _replayed(tmp_path, 70003) == []


@pytest.mark.asyncio
async def test_committed_normal_turn_receipts_only_after_commit(tmp_path):
    events = []
    app = _build_handing_off(_adapter(tmp_path), 111, events)
    await _process(app, _text_update(app.bot, 70004))
    runner = _marker_runner()
    assert await runner._mark_durable_active_turn(events[0], "sk")
    # Turn marker alone: no mark, no receipt; the base default would release.
    assert await _replayed(tmp_path, 70004)
    settle_inbound_handoff(events[0])
    assert await _replayed(tmp_path, 70004)

    events2 = []
    app2 = _build_handing_off(_adapter(tmp_path), 111, events2)
    await _process(app2, _text_update(app2.bot, 70005))
    assert await runner._mark_durable_active_turn(events2[0], "sk2")
    runner._on_inbound_persisted("sk2")  # the user row is committed
    assert await _replayed(tmp_path, 70005) == []


@pytest.mark.asyncio
async def test_turn_that_ends_without_commit_stays_replayable_through_base_settle(tmp_path):
    events = []
    app = _build_handing_off(_adapter(tmp_path), 111, events)
    await _process(app, _text_update(app.bot, 70006))
    runner = _marker_runner()
    assert await runner._mark_durable_active_turn(events[0], "sk")
    runner._forget_inbound_handoff(events[0])
    settle_inbound_handoff(events[0])  # base end-of-handler, never committed
    assert await _replayed(tmp_path, 70006)


@pytest.mark.asyncio
async def test_release_and_complete_are_idempotent_in_both_orders(tmp_path):
    adapter = _adapter(tmp_path)
    events = []
    app = _build_handing_off(adapter, 111, events)
    for uid in (70010, 70011):
        await _process(app, _text_update(app.bot, uid))
    released_first, completed_first = events

    release_inbound_handoff(released_first)
    complete_inbound_handoff(released_first)  # no-op after a release
    release_inbound_handoff(released_first)
    assert await _replayed(tmp_path, 70010)

    complete_inbound_handoff(completed_first)
    release_inbound_handoff(completed_first)  # no-op after a completion
    complete_inbound_handoff(completed_first)
    assert await _replayed(tmp_path, 70011) == []


# --- commands: the table of what may opt in ---------------------------------


@pytest.mark.asyncio
async def test_restart_replay_after_restart_is_suppressed(tmp_path, monkeypatch):
    """/restart is marked durable, so the restarted gateway never loops on it."""
    _sandbox(tmp_path, monkeypatch)
    adapter = _adapter(tmp_path)
    events = []
    app = _build_handing_off(adapter, 111, events)
    await _process(app, _text_update(app.bot, 80000))
    event = events[0]
    event.text = "/restart"
    runner, _ = _make_runner()
    runner.adapters[event.source.platform] = adapter
    runner._handle_restart_command = AsyncMock(return_value="restarting")
    adapter._message_handler = runner._handle_message
    adapter._send_with_retry = AsyncMock()
    await adapter._process_message_background(event, build_session_key(event.source))
    runner._handle_restart_command.assert_awaited_once()
    assert await _replayed(tmp_path, 80000) == []


def test_only_fully_handled_commands_may_opt_in():
    """Contract: nothing that stores text for later can ever be marked."""
    from gateway.run import GatewayRunner

    safe = GatewayRunner._RECEIPT_SAFE_COMMANDS
    memory_only = {
        "steer", "queue", "bg", "btw", "goal", "loop", "subgoal", "heartbeat",
        "moa", "plan", "learn", "init", "blueprint", "retry", "kanban",
    }
    assert not (safe & memory_only)
    assert {"restart", "stop", "new", "update", "approve", "deny", "model"} <= safe


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["restart", "stop", "new", "update", "approve", "deny", "model"])
async def test_marked_commands_receipt_and_payload_commands_release(tmp_path, name):
    from gateway.run import GatewayRunner

    adapter = _adapter(tmp_path)
    events = []
    app = _build_handing_off(adapter, 111, events)
    await _process(app, _text_update(app.bot, 81000))
    runner = object.__new__(GatewayRunner)
    runner._mark_command_receipt_safe(events[0], name)
    settle_inbound_handoff(events[0])
    assert await _replayed(tmp_path, 81000) == []

    app2 = _build_handing_off(adapter, 111, events)
    await _process(app2, _text_update(app2.bot, 81001))
    runner._mark_command_receipt_safe(events[-1], "steer")
    settle_inbound_handoff(events[-1])
    assert await _replayed(tmp_path, 81001)
