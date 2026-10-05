"""Native Stop (``stopped_message_generation``) through the real gateway.

Real pieces: ``TelegramAdapter`` (+ the base adapter session loop that spawns the
turn and drains queued follow-ups), ``GatewayRunner._handle_message_with_agent``
(including its stale-generation discard), ``_run_agent``, the stream consumer, the
generation/interrupt machinery and the Telegram handler — over the in-memory Bot
API connector.  Only the fake agent is a stand-in: it blocks until the gateway
interrupts it, exactly like a long-running turn.  ``StopRunner`` overrides just
the pre-processing of ``_handle_message`` (auth/commands/session lookup).
"""

import asyncio
import re
import sys
import threading
import time
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig, StreamingConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key
from gateway.stream_consumer import GatewayStreamConsumer
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway._telegram_fake_api import FakeTelegramApi
from tests.gateway.test_telegram_native_progress_runner import DEFAULT_DISPLAY

FOLLOWUP = "FOLLOWUPANSWER"
STOCK_STOP = "STOCKSTOPACK"


class BlockingAgent:
    """Turn that runs until the gateway interrupts it (like a long tool call)."""

    started: list = []
    instances: list = []
    release = threading.Event()          # lets a turn that was NOT stopped finish
    interrupts: list = []

    def __init__(self, **kwargs):
        self.tools = []
        self.kwargs = kwargs
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.stream_delta_callback = kwargs.get("stream_delta_callback")
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.reasoning_callback = None
        self.session_id = kwargs.get("session_id")
        self.is_interrupted = False
        self.message = None
        BlockingAgent.instances.append(self)

    def hard_interrupt(self, message=None, **kw):
        self.is_interrupted = True
        BlockingAgent.interrupts.append(message)

    interrupt = hard_interrupt

    def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
        # the real handler also passes persist_user_message & co.
        self.message = message
        BlockingAgent.started.append(message)
        # a turn that owns background processes (so a reaper pass would have work to do)
        if not getattr(self, "_gateway_turn_process_task_id", None):
            self._gateway_turn_process_task_id = "tid-" + str(message)
        if getattr(self, "_gateway_turn_process_baseline", None) is None:
            self._gateway_turn_process_baseline = {"baseline": 1}
        if "second question" not in message and "hello" in message:
            cb = self.tool_progress_callback
            if cb:
                cb("tool.started", "web_search", "sony reviews", {"query": "sony reviews"})
            if self.stream_delta_callback:
                self.stream_delta_callback("partial answer so far ")
            time.sleep(0.3)   # a turn that outlives the consumer's first pump (frames can go out)
            deadline = time.monotonic() + 15
            while not self.is_interrupted and not BlockingAgent.release.is_set() and time.monotonic() < deadline:
                time.sleep(0.02)
            if self.is_interrupted:
                return {"final_response": "", "interrupted": True, "messages": [], "api_calls": 1}
            if self.stream_delta_callback:
                self.stream_delta_callback("first answer")
            return {"final_response": "first answer", "response_previewed": True, "messages": [], "api_calls": 1}
        if self.stream_delta_callback:
            self.stream_delta_callback(FOLLOWUP)
        return {"final_response": FOLLOWUP, "response_previewed": True, "messages": [], "api_calls": 1}


def stop_update(chat_id, draft_id, *, chat_type="private", thread=None, drop=(), raw=None):
    payload = {"chat": {"id": chat_id, "type": chat_type}, "draft_id": draft_id}
    if thread is not None:
        payload["message_thread_id"] = thread
    for key in drop:
        payload.pop(key, None)
    return SimpleNamespace(
        stopped_message_generation=None,
        api_kwargs={"stopped_message_generation": payload if raw is None else raw},
    )


class StopRunner(gateway_run.GatewayRunner):
    """Real runner; only ``_handle_message``'s pre-processing is replaced."""

    async def _handle_message(self, event):
        adapter = self.adapters[Platform.TELEGRAM]
        source = event.source
        key = build_session_key(
            source,
            group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
            profile=adapter._session_key_profile(source),
        )
        # The claim / generation / release lifecycle below mirrors the real
        # ``_handle_message`` (claim the slot, begin a generation, always release
        # the slot and the turn lease on exit) so stop + follow-up behave as in prod.
        lease, limit_message = self._claim_active_session_slot(key, source)
        assert limit_message is None
        state = self._session_state(key)
        if lease is not None:
            state.turn.lease = lease
        state.turn.agent = gateway_run._AGENT_PENDING_SENTINEL
        state.turn.started_ts = time.time()
        generation = self._begin_session_run_generation(key)
        try:
            return await self._handle_message_with_agent(event, source, key, generation)
        finally:
            self._release_running_agent_state(key)
            self._release_turn_lease(key, generation)


