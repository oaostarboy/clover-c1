"""A human message queued behind a running turn is never lost silently on restart.

An orderly shutdown records every queued human event that has not started; the
next startup tells that chat once, in plain words, that the message was not
handled so the person can resend it.  Nothing is replayed.

Drives the real ``GatewayRunner.stop()`` and the real startup boot-send path;
only the agent and the network are faked.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clover_constants import get_clover_home
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    build_session_key,
)
from gateway.session import SessionSource
from tests.gateway.restart_test_helpers import RestartTestAdapter, make_restart_runner

FILE_NAME = "gateway_unhandled_on_restart.jsonl"


class _Adapter(RestartTestAdapter):
    """Records sends; ``outcome`` decides what the transport does."""

    def __init__(self, outcome=None, platform=Platform.TELEGRAM):
        BasePlatformAdapter.__init__(self, PlatformConfig(enabled=True, token="***"), platform)
        self.sent: list[str] = []
        self.sent_calls: list[tuple] = []
        self.outcome = outcome or (lambda: SendResult(success=True, message_id="1"))
        self.handled: list[MessageEvent] = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        self.sent_calls.append((chat_id, content, metadata))
        result = self.outcome()
        if isinstance(result, Exception):
            raise result
        return result

    async def handle_message(self, event):
        self.handled.append(event)
        await super().handle_message(event)


def _source(
    chat_id="123", *, profile=None, thread_id=None, name="Ana", platform=Platform.TELEGRAM
) -> SessionSource:
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type="dm",
        user_id="u1",
        user_name=name,
        thread_id=thread_id,
        profile=profile,
    )


def _human(text, *, photo=None, source=None) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.PHOTO if photo else MessageType.TEXT,
        source=source or _source(),
        media_urls=[photo] if photo else [],
        media_types=["image/jpeg"] if photo else [],
    )


def _key(source: SessionSource) -> str:
    return build_session_key(
        source,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
        profile=source.profile,
    )


def _gateway(adapter=None, *, secondary=None, served=None):
    """A fresh runner as the next process would build it."""
    runner, adapter = make_restart_runner(adapter or _Adapter())
    runner.config.multiplex_profiles = bool(secondary) or served is not None
    runner._profile_adapters = {
        name: {Platform.TELEGRAM: ad} for name, ad in (secondary or {}).items()
    }
    runner._busy_text_mode = "queue"
    runner._restart_drain_timeout = 0.0
    runner._background_tasks = set()
    runner._claim_pending_obligations = AsyncMock(return_value=[])
    runner._redeliver_claimed_obligations = AsyncMock(return_value=0)
    runner._send_restart_notification = AsyncMock(return_value=None)
    runner._bind_overflow_enqueuer(adapter)
    for ad in (secondary or {}).values():
        runner._bind_overflow_enqueuer(ad)
    if served is not None:
        patcher = patch("gateway.run._multiplex_profile_homes", return_value=served)
        patcher.start()
        runner._test_patcher = patcher
    return runner, adapter


def _queue_during_turn(runner, adapter, *events):
    """Park events behind a running turn the way /queue and the busy path do."""
    session_key = _key(events[0].source)
    agent = MagicMock()
    runner._running_agents = {session_key: agent}
    agent.interrupt.side_effect = lambda *a, **k: runner._running_agents.clear()
    adapter._active_sessions[session_key] = asyncio.Event()
    for event in events:
        runner._enqueue_fifo(session_key, event, adapter)
    return session_key


async def _orderly_shutdown(runner, *, restart=False):
    with (
        patch("gateway.status.remove_pid_file"),
        patch("gateway.status.write_runtime_status"),
        patch("agent.auxiliary_client.shutdown_cached_clients"),
    ):
        await runner.stop(restart=restart)


async def _startup(runner):
    """The boot-send phase of a start, after the adapters are connected."""
    await runner._await_startup_boot_sends(planned_restart_notification_pending=False)
    tasks = [t for t in list(runner._background_tasks) if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _rows(home: Path) -> list[dict]:
    path = home / FILE_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _stop_patchers():
    yield
    # _gateway() may leave a started patcher on the runner; patch.stopall is
    # the one place that cannot be forgotten.
    patch.stopall()


@pytest.mark.asyncio
async def test_two_queued_humans_get_one_notice_after_restart_and_no_turn_runs():
    runner, adapter = _gateway()
    _queue_during_turn(
        runner,
        adapter,
        _human("please check the invoice\nand the totals"),
        _human("here is the photo", photo="/tmp/a.jpg"),
        MessageEvent(text="child done", source=_source(), internal=True),
    )
    await _orderly_shutdown(runner)

    rows = _rows(get_clover_home())
    assert len(rows) == 2, "one row per queued human; the internal event is not written"
    for row in rows:
        assert row["platform"] == "telegram"
        assert row["chat_id"] == "123"
        assert row["user_name"] == "Ana"
        assert "session_id" not in row and "session_key" not in row

    fresh, fresh_adapter = _gateway()
    await _startup(fresh)

    assert len(fresh_adapter.sent) == 1
    notice = fresh_adapter.sent[0]
    assert "I restarted before handling your last message(s)" in notice
    assert "please check the invoice and the totals" in notice
    assert "[photo]" in notice and "here is the photo" in notice
    assert "Please send again." in notice
    assert fresh_adapter.sent_calls[0][0] == "123"
    assert fresh_adapter.handled == [], "nothing is replayed"
    fresh_adapter._message_handler.assert_not_awaited()
    assert _rows(get_clover_home()) == []


@pytest.mark.asyncio
async def test_preview_is_capped_and_collapsed_and_marks_media():
    runner, adapter = _gateway()
    _queue_during_turn(
        runner,
        adapter,
        _human("x" * 400),
        _human("", photo="/tmp/only-photo.jpg"),
    )
    await _orderly_shutdown(runner)

    previews = [row["preview"] for row in _rows(get_clover_home())]
    assert all(len(p) <= 120 for p in previews)
    assert previews[0] == "x" * 120
    assert previews[1] == "[photo]"


@pytest.mark.asyncio
async def test_many_messages_in_one_chat_collapse_into_one_notice_with_a_count():
    runner, adapter = _gateway()
    _queue_during_turn(runner, adapter, *[_human(f"msg {i}") for i in range(6)])
    await _orderly_shutdown(runner)

    fresh, fresh_adapter = _gateway()
    await _startup(fresh)

    assert len(fresh_adapter.sent) == 1
    assert "+3 more" in fresh_adapter.sent[0]
    assert _rows(get_clover_home()) == []


@pytest.mark.asyncio
async def test_internal_and_synthetic_events_are_not_recorded():
    runner, adapter = _gateway()
    internal = MessageEvent(text="child done", source=_source(), internal=True)
    synthetic = MessageEvent(text="[Continuing toward your standing goal]", source=_source())
    synthetic.synthetic = True
    _queue_during_turn(runner, adapter, internal, _human("a person wrote this"), synthetic)
    await _orderly_shutdown(runner)

    assert [r["preview"] for r in _rows(get_clover_home())] == ["a person wrote this"]


@pytest.mark.asyncio
async def test_follow_up_discarded_by_the_shutdown_drain_is_recorded_once():
    runner, adapter = _gateway()
    runner._draining = True
    source = _source()

    event = _human("dropped by the drain")
    runner._record_unhandled_follow_up(event, None, source)
    runner._record_unhandled_follow_up(None, "typed while interrupting", source)
    runner._record_unhandled_follow_up(
        MessageEvent(text="child done", source=source, internal=True), None, source
    )
    # the same event is also still queued when stop() sweeps the queues
    _queue_during_turn(runner, adapter, event)
    await _orderly_shutdown(runner)

    previews = [row["preview"] for row in _rows(get_clover_home())]
    assert previews == ["dropped by the drain", "typed while interrupting"]


@pytest.mark.asyncio
async def test_notice_send_returning_failure_is_never_resent():
    runner, adapter = _gateway()
    _queue_during_turn(runner, adapter, _human("hello there"))
    await _orderly_shutdown(runner)

    failing = _Adapter(outcome=lambda: SendResult(success=False, error="chat not found"))
    first, _ = _gateway(failing)
    await _startup(first)
    assert len(failing.sent) == 1
    assert _rows(get_clover_home()) == []

    healthy = _Adapter()
    second, _ = _gateway(healthy)
    await _startup(second)
    assert healthy.sent == []


@pytest.mark.asyncio
async def test_notice_send_that_raises_is_never_resent(caplog):
    runner, adapter = _gateway()
    _queue_during_turn(runner, adapter, _human("hello there"))
    await _orderly_shutdown(runner)

    raising = _Adapter(outcome=lambda: RuntimeError("boom"))
    first, _ = _gateway(raising)
    with caplog.at_level(logging.WARNING):
        await _startup(first)
    assert len(raising.sent) == 1
    assert any("hello there" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)

    healthy = _Adapter()
    second, _ = _gateway(healthy)
    await _startup(second)
    assert healthy.sent == []
    assert _rows(get_clover_home()) == []


@pytest.mark.asyncio
async def test_missing_adapter_keeps_the_row_and_the_next_startup_delivers_it():
    runner, adapter = _gateway()
    _queue_during_turn(runner, adapter, _human("please resend me"))
    await _orderly_shutdown(runner)

    offline, _ = _gateway()
    offline.adapters = {}
    await _startup(offline)
    assert len(_rows(get_clover_home())) == 1

    back, back_adapter = _gateway()
    await _startup(back)
    assert len(back_adapter.sent) == 1 and "please resend me" in back_adapter.sent[0]
    assert _rows(get_clover_home()) == []


@pytest.mark.asyncio
async def test_missing_adapter_row_is_dropped_with_a_warning_on_the_third_startup(caplog):
    runner, adapter = _gateway()
    _queue_during_turn(runner, adapter, _human("never deliverable"))
    await _orderly_shutdown(runner)

    for startup in (1, 2, 3):
        offline, _ = _gateway()
        offline.adapters = {}
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            await _startup(offline)
        if startup < 3:
            assert len(_rows(get_clover_home())) == 1, f"kept after startup {startup}"
        else:
            assert _rows(get_clover_home()) == []
            assert any(
                "never deliverable" in r.getMessage()
                for r in caplog.records
                if r.levelno >= logging.WARNING
            )

    healthy = _Adapter()
    later, _ = _gateway(healthy)
    await _startup(later)
    assert healthy.sent == []


@pytest.mark.asyncio
async def test_secondary_profile_uses_its_own_file_and_its_own_adapter(tmp_path):
    root = get_clover_home()
    medicina_home = tmp_path / "profiles" / "medicina"
    medicina_home.mkdir(parents=True)
    served = [("default", root), ("medicina", medicina_home)]

    primary_adapter, secondary_adapter = _Adapter(), _Adapter()
    runner, _ = _gateway(primary_adapter, secondary={"medicina": secondary_adapter}, served=served)
    _queue_during_turn(runner, primary_adapter, _human("root question"))
    sec_source = _source(chat_id="777", profile="medicina", name="Dra")
    _queue_during_turn(runner, secondary_adapter, _human("clinic question", source=sec_source))
    await _orderly_shutdown(runner)

    root_file, sec_file = root / FILE_NAME, medicina_home / FILE_NAME
    assert root_file != sec_file
    assert [r["preview"] for r in _rows(root)] == ["root question"]
    assert [r["preview"] for r in _rows(medicina_home)] == ["clinic question"]

    new_primary, new_secondary = _Adapter(), _Adapter()
    fresh, _ = _gateway(new_primary, secondary={"medicina": new_secondary}, served=served)
    await _startup(fresh)

    assert len(new_primary.sent) == 1 and "root question" in new_primary.sent[0]
    assert new_primary.sent_calls[0][0] == "123"
    assert len(new_secondary.sent) == 1 and "clinic question" in new_secondary.sent[0]
    assert new_secondary.sent_calls[0][0] == "777"
    assert _rows(root) == [] and _rows(medicina_home) == []


@pytest.mark.asyncio
async def test_secondary_profile_row_waits_when_only_the_primary_adapter_is_up(tmp_path):
    root = get_clover_home()
    medicina_home = tmp_path / "profiles" / "medicina"
    medicina_home.mkdir(parents=True)
    served = [("default", root), ("medicina", medicina_home)]

    primary_adapter, secondary_adapter = _Adapter(), _Adapter()
    runner, _ = _gateway(primary_adapter, secondary={"medicina": secondary_adapter}, served=served)
    sec_source = _source(chat_id="777", profile="medicina")
    _queue_during_turn(runner, secondary_adapter, _human("clinic question", source=sec_source))
    await _orderly_shutdown(runner)

    only_primary = _Adapter()
    fresh, _ = _gateway(only_primary, served=served)
    await _startup(fresh)

    assert only_primary.sent == [], "never through another profile's bot"
    assert len(_rows(medicina_home)) == 1


@pytest.mark.asyncio
async def test_threads_in_one_chat_get_separate_notices():
    runner, adapter = _gateway()
    a = _source(thread_id="10")
    b = _source(thread_id="20")
    _queue_during_turn(runner, adapter, _human("in thread ten", source=a))
    _queue_during_turn(runner, adapter, _human("in thread twenty", source=b))
    await _orderly_shutdown(runner)

    fresh, fresh_adapter = _gateway()
    await _startup(fresh)

    assert len(fresh_adapter.sent) == 2
    by_thread = {(c[2] or {}).get("thread_id"): c[1] for c in fresh_adapter.sent_calls}
    assert "in thread ten" in by_thread["10"]
    assert "in thread twenty" in by_thread["20"]


RESEND_REPLY = "I'm restarting and won't be able to handle that — please send it again in a minute."


def _set_skin(monkeypatch, name):
    from clover_cli import skin_engine

    monkeypatch.setattr(skin_engine, "_active_skin", None)
    monkeypatch.setattr(skin_engine, "_active_skin_name", name)


@pytest.mark.asyncio
@pytest.mark.parametrize("skin", ["clover", "default"])
@pytest.mark.parametrize("mode", ["queue", "steer"])
async def test_busy_handler_drain_reply_asks_to_resend_instead_of_promising(monkeypatch, mode, skin):
    _set_skin(monkeypatch, skin)
    runner, adapter = _gateway()
    runner._busy_input_mode = mode
    runner._restart_requested = True
    runner._draining = True
    event = _human("please do this")

    assert await runner._handle_active_session_busy_message(event, _key(event.source)) is True

    assert adapter.sent == [RESEND_REPLY]


@pytest.mark.asyncio
@pytest.mark.parametrize("skin", ["clover", "default"])
@pytest.mark.parametrize("mode", ["queue", "steer"])
async def test_priority_path_drain_reply_asks_to_resend_instead_of_promising(monkeypatch, mode, skin):
    monkeypatch.setenv("CLOVER_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "0")
    _set_skin(monkeypatch, skin)
    runner, adapter = _gateway()
    runner._busy_input_mode = mode
    runner._restart_requested = True
    runner._draining = True
    event = _human("please do this")
    key = _key(event.source)
    agent = MagicMock()
    agent._active_children = []
    runner._running_agents[key] = agent

    reply = await runner._handle_message(event)

    assert str(reply) == RESEND_REPLY


def _previews(home=None):
    return [row["preview"] for row in _rows(home or get_clover_home())]


@pytest.mark.asyncio
async def test_message_that_arrives_while_completion_batches_are_cancelled_is_recorded():
    runner, adapter = _gateway()
    runner._busy_input_mode = "queue"
    _queue_during_turn(runner, adapter, _human("first"))

    async def _late_arrival():
        await adapter.handle_message(_human("arrived mid-teardown"))

    runner._cancel_process_completion_batch_tasks = _late_arrival

    await _orderly_shutdown(runner, restart=True)

    assert adapter.sent.count(RESEND_REPLY) == 1
    assert _previews() == ["first", "arrived mid-teardown"]

    fresh, fresh_adapter = _gateway()
    await _startup(fresh)
    assert len(fresh_adapter.sent) == 1
    assert "arrived mid-teardown" in fresh_adapter.sent[0]


def _second_platform(runner):
    """A live Discord adapter next to the Telegram one, with a busy turn of its own."""
    discord = _Adapter(platform=Platform.DISCORD)
    discord.set_message_handler(AsyncMock(return_value=None))
    discord.set_busy_session_handler(runner._handle_active_session_busy_message)
    runner.adapters[Platform.DISCORD] = discord
    runner._bind_overflow_enqueuer(discord)
    source = _source(chat_id="d1", platform=Platform.DISCORD)
    _queue_during_turn(runner, discord, _human("first on discord", source=source))
    return discord, source


@pytest.mark.asyncio
async def test_message_delivered_to_a_live_adapter_while_another_one_disconnects_is_recorded():
    runner, telegram = _gateway()
    runner._busy_input_mode = "queue"
    discord, source = _second_platform(runner)

    async def _telegram_disconnect():
        await discord.handle_message(_human("arrived during telegram teardown", source=source))

    telegram.disconnect = _telegram_disconnect

    await _orderly_shutdown(runner, restart=True)

    assert sorted(_previews()) == ["arrived during telegram teardown", "first on discord"]


@pytest.mark.asyncio
async def test_message_arriving_while_an_adapter_cancels_its_tasks_is_recorded():
    runner, adapter = _gateway()
    runner._busy_input_mode = "queue"
    _queue_during_turn(runner, adapter, _human("first"))
    real_cancel = adapter.cancel_background_tasks

    async def _cancel_with_a_late_arrival():
        await adapter.handle_message(_human("arrived while cancelling"))
        await real_cancel()

    adapter.cancel_background_tasks = _cancel_with_a_late_arrival

    await _orderly_shutdown(runner, restart=True)

    assert _previews() == ["first", "arrived while cancelling"]


@pytest.mark.asyncio
async def test_message_acknowledged_just_before_an_abrupt_exit_is_already_on_disk():
    class _HardExit(Exception):
        pass

    runner, adapter = _gateway()
    runner._busy_input_mode = "queue"
    _queue_during_turn(runner, adapter, _human("first"))

    async def _late_arrival_then_the_process_dies():
        await adapter.handle_message(_human("acknowledged, then the exit"))
        raise _HardExit()

    runner._cancel_process_completion_batch_tasks = _late_arrival_then_the_process_dies

    try:
        await _orderly_shutdown(runner, restart=True)
    except _HardExit:
        pass

    assert adapter.sent.count(RESEND_REPLY) == 1
    assert _previews() == ["first", "acknowledged, then the exit"]


@pytest.mark.asyncio
async def test_debounced_queue_mode_text_is_recorded_not_lost():
    runner, adapter = _gateway()
    adapter._busy_text_mode = "queue"
    adapter._busy_text_debounce_seconds = 60.0
    adapter._busy_text_hard_cap_seconds = 120.0
    _queue_during_turn(runner, adapter, _human("first"))

    await adapter.handle_message(_human("second, still in the debounce buffer"))
    assert adapter._text_debounce_store(), "the text is buffered, not yet queued"

    await _orderly_shutdown(runner)

    assert _previews() == ["first", "second, still in the debounce buffer"]


@pytest.mark.asyncio
async def test_early_exit_right_after_the_drain_still_leaves_the_rows():
    class _HardExit(Exception):
        pass

    runner, adapter = _gateway()
    _queue_during_turn(runner, adapter, _human("first"), _human("second"))

    async def _exit_before_any_cleanup(_active_agents):
        raise _HardExit()

    runner._finalize_shutdown_agents = _exit_before_any_cleanup

    try:
        await _orderly_shutdown(runner)
    except _HardExit:
        pass

    assert _previews() == ["first", "second"]


@pytest.mark.asyncio
async def test_adapter_teardown_records_what_it_is_about_to_discard():
    runner, adapter = _gateway()
    runner._shutdown_recording = True
    session_key = _queue_during_turn(runner, adapter, _human("sitting in the slot"))
    adapter._local_overflow_store()[session_key] = [_human("sitting in the local overflow")]

    await adapter.cancel_background_tasks()

    assert adapter._pending_messages == {}
    assert sorted(_previews()) == ["sitting in the local overflow", "sitting in the slot"]


@pytest.mark.asyncio
async def test_refused_arrival_during_shutdown_gets_the_full_reply_and_no_row():
    runner, adapter = _gateway()
    runner._busy_input_mode = "queue"
    runner._BUSY_QUEUE_MAX_PENDING = 1
    _queue_during_turn(runner, adapter, _human("first"))

    async def _late_arrival():
        await adapter.handle_message(_human("one too many"))

    runner._cancel_process_completion_batch_tasks = _late_arrival

    await _orderly_shutdown(runner, restart=True)

    assert adapter.sent.count("I'm backed up — please resend that in a moment.") == 1
    assert RESEND_REPLY not in adapter.sent
    assert _previews() == ["first"]
