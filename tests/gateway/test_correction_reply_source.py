"""Mid-turn correction: open questions stay owed, the final replies to the correction.

Captured shape (session 20261004_015043_106043ce): user message 79056 asks
"Why only 1 subagent?"; while that turn is live, user message 79102 redirects
the thinking-UI work to a reference article and defers the summary idea.  The
final answer covered the article only and was reply-anchored to 79056.

Real pieces: ``TelegramAdapter`` + the base adapter session loop, the runner's
busy handler (``_handle_active_session_busy_message``), ``_handle_message_with_agent``,
``_run_agent``, the stream consumer, and the real ``AIAgent`` conversation loop
(``redirect`` → ``_apply_active_turn_redirect`` → provider projection) over the
in-memory Bot API.  Only the provider is scripted: it records the request it was
given and returns an opaque final marker, so nothing here asserts what a model
would say — the model-facing assertions prove the guidance is on the wire.
"""

import asyncio
import sys
import threading
import time
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.run as gateway_run
import run_agent as real_run_agent
from agent.agent_runtime_helpers import _INTERRUPTED_PLACEHOLDER as INTERRUPTED_PLACEHOLDER
from gateway.config import GatewayConfig, Platform, PlatformConfig, StreamingConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key
from gateway.stream_consumer import GatewayStreamConsumer
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway._telegram_fake_api import FakeTelegramApi

CHAT = "12345"
QUESTION_ID = "79056"
QUESTION = "Why only 1 subagent?"
CORRECTION_ID = "79102"
CORRECTION = (
    "Use the Ivan Magda article as the exact reference for the thinking UI instead. "
    "I'll follow up on the summary idea later."
)
COMMENTARY = "Reworking the thinking UI layout first."
SECOND_ID = "79103"
SECOND = "Also keep the native Stop button."
FINAL = "FINALAFTERCORRECTION"
SCAFFOLD = "[This response was interrupted by a user correction.]"

_TOOL_DEFS = [{
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "search",
        "parameters": {"type": "object", "properties": {}},
    },
}]


def _final_response(text):
    message = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