class Env:
    def __init__(self, monkeypatch, tmp_path, *, allowed=("12345", "67890")):
        BlockingAgent.started, BlockingAgent.instances, BlockingAgent.interrupts = [], [], []
        BlockingAgent.release = threading.Event()
        monkeypatch.setattr(GatewayStreamConsumer, "NATIVE_MIN_SEND_INTERVAL", 0.0)
        monkeypatch.setattr(GatewayStreamConsumer, "NATIVE_FINAL_DRAIN", 0.3)
        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = lambda *a, **k: None
        monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
        fake_run_agent = types.ModuleType("run_agent")
        fake_run_agent.AIAgent = BlockingAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
        import tools.terminal_tool  # noqa: F401

        monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
        monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
        monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *a, **k: 100_000)
        display = dict(DEFAULT_DISPLAY, platforms={"telegram": {
            "interim_assistant_messages": False, "cleanup_progress": False,
        }})
        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {"display": display})
        monkeypatch.setattr(gateway_run._clover_acks, "stop_ack", lambda stock, key, rng=None: STOCK_STOP)
        self.reaps = []
        monkeypatch.setattr(
            gateway_run, "_reap_gateway_turn_processes", lambda *a, **k: self.reaps.append((a, k)),
        )
        self.adapter = TelegramAdapter(PlatformConfig(
            enabled=True, token="fake-token", extra={"rich_messages": True, "native_progress": True},
        ))
        self.api = FakeTelegramApi()
        self.adapter._bot = self.api
        self.adapter._native_stop_ready = True

        runner = StopRunner(GatewayConfig())
        runner.config.streaming = StreamingConfig(
            enabled=True, transport="draft", edit_interval=0.05, buffer_threshold=5, cursor="",
        )
        runner.adapters = {Platform.TELEGRAM: self.adapter}
        runner._running_agents = {}
        runner._running_agents_ts = {}
        runner._pending_messages = {}
        runner._pending_approvals = {}
        runner._is_user_authorized = lambda source: str(source.user_id) in allowed
        runner._set_session_env = lambda _context: None
        runner._handle_active_session_busy_message = AsyncMock(return_value=False)
        runner._session_db = MagicMock()
        runner._recover_telegram_topic_thread_id = lambda _source: None
        runner._cache_session_source = lambda _key, _source: None
        runner._get_guild_id = lambda _event: None
        runner._should_send_voice_reply = lambda *_a, **_kw: False
        runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
        runner.session_store = MagicMock()
        runner.session_store.get_or_create_session.side_effect = lambda source, **kw: SessionEntry(
            session_key=build_session_key(source),
            session_id=f"sess-{source.chat_id}",
            created_at=datetime.now(), updated_at=datetime.now(),
            platform=Platform.TELEGRAM, chat_type="dm",
        )
        runner.session_store.load_transcript.return_value = []
        runner.session_store.append_to_transcript = MagicMock()
        runner.session_store.update_session = MagicMock()
        self.runner = runner
        self.adapter.set_message_handler(runner._handle_message)

    @staticmethod
    def source(chat_id):
        return SessionSource(
            platform=Platform.TELEGRAM, chat_id=str(chat_id), chat_type="dm", user_id=str(chat_id),
        )

    def key(self, chat_id):
        return build_session_key(
            self.source(chat_id), group_sessions_per_user=True, thread_sessions_per_user=False,
            profile=self.adapter._session_key_profile(self.source(chat_id)),
        )

    async def start(self, chat_id="12345", text="hello", pending=None, pending_id="4242"):
        """Inbound message through the REAL base adapter dispatch (spawns the turn task)."""
        source = self.source(chat_id)
        if pending is not None:
            self.adapter._pending_messages[self.key(chat_id)] = MessageEvent(
                text=pending, message_type=MessageType.TEXT, source=source, message_id=pending_id,
            )
        await self.adapter.handle_message(MessageEvent(
            text=text, message_type=MessageType.TEXT, source=source, message_id="1001",
        ))
        return self.key(chat_id)

    def active(self, chat_id):
        return self.key(chat_id) in self.adapter._active_sessions

    async def idle(self, chat_id, timeout=15):
        assert await until(lambda: not self.active(chat_id), timeout=timeout), "turn chain did not finish"

    def draft_id(self, chat_id):
        frames = [f for f in self.api.rich_drafts() if f["chat_id"] == int(chat_id)]
        return frames[-1]["draft_id"] if frames else None

    def agent_of(self, chat_id):
        """The first agent built for ``chat_id``'s session (the one that blocks)."""
        return next(a for a in BlockingAgent.instances if a.session_id == f"sess-{chat_id}")

    def confirmations(self):
        return [kw for kw in self.api.methods("send_message") if STOCK_STOP in kw["text"]]

    def replies(self, chat_id, marker):
        return [
            kw for kw in self.api.methods("send_message")
            if marker in kw["text"] and kw["chat_id"] == int(chat_id)
        ]


