"""Human-first ordering and merge contract for busy-session follow-ups (I3).

A human message that arrives while an internal event (async-delegation or
background-process completion) is parked must run as the next turn.  Internal
and synthetic events are deferred, kept in order among themselves, and never
merged with — or allowed to overwrite — events of another class.  A full queue
is never silent.

Drives the real ``BasePlatformAdapter`` guard + busy handler + the runner's
drain helpers; only the agent and the network are faked.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MergeResult,
    MessageEvent,
    MessageType,
    SendResult,
    build_session_key,
    merge_pending_message_event,
)
from gateway.run import GatewayRunner, _dequeue_pending_event
from gateway.session import SessionSource

BACKED_UP_REPLY = "I'm backed up — please resend that in a moment."


class _Adapter(BasePlatformAdapter):
    def __init__(self) -> None:
        super().__init__(PlatformConfig(enabled=True, token="t"), Platform.TELEGRAM)
        self.sent: list[str] = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id="m")

    async def _send_with_retry(self, chat_id, content, reply_to=None, metadata=None, **kw):
        self.sent.append(content)
        return SendResult(success=True, message_id="m")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


def _source(user: str = "u1") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", user_id=user
    )


def _human(text: str, *, photo: str | None = None, user: str = "u1") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.PHOTO if photo else MessageType.TEXT,
        source=_source(user),
        media_urls=[photo] if photo else [],
        media_types=["image/jpeg"] if photo else [],
    )


def _internal(text: str = "[ASYNC DELEGATION COMPLETE] child done") -> MessageEvent:
    return MessageEvent(
        text=text, message_type=MessageType.TEXT, source=_source(), internal=True
    )


def _synthetic(text: str = "[Continuing toward your standing goal]\nGoal: x") -> MessageEvent:
    event = MessageEvent(text=text, message_type=MessageType.TEXT, source=_source())
    event.synthetic = True
    return event


class _Harness:
    def __init__(self, monkeypatch, *, mode: str = "interrupt", text_mode: str | None = None):
        monkeypatch.setenv("CLOVER_GATEWAY_BUSY_ACK_ENABLED", "false")
        self.adapter = _Adapter()
        self.adapter.set_message_handler(AsyncMock())
        self.adapter._busy_text_mode = text_mode or mode
        self.adapter._busy_text_debounce_seconds = 0.0

        runner = object.__new__(GatewayRunner)
        runner.config = MagicMock()
        runner.config.multiplex_profiles = False
        runner._busy_input_mode = mode
        runner._busy_text_mode = text_mode or mode
        runner._draining = False
        runner._running_agents_ts = {}
        runner._background_tasks = set()
        runner._profile_adapters = {}
        runner.adapters = {Platform.TELEGRAM: self.adapter}
        runner.session_store = None
        runner._is_user_authorized = lambda _s: True
        runner._admit_bot_message = lambda _s: True
        runner._session_has_compression_in_flight = AsyncMock(return_value=False)
        self.runner = runner

        self.sk = build_session_key(
            _source(),
            group_sessions_per_user=self.adapter.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.adapter.config.extra.get("thread_sessions_per_user", False),
            profile=self.adapter._session_key_profile(_source()),
        )
        self.parent = MagicMock()
        self.parent._active_children = []
        self.parent._active_children_lock = threading.Lock()
        runner._session_state(self.sk).turn.agent = self.parent
        self.adapter._active_sessions[self.sk] = asyncio.Event()

        self.adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
        runner._bind_overflow_enqueuer(self.adapter)

    async def send(self, event: MessageEvent) -> None:
        await self.adapter.handle_message(event)
        await self.settle()

    async def settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0)
        tasks = [t for t in list(self.runner._background_tasks) if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # queue-mode text debounce: force-flush so the test is deterministic
        await self.adapter._flush_text_debounce_now(self.sk)

    def drain(self) -> MessageEvent | None:
        head = _dequeue_pending_event(self.adapter, self.sk)
        return self.runner._promote_queued_event(self.sk, self.adapter, head)

    def drain_all(self) -> list[MessageEvent]:
        out = []
        while (event := self.drain()) is not None:
            out.append(event)
        return out

    def queued(self) -> list[MessageEvent]:
        state = self.runner._peek_session_state(self.sk)
        overflow = list(state.conversation.queued_events) if state else []
        head = self.adapter._pending_messages.get(self.sk)
        return ([head] if head is not None else []) + overflow


@pytest.fixture
def h(monkeypatch):
    return _Harness(monkeypatch)


@pytest.mark.asyncio
async def test_human_photo_interrupt_is_next_turn_ahead_of_internal_completion(h):
    internal = _internal()
    await h.send(internal)
    await h.send(_human("please answer me", photo="a.jpg"))

    first = h.drain()
    assert first is not None
    assert first.internal is False
    assert first.text == "please answer me"
    assert first.media_urls == ["a.jpg"]
    h.parent.interrupt.assert_called()


@pytest.mark.asyncio
async def test_human_text_interrupt_is_next_turn_ahead_of_internal_completion(h):
    await h.send(_internal())
    await h.send(_human("please answer me"))

    first = h.drain()
    assert first is not None
    assert first.internal is False
    assert first.text == "please answer me"
    h.parent.interrupt.assert_called_with("please answer me")


@pytest.mark.asyncio
async def test_internal_completion_runs_after_human_turn_unmerged_and_still_internal(h):
    await h.send(_internal("child done"))
    await h.send(_human("please answer me"))

    assert h.drain().text == "please answer me"
    second = h.drain()
    assert second is not None
    assert second.internal is True
    assert second.text == "child done"
    assert h.drain() is None


@pytest.mark.asyncio
async def test_queued_humans_keep_arrival_order_and_media_merge_ahead_of_internals(h):
    await h.send(_internal("child done"))
    await h.send(_human("first"))
    await h.send(_human("", photo="a.jpg"))
    await h.send(_human("album caption", photo="b.jpg"))
    await h.send(_human("last"))

    drained = h.drain_all()

    assert [e.internal for e in drained] == [False, False, False, True]
    assert drained[0].text == "first"
    # the photo burst merged into one album turn, in arrival order
    assert drained[1].media_urls == ["a.jpg", "b.jpg"]
    assert drained[1].text == "album caption"
    assert drained[2].text == "last"
    assert drained[3].text == "child done"


@pytest.mark.asyncio
async def test_internal_events_keep_fifo_among_themselves(h):
    await h.send(_internal("one"))
    await h.send(_internal("two"))
    await h.send(_internal("three"))

    drained = h.drain_all()

    assert [e.text for e in drained] == ["one", "two", "three"]
    assert all(e.internal for e in drained)


@pytest.mark.asyncio
async def test_synthetic_prompts_never_outrank_a_parked_internal(h):
    # goal continuation parked first, then a completion arrives
    h.runner._enqueue_fifo(h.sk, _synthetic("goal tick"), h.adapter)
    await h.send(_internal("child done"))

    drained = h.drain_all()

    assert [e.text for e in drained] == ["child done", "goal tick"]


@pytest.mark.asyncio
async def test_synthetic_head_does_not_outrank_a_later_human(h):
    h.runner._enqueue_fifo(h.sk, _synthetic("heartbeat"), h.adapter)
    await h.send(_human("hello"))

    assert [e.text for e in h.drain_all()] == ["hello", "heartbeat"]


@pytest.mark.asyncio
async def test_busy_queue_mode_text_is_not_absorbed_into_parked_internal(monkeypatch):
    h = _Harness(monkeypatch, mode="queue", text_mode="queue")
    await h.send(_internal("child done"))
    await h.send(_human("please answer me"))

    drained = h.drain_all()

    assert [(e.internal, e.text) for e in drained] == [
        (False, "please answer me"),
        (True, "child done"),
    ]


@pytest.mark.asyncio
async def test_internal_after_human_photo_is_not_merged_into_caption(h):
    await h.send(_human("look at this", photo="a.jpg"))
    await h.send(_internal("child done"))

    drained = h.drain_all()

    assert [(e.internal, e.text) for e in drained] == [
        (False, "look at this"),
        (True, "child done"),
    ]
    assert drained[0].media_urls == ["a.jpg"]


def test_merge_contract_never_overwrites_or_merges_across_classes():
    slot: dict[str, MessageEvent] = {}
    human = _human("h")
    internal = _internal("i")
    synthetic = _synthetic("s")

    assert merge_pending_message_event(slot, "k", human) is MergeResult.STORED
    assert merge_pending_message_event(slot, "k", internal) is MergeResult.REFUSED
    assert merge_pending_message_event(slot, "k", synthetic) is MergeResult.REFUSED
    assert slot["k"] is human and human.text == "h"

    slot = {"k": internal}
    assert merge_pending_message_event(slot, "k", _internal("i2")) is MergeResult.REFUSED
    assert merge_pending_message_event(slot, "k", human) is MergeResult.REFUSED
    assert slot["k"] is internal and internal.text == "i"

    # humans from a different sender are a different security context
    slot = {"k": human}
    other = _human("other", user="u2")
    assert merge_pending_message_event(slot, "k", other) is MergeResult.REFUSED
    assert slot["k"] is human

    # human + human keeps the existing media/text merge rules
    slot = {"k": _human("a", photo="a.jpg")}
    result = merge_pending_message_event(slot, "k", _human("b", photo="b.jpg"))
    assert result is MergeResult.MERGED
    assert slot["k"].media_urls == ["a.jpg", "b.jpg"]


@pytest.mark.asyncio
async def test_adapter_without_runner_drains_local_queue_human_first_then_fifo():
    adapter = _Adapter()
    adapter.set_message_handler(AsyncMock())
    sk = build_session_key(
        _source(), profile=adapter._session_key_profile(_source())
    )
    adapter._active_sessions[sk] = asyncio.Event()
    adapter.set_busy_session_handler(None)

    await adapter.handle_message(_internal("one"))
    await adapter.handle_message(_human("two"))
    await adapter.handle_message(_internal("three"))

    got = []
    while (event := adapter.get_pending_message(sk)) is not None:
        got.append(event.text)
    # same class order as the runner: the human first, internals in arrival order
    assert got == ["two", "one", "three"]


@pytest.mark.asyncio
async def test_more_than_depth_cap_recursions_with_mixed_classes_lose_nothing(h):
    sent = [
        _internal("i1"), _human("h1"), _synthetic("s1"), _human("h2", photo="p.jpg"),
        _internal("i2"), _human("h3"), _synthetic("s2"), _human("h4"),
    ]
    for event in sent:
        if event.internal or getattr(event, "synthetic", False):
            if getattr(event, "synthetic", False):
                h.runner._enqueue_fifo(h.sk, event, h.adapter)
            else:
                await h.send(event)
        else:
            await h.send(event)

    processed: list[MessageEvent] = []
    depth = 0
    for _guard in range(100):
        pending = h.drain()
        if pending is None:
            break
        if depth >= h.runner._MAX_INTERRUPT_DEPTH:
            h.runner._defer_followup_at_depth_cap(h.sk, h.adapter, pending, None, _source())
            depth = 0  # the adapter hands the slot to a fresh task
            continue
        processed.append(pending)
        depth += 1

    texts = [e.text for e in processed]
    assert sorted(texts) == sorted(e.text for e in sent), "lost or duplicated an event"
    humans = [e.text for e in processed if not e.internal and not getattr(e, "synthetic", False)]
    internals = [e.text for e in processed if e.internal]
    synth = [e.text for e in processed if getattr(e, "synthetic", False)]
    assert humans == ["h1", "h2", "h3", "h4"]
    assert internals == ["i1", "i2"]
    assert synth == ["s1", "s2"]
    # class order holds across the cap boundary
    assert texts.index("h4") < texts.index("i1") < texts.index("s1")


def test_depth_cap_requeues_text_only_follow_up_at_the_front_as_a_human_turn(h):
    h.adapter._pending_messages[h.sk] = _internal("child done")

    h.runner._defer_followup_at_depth_cap(
        h.sk, h.adapter, None, "typed while interrupted", _source()
    )

    drained = h.drain_all()
    assert [(e.internal, e.text) for e in drained] == [
        (False, "typed while interrupted"),
        (True, "child done"),
    ]


def test_human_interrupt_message_is_not_dropped_behind_an_internal_head(h):
    internal = _internal("child done")

    event, text = h.runner._reconcile_interrupt_with_queue(
        h.sk, h.adapter, internal, "please answer me", _source()
    )

    assert event is None
    assert text == "please answer me"
    # the internal head was kept, not discarded
    assert [e.text for e in h.queued()] == ["child done"]


def test_queued_human_event_is_run_once_not_again_from_interrupt_message(h):
    human = _human("please answer me")

    event, text = h.runner._reconcile_interrupt_with_queue(
        h.sk, h.adapter, human, "please answer me", _source()
    )

    assert event is human
    assert text is None
    assert h.queued() == []


@pytest.mark.asyncio
async def test_queue_full_human_gets_a_reply_and_nothing_is_dropped_silently(h):
    cap = h.runner._BUSY_QUEUE_MAX_PENDING
    for i in range(cap):
        await h.send(_human(f"msg {i}"))
    assert len([e for e in h.queued() if not e.internal]) == cap

    await h.send(_human("one too many"))

    assert BACKED_UP_REPLY in h.adapter.sent
    assert len(h.queued()) == cap
    assert "one too many" not in [e.text for e in h.queued()]


@pytest.mark.asyncio
async def test_queue_full_does_not_block_internal_completions(h):
    cap = h.runner._BUSY_QUEUE_MAX_PENDING
    for i in range(cap):
        await h.send(_human(f"msg {i}"))

    await h.send(_internal("child done"))

    assert "child done" in [e.text for e in h.queued()]
    assert BACKED_UP_REPLY not in h.adapter.sent


@pytest.mark.asyncio
async def test_slash_queue_still_gives_one_fifo_turn_per_item(h):
    for text in ("a", "b", "c"):
        h.runner._enqueue_fifo(h.sk, _human(text), h.adapter)

    assert [e.text for e in h.drain_all()] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_goal_continuation_producer_marks_event_synthetic(tmp_path, monkeypatch):
    from unittest.mock import patch

    from clover_cli import goals
    from clover_cli.goals import GoalManager
    from gateway.session import SessionEntry

    goals._DB_CACHE.clear()
    source = _source()
    adapter = _Adapter()
    sk = build_session_key(source)
    runner = object.__new__(GatewayRunner)
    runner.config = MagicMock()
    runner._queued_events = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    entry = SessionEntry(
        session_key=sk,
        session_id="goal-sess-p2a",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store._generate_session_key.return_value = sk
    GoalManager(entry.session_id).set("ship it")
    with patch(
        "clover_cli.goals.judge_goal",
        return_value=("continue", "needs work", False, None, False),
    ):
        await runner._post_turn_goal_continuation(
            session_entry=entry, source=source, final_response="partial"
        )

    event = adapter._pending_messages[sk]
    assert event.text.startswith("[Continuing toward your standing goal]")
    assert event.synthetic is True
    assert event.internal is False
    goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_heartbeat_producer_marks_event_synthetic(monkeypatch):
    from unittest.mock import patch

    source = _source()
    adapter = _Adapter()
    sk = build_session_key(source)
    runner = object.__new__(GatewayRunner)
    runner._queued_events = {}
    runner._running_agents_ts = {}
    runner._background_tasks = set()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    runner._heartbeat_poll_task = None
    runner._heartbeat_watch = {sk: (source, "hb-session")}
    runner._warm_goals_session_db = AsyncMock()

    class _FakeHeartbeat:
        def __init__(self, session_id):
            pass

        def has_heartbeat(self):
            return True

        def due_prompt(self):
            return "heartbeat tick"

    with patch("clover_cli.heartbeat.POLL_SECONDS", 0.01), patch(
        "clover_cli.heartbeat.HeartbeatManager", _FakeHeartbeat
    ):
        runner._start_heartbeat_poller()
        for _ in range(100):
            if sk in adapter._pending_messages:
                break
            await asyncio.sleep(0.01)
        runner._heartbeat_poll_task.cancel()
        await asyncio.gather(runner._heartbeat_poll_task, return_exceptions=True)

    event = adapter._pending_messages[sk]
    assert event.text == "heartbeat tick"
    assert event.synthetic is True


@pytest.mark.asyncio
async def test_events_parked_while_the_response_is_being_sent_still_run_human_first(monkeypatch):
    """The adapter's own post-response drain applies the same class order.

    Events that arrive while the previous turn's response is still being sent
    never pass through the runner's drain; the adapter hands them to a fresh
    turn itself and must pick the human ahead of the internal head.
    """
    monkeypatch.setenv("CLOVER_GATEWAY_BUSY_ACK_ENABLED", "false")
    ran: list[tuple[bool, str]] = []
    sending = asyncio.Event()
    release = asyncio.Event()

    class _SlowSendAdapter(_Adapter):
        async def _send_with_retry(self, chat_id, content, reply_to=None, metadata=None, **kw):
            if content == "reply:running":
                sending.set()
                await release.wait()
            return SendResult(success=True, message_id="m")

    adapter = _SlowSendAdapter()

    async def handler(event):
        ran.append((event.internal, event.text))
        return f"reply:{event.text}"

    adapter.set_message_handler(handler)
    harness = _Harness(monkeypatch)
    runner = harness.runner
    runner.adapters = {Platform.TELEGRAM: adapter}
    sk = harness.sk
    runner._session_state(sk).turn.agent = None
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    runner._bind_overflow_enqueuer(adapter)

    await adapter.handle_message(_human("running"))
    await asyncio.wait_for(sending.wait(), 5)

    # response of the first turn is mid-send; an internal completion parks in
    # the head slot, then a human message queues behind it
    await adapter.handle_message(_internal("child done"))
    await adapter.handle_message(_human("answer me"))
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    for _ in range(200):
        if len(ran) >= 3:
            break
        await asyncio.sleep(0.01)

    assert [text for _internal_flag, text in ran] == ["running", "answer me", "child done"]
    assert ran[2][0] is True


async def _arrive(h: _Harness, event: MessageEvent) -> None:
    """Deliver through the real ``handle_message`` and leave the debounce timer alone."""
    await h.adapter.handle_message(event)
    for _ in range(3):
        await asyncio.sleep(0)


def _buffering_harness(monkeypatch) -> _Harness:
    h = _Harness(monkeypatch, mode="queue", text_mode="queue")
    h.adapter._busy_text_debounce_seconds = 60.0
    h.adapter._busy_text_hard_cap_seconds = 120.0
    return h


@pytest.mark.asyncio
async def test_three_senders_in_one_busy_session_are_all_handled_in_order(monkeypatch):
    h = _buffering_harness(monkeypatch)
    try:
        await _arrive(h, _human("from A", user="A"))
        await _arrive(h, _human("from B", user="B"))
        await _arrive(h, _human("from C", user="C"))

        drained = h.drain_all()

        assert [(e.source.user_id, e.text) for e in drained] == [
            ("A", "from A"),
            ("B", "from B"),
            ("C", "from C"),
        ]
        assert h.adapter.sent == [], "nobody was refused, so nobody gets a reply"
    finally:
        for state in list(h.adapter._text_debounce_store().values()):
            if state.task is not None:
                state.task.cancel()


@pytest.mark.asyncio
async def test_buffered_human_from_another_sender_runs_before_an_internal_head(monkeypatch):
    h = _buffering_harness(monkeypatch)
    try:
        await _arrive(h, _internal("child done"))
        await _arrive(h, _human("please answer me", user="B"))

        drained = h.drain_all()

        assert [(e.internal, e.text) for e in drained] == [
            (False, "please answer me"),
            (True, "child done"),
        ]
    finally:
        for state in list(h.adapter._text_debounce_store().values()):
            if state.task is not None:
                state.task.cancel()


@pytest.mark.asyncio
async def test_full_queue_tells_a_buffered_sender_instead_of_dropping_silently(monkeypatch):
    h = _buffering_harness(monkeypatch)
    h.runner._BUSY_QUEUE_MAX_PENDING = 2
    try:
        await _arrive(h, _human("from A", user="A"))
        await _arrive(h, _human("from B", user="B"))
        await _arrive(h, _human("from C", user="C"))
        await h.adapter._flush_text_debounce_now(h.sk)
        await h.settle()

        delivered = [e.text for e in h.drain_all()]

        assert delivered == ["from A", "from B"]
        assert h.adapter.sent == [BACKED_UP_REPLY]
    finally:
        for state in list(h.adapter._text_debounce_store().values()):
            if state.task is not None:
                state.task.cancel()