class LoopAgent(real_run_agent.AIAgent):
    """The real agent loop; only the provider request is scripted."""

    instances: list = []
    turns: list = []
    requests: list = []
    live: list = []                 # request numbers currently held open
    blocking = 1                    # the first N requests wait for a redirect
    release = threading.Event()     # lets a held request answer without a redirect

    def __init__(self, **kwargs):
        with (
            patch("run_agent.get_tool_definitions", return_value=_TOOL_DEFS),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            super().__init__(
                api_key="test-key-1234567890",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                session_id=kwargs.get("session_id"),
                stream_delta_callback=kwargs.get("stream_delta_callback"),
                tool_progress_callback=kwargs.get("tool_progress_callback"),
            )
        self.client = MagicMock()
        self._cached_system_prompt = "You are helpful."
        self._use_prompt_caching = False
        self.compression_enabled = False
        self.save_trajectories = False
        self._save_trajectory = lambda *a, **k: None
        self._cleanup_task_resources = lambda *a, **k: None
        LoopAgent.instances.append(self)

    def run_conversation(self, message, *args, **kwargs):
        LoopAgent.turns.append(message)
        return super().run_conversation(message, *args, **kwargs)

    def _scripted_request(self, api_kwargs, **_kw):
        LoopAgent.requests.append(api_kwargs)
        number = len(LoopAgent.requests)
        if number <= LoopAgent.blocking:
            if number == 1:
                # Interim commentary reaches the user before the correction lands.
                self._fire_stream_delta(COMMENTARY)
            LoopAgent.live.append(number)
            deadline = time.monotonic() + 10
            while (
                not self._interrupt_requested
                and not LoopAgent.release.is_set()
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            if self._interrupt_requested:
                raise InterruptedError("request cancelled by redirect")
        self._fire_stream_delta(FINAL)
        return _final_response(FINAL)

    _interruptible_api_call = _scripted_request
    _interruptible_streaming_api_call = _scripted_request


class Runner(gateway_run.GatewayRunner):
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
    def __init__(self, monkeypatch, tmp_path, *, streaming=True, blocking=1):
        LoopAgent.instances, LoopAgent.turns, LoopAgent.requests = [], [], []
        LoopAgent.live = []
        LoopAgent.blocking = blocking
        LoopAgent.release = threading.Event()
        monkeypatch.setattr(GatewayStreamConsumer, "NATIVE_MIN_SEND_INTERVAL", 0.0)
        monkeypatch.setattr(GatewayStreamConsumer, "NATIVE_FINAL_DRAIN", 0.3)
        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = lambda *a, **k: None
        monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
        monkeypatch.setitem(sys.modules, "run_agent", real_run_agent)
        monkeypatch.setattr(real_run_agent, "AIAgent", LoopAgent)
        import tools.terminal_tool  # noqa: F401

        monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
        monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
        monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *a, **k: 100_000)
        display = {
            "tool_progress": "all", "tool_preview_length": 0,
            "thinking_progress": True, "live_reasoning": False,
            "platforms": {"telegram": {
                "interim_assistant_messages": False, "cleanup_progress": False,
            }},
        }
        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {"display": display})
        monkeypatch.setattr(
            gateway_run, "_reap_gateway_turn_processes", lambda *a, **k: None,
        )
        self.adapter = TelegramAdapter(PlatformConfig(
            enabled=True, token="fake-token", extra={"rich_messages": True, "native_progress": True},
        ))
        self.api = FakeTelegramApi()
        self.adapter._bot = self.api
        self.adapter._native_stop_ready = True

        runner = Runner(GatewayConfig())
        runner.config.streaming = StreamingConfig(
            enabled=streaming, transport="draft", edit_interval=0.05, buffer_threshold=5, cursor="",
        )
        runner.adapters = {Platform.TELEGRAM: self.adapter}
        runner._running_agents = {}
        runner._running_agents_ts = {}
        runner._pending_messages = {}
        runner._pending_approvals = {}
        runner._busy_text_mode = "interrupt"
        runner._is_user_authorized = lambda source: True
        runner._set_session_env = lambda _context: None
        runner._session_db = None
        runner._recover_telegram_topic_thread_id = lambda _source: None
        runner._cache_session_source = lambda _key, _source: None
        runner._get_guild_id = lambda _event: None
        runner._should_send_voice_reply = lambda *_a, **_kw: False
        runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
        runner.session_store = MagicMock()
        runner.session_store._entries = {}
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
        # The REAL busy handler decides redirect / interrupt / queue.
        self.adapter.set_busy_session_handler(runner._handle_active_session_busy_message)

    @staticmethod
    def source():
        return SessionSource(
            platform=Platform.TELEGRAM, chat_id=CHAT, chat_type="dm", user_id=CHAT,
        )

    @property
    def key(self):
        return build_session_key(
            self.source(), group_sessions_per_user=True, thread_sessions_per_user=False,
            profile=self.adapter._session_key_profile(self.source()),
        )

    async def inbound(self, text, message_id, *, message_type=MessageType.TEXT, media=()):
        """Inbound update through the REAL base adapter dispatch."""
        await self.adapter.handle_message(MessageEvent(
            text=text, message_type=message_type, source=self.source(), message_id=message_id,
            media_urls=list(media), media_types=["image/png"] * len(media),
        ))

    async def idle(self, timeout=20):
        assert await until(lambda: self.key not in self.adapter._active_sessions, timeout=timeout), (
            "turn chain did not finish"
        )

    def finals(self):
        """Bot API ``sendMessage`` calls that delivered the final answer bubble.

        Progress artifacts (``💭 …`` thought lines) may mirror streamed text;
        only the bubble whose whole text is the final marker counts.
        """
        return [
            kw for kw in self.api.methods("send_message")
            if (kw.get("text") or "").replace("\\", "").strip() == FINAL
            and kw["chat_id"] == int(CHAT)
        ]


async def until(predicate, timeout=5.0, step=0.02):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return bool(predicate())


async def held(env, number):
    """Wait until provider request ``number`` is live and the runner tracks its agent."""

    def _live():
        state = env.runner._peek_session_state(env.key)
        return number in LoopAgent.live and state is not None and isinstance(state.turn.agent, LoopAgent)

    assert await until(_live, timeout=10), f"request {number} never became live"


async def captured_shape(env):
    """Question 79056, commentary, correction 79102 mid-request, retry, final."""
    await env.inbound(QUESTION, QUESTION_ID)
    await held(env, 1)
    await env.inbound(CORRECTION, CORRECTION_ID)
    await env.idle()

    # One turn, re-entered once: the correction was neither dropped nor replayed.
    assert LoopAgent.turns == [QUESTION]
    assert len(LoopAgent.requests) == 2
    assert env.adapter._pending_messages.get(env.key) is None
    return [r["messages"] for r in LoopAgent.requests]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "unstreamed"])