async def until(predicate, timeout=5.0, step=0.02):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return bool(predicate())


def unmd(text):
    return re.sub(r"\\(.)", r"\1", text)


async def _finish(env, *chats):
    BlockingAgent.release.set()
    for chat in chats:
        await env.idle(chat)


@pytest.mark.asyncio
async def test_private_stop_cancels_only_the_current_run_and_preserves_the_queued_question(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path)
    await env.start("12345", pending="second question")
    await env.start("67890")
    assert await until(lambda: env.draft_id(12345) and env.draft_id(67890))
    draft_a = env.draft_id(12345)
    assert env.adapter._native_drafts            # the draft is registered for Stop

    gate = env.api.gate["send_message"] = asyncio.Event()      # confirmation/artifact network is slow
    started = time.monotonic()
    await env.adapter._on_stopped_message_generation(stop_update(12345, draft_a), None)
    assert time.monotonic() - started < 0.25            # PTB dispatch never awaits the network
    calls_at_claim = len(env.api.calls)
    # writer fenced + registry claimed synchronously, before any cancellation work completes
    assert not any(k[2] == draft_a for k in env.adapter._native_drafts)

    gate.set()
    await env.idle(12345)

    # only the current run was cancelled
    assert env.agent_of(12345).is_interrupted is True
    assert env.agent_of(67890).is_interrupted is False
    assert env.active(67890)
    # the queued question ran exactly once and produced a real reply in the right chat
    assert sum("second question" in m for m in BlockingAgent.started) == 1
    assert len(env.replies(12345, FOLLOWUP)) == 1
    assert env.replies(67890, FOLLOWUP) == []
    # the stopped turn's partial/interrupted output never became a final answer
    assert env.replies(12345, "first answer") == []
    # one confirmation, existing stop wording
    assert len(env.confirmations()) == 1
    # no further frames were written to the stopped draft after the claim
    late = [
        f for m, f in env.api.calls[calls_at_claim:]
        if m == "do_api_request:sendRichMessageDraft" and f["api_kwargs"]["draft_id"] == draft_a
    ]
    assert late == []
    # visible history survived (the draft is ephemeral): artifact precedes the confirmation
    texts = [unmd(kw["text"]) for kw in env.api.methods("send_message") if kw["chat_id"] == 12345]
    artifact = next(i for i, t in enumerate(texts) if "sony reviews" in t)
    confirm = next(i for i, t in enumerate(texts) if STOCK_STOP in t)
    assert artifact < confirm
    # no process reaping, nothing killed beyond the foreground agent
    assert env.reaps == []
    await _finish(env, 67890)


@pytest.mark.asyncio
async def test_queued_followup_is_anchored_like_a_normal_queued_turn(monkeypatch, tmp_path):
    stopped = Env(monkeypatch, tmp_path)
    await stopped.start("12345", pending="second question")
    assert await until(lambda: stopped.draft_id(12345))
    await stopped.adapter._on_stopped_message_generation(stop_update(12345, stopped.draft_id(12345)), None)
    await stopped.idle(12345)
    [stop_reply] = stopped.replies(12345, FOLLOWUP)

    control = Env(monkeypatch, tmp_path)
    BlockingAgent.release.set()                         # first turn finishes normally
    await control.start("12345", pending="second question")
    await control.idle(12345)
    [normal_reply] = control.replies(12345, FOLLOWUP)

    assert stop_reply["reply_to_message_id"] == normal_reply["reply_to_message_id"]
    assert stop_reply["message_thread_id"] == normal_reply["message_thread_id"]


@pytest.mark.asyncio
async def test_default_stop_still_reaps_and_discards_while_native_stop_does_not(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path)
    key = await env.start("12345", pending="second question")
    assert await until(lambda: env.draft_id(12345) and BlockingAgent.instances)
    await env.runner._interrupt_and_clear_session(
        key, env.source(12345), interrupt_reason="Stop requested", invalidation_reason="stop_command",
    )
    await env.idle(12345)
    await asyncio.sleep(0.2)                                     # the reaper runs on its own thread
    assert len(env.reaps) == 1                                   # existing /stop semantics unchanged
    assert sum("second question" in m for m in BlockingAgent.started) == 0   # queued text discarded


BAD_STOPS = {
    "foreign_chat": lambda draft: stop_update(99999, draft),
    "unknown_draft": lambda draft: stop_update(12345, draft + 1),
    "topic_thread": lambda draft: stop_update(12345, draft, thread=7),
    "not_private": lambda draft: stop_update(12345, draft, chat_type="supergroup"),
    "no_draft_id": lambda draft: stop_update(12345, draft, drop=("draft_id",)),
    "string_draft_id": lambda draft: stop_update(12345, str(draft)),
    "no_chat": lambda draft: stop_update(12345, draft, drop=("chat",)),
    "empty_payload": lambda draft: stop_update(12345, draft, raw={}),
    "not_a_dict": lambda draft: stop_update(12345, draft, raw="stop"),
    "no_payload": lambda draft: SimpleNamespace(stopped_message_generation=None, api_kwargs={}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(BAD_STOPS))
async def test_malformed_foreign_or_topic_stop_requests_are_noops(monkeypatch, tmp_path, case):
    env = Env(monkeypatch, tmp_path)
    await env.start("12345", pending="second question")
    assert await until(lambda: env.draft_id(12345))
    draft = env.draft_id(12345)
    registered = dict(env.adapter._native_drafts)

    await env.adapter._on_stopped_message_generation(BAD_STOPS[case](draft), None)
    await asyncio.sleep(0.2)

    assert BlockingAgent.interrupts == []
    assert env.confirmations() == []
    assert dict(env.adapter._native_drafts) == registered
    assert env.active(12345)
    await _finish(env, 12345)


@pytest.mark.asyncio
async def test_unauthorized_user_and_stale_generation_cannot_stop(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, allowed=("12345",))
    # chat 55555 reaches the agent loop but the gateway does not authorize it for Stop
    await env.start("55555")
    await env.start("12345")
    assert await until(lambda: env.draft_id(55555) and env.draft_id(12345))
    await env.adapter._on_stopped_message_generation(stop_update(55555, env.draft_id(55555)), None)
    await asyncio.sleep(0.2)
    assert BlockingAgent.interrupts == [] and env.confirmations() == []

    # stale: the run generation moved on (e.g. /new) before the Stop arrived
    env.runner._invalidate_session_run_generation(env.key(12345), reason="test")
    await env.adapter._on_stopped_message_generation(stop_update(12345, env.draft_id(12345)), None)
    await asyncio.sleep(0.2)
    assert BlockingAgent.interrupts == [] and env.confirmations() == []
    await _finish(env, 55555, 12345)


@pytest.mark.asyncio
async def test_duplicate_stop_is_a_noop(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path)
    await env.start("12345")
    assert await until(lambda: env.draft_id(12345))
    draft = env.draft_id(12345)
    await env.adapter._on_stopped_message_generation(stop_update(12345, draft), None)
    await env.adapter._on_stopped_message_generation(stop_update(12345, draft), None)
    await env.idle(12345)
    # one Stop request reached the agent (the gateway's interrupt monitor may relay the
    # same interrupt once more with no message; that is not a second Stop)
    assert BlockingAgent.interrupts.count("Stop requested") == 1
    assert len(env.confirmations()) == 1


@pytest.mark.asyncio
async def test_fresh_question_after_stop_gets_a_clean_native_draft(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path)
    await env.start("12345")
    assert await until(lambda: env.draft_id(12345))
    old = env.draft_id(12345)
    await env.adapter._on_stopped_message_generation(stop_update(12345, old), None)
    await env.idle(12345)

    BlockingAgent.release.set()
    await env.start("12345", text="hello again")
    await env.idle(12345)
    new_frames = [f for f in env.api.rich_drafts() if f["draft_id"] != old]
    assert new_frames and all(f["can_stop"] is True for f in new_frames)
    assert env.replies(12345, "first answer")           # the new question was answered normally


@pytest.mark.asyncio
async def test_native_composer_requires_a_stop_scope(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path)
    from gateway.stream_consumer import StreamConsumerConfig

    async def history(lines, reason):
        return None

    cfg = StreamConsumerConfig(transport="draft", chat_type="dm", edit_interval=0.05, buffer_threshold=5, cursor="")
    scoped = GatewayStreamConsumer(
        env.adapter, "12345", cfg, on_native_history=history,
        native_scope=SimpleNamespace(session_key="k", run_generation=1, source=None),
    )
    unscoped = GatewayStreamConsumer(env.adapter, "12345", cfg, on_native_history=history)
    assert scoped.native_activity_active is True
    assert unscoped.native_activity_active is False      # no Stop path -> never a native preview