async def test_retry_after_correction_still_owes_the_open_question(monkeypatch, tmp_path, streaming):
    env = Env(monkeypatch, tmp_path, streaming=streaming)
    first, retry = await captured_shape(env)

    # Prompt cache: everything the provider already saw is replayed byte-identically.
    assert retry[: len(first)] == first
    assert [m["role"] for m in retry[len(first):]] == ["assistant", "user"]
    # Streamed commentary is the visible checkpoint; an unstreamed turn showed
    # nothing, so its checkpoint is the neutral placeholder — never the scaffold.
    checkpoint = retry[len(first)]["content"]
    assert checkpoint == (COMMENTARY if streaming else INTERRUPTED_PLACEHOLDER)
    assert SCAFFOLD not in checkpoint
    boundary = retry[-1]["content"]
    assert boundary.endswith(CORRECTION)
    guidance = boundary[: -len(CORRECTION)]
    assert SCAFFOLD in guidance
    # B5 (guidance only — not model behaviour): the still-unanswered question is
    # carried as an open obligation at the correction boundary, after the
    # interrupted commentary, so that commentary cannot pass for an answer.
    assert QUESTION in guidance
    assert guidance.index(SCAFFOLD) < guidance.index(QUESTION)
    if streaming:
        assert guidance.index(COMMENTARY) < guidance.index(QUESTION)
    else:
        # Nothing was on screen: that is not itself an open question.
        assert INTERRUPTED_PLACEHOLDER not in guidance
        assert COMMENTARY not in guidance


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "unstreamed"])
async def test_final_after_correction_replies_to_the_correction(monkeypatch, tmp_path, streaming):
    env = Env(monkeypatch, tmp_path, streaming=streaming)
    await captured_shape(env)

    # B6: read off the Bot API call — the final is delivered exactly once, as a
    # reply to the correction (79102), not to the older question (79056).
    finals = env.finals()
    assert len(finals) == 1
    assert finals[0]["chat_id"] == int(CHAT)
    assert finals[0]["reply_to_message_id"] == int(CORRECTION_ID)
    assert env.runner._correction_reply_source(env.key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "unstreamed"])
async def test_uncorrected_turn_still_replies_to_its_own_message(monkeypatch, tmp_path, streaming):
    env = Env(monkeypatch, tmp_path, streaming=streaming, blocking=0)
    await env.inbound(QUESTION, QUESTION_ID)
    await env.idle()

    assert len(LoopAgent.requests) == 1
    finals = env.finals()
    assert len(finals) == 1
    assert finals[0]["reply_to_message_id"] == int(QUESTION_ID)


@pytest.mark.asyncio
async def test_latest_of_several_corrections_owns_the_final_reply(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, blocking=2)
    await env.inbound(QUESTION, QUESTION_ID)
    await held(env, 1)
    await env.inbound(CORRECTION, CORRECTION_ID)
    await held(env, 2)
    await env.inbound(SECOND, SECOND_ID)
    await env.idle()

    assert LoopAgent.turns == [QUESTION]
    assert len(LoopAgent.requests) == 3
    second, third = (r["messages"] for r in LoopAgent.requests[1:])
    # The first correction's row is history now: replayed byte-identically.
    assert third[: len(second)] == second
    boundary = third[-1]["content"]
    assert boundary.endswith(SECOND)
    guidance = boundary[: -len(SECOND)]
    # Both earlier asks are still open; the first boundary's guidance is not nested.
    assert QUESTION in guidance and CORRECTION in guidance
    assert guidance.count(SCAFFOLD) == 1

    finals = env.finals()
    assert len(finals) == 1
    assert finals[0]["reply_to_message_id"] == int(SECOND_ID)


@pytest.mark.asyncio
async def test_next_turn_does_not_inherit_a_spent_correction_reply_source(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path)
    await env.inbound(QUESTION, QUESTION_ID)
    await held(env, 1)
    await env.inbound(CORRECTION, CORRECTION_ID)
    await env.idle()
    assert env.runner._correction_reply_source(env.key) is None

    await env.inbound("And the summary idea?", "79120")
    await env.idle()

    assert LoopAgent.turns == [QUESTION, "And the summary idea?"]
    replies = [kw["reply_to_message_id"] for kw in env.finals()]
    assert replies == [int(CORRECTION_ID), 79120]


@pytest.mark.asyncio
async def test_screenshot_followup_is_its_own_turn_and_never_claims_the_live_reply(
    monkeypatch, tmp_path,
):
    env = Env(monkeypatch, tmp_path)
    await env.inbound(QUESTION, QUESTION_ID)
    await held(env, 1)
    agent = LoopAgent.instances[0]
    redirects = []
    real_redirect = agent.redirect
    agent.redirect = lambda text: redirects.append(text) or real_redirect(text)

    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    await env.inbound("see this", "79101", message_type=MessageType.PHOTO, media=[str(shot)])

    # Media never rides the text-only redirect, and never takes over the live
    # run's reply: it waits, intact and with its own id, for its own turn.
    assert redirects == []
    assert env.runner._correction_reply_source(env.key) is None
    queued = env.adapter._pending_messages.get(env.key)
    if queued is not None:
        assert queued.message_id == "79101"
        assert queued.media_urls == [str(shot)]

    # An empty text follow-up never claims the live turn's reply either.
    env.runner._note_correction_reply_source(env.key, MessageEvent(
        text="   ", message_type=MessageType.TEXT, source=env.source(), message_id="79099",
    ))
    assert env.runner._correction_reply_source(env.key) is None

    LoopAgent.release.set()
    await env.idle()
    assert sum("see this" in turn for turn in LoopAgent.turns) == 1
    replies = [kw["reply_to_message_id"] for kw in env.finals()]
    assert replies and replies[-1] == 79101
    assert set(replies) <= {int(QUESTION_ID), 79101}
