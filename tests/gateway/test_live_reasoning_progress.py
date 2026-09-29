"""Tests for live reasoning progress (default-on gateway feature).

Bug: with three agents on the same C1 main, only Alfred (grok-4.7) showed
live "thoughts" — Suni (openai-codex gpt-6-sol) and Oracle (gemini-3.8-flash-high)
only showed tool lines. The gateway never wired ``agent.reasoning_callback``
for chat, so provider REASONING summaries (Codex/Responses reasoning
summaries, Gemini thoughts, Anthropic thinking) never reached the live
progress stream even though ``interim_assistant_messages`` (the model's
visible assistant TEXT between tool calls) did.

Covers:
  - ``gateway.stream_consumer.ReasoningProgressRelay`` — the pure buffering/
    formatting/dedup/rate-limit logic.
  - The gateway wiring in ``gateway/run.py`` that assigns
    ``agent.reasoning_callback`` and feeds ``ctx.progress_queue`` +
    ``ctx._summary_thoughts``, exercised end-to-end via
    ``TurnRunner.run_sync()`` (same harness as test_turn_context.py).
"""

from __future__ import annotations

import queue as queue_mod
from types import SimpleNamespace
from unittest.mock import MagicMock

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.stream_consumer import ReasoningProgressRelay
from gateway.turn_context import TurnContext


# ---------------------------------------------------------------------------
# ReasoningProgressRelay — pure buffering/formatting unit tests
# ---------------------------------------------------------------------------


class TestReasoningProgressRelayFeed:
    def test_single_call_with_embedded_newline_emits_first_line_only(self):
        relay = ReasoningProgressRelay()
        assert relay.feed("**Fetching remote updates**\nchecking origin") == (
            "Fetching remote updates"
        )

    def test_token_streamed_deltas_produce_one_line(self):
        relay = ReasoningProgressRelay()
        chunks = ["**Fetching", " remote", " updates**\n", "checking", " origin"]
        results = [relay.feed(chunk) for chunk in chunks]
        non_none = [r for r in results if r is not None]
        assert non_none == ["Fetching remote updates"]

    def test_no_newline_never_emits_via_feed(self):
        relay = ReasoningProgressRelay()
        assert relay.feed("still thinking, no boundary yet") is None

    def test_empty_lines_are_skipped_not_treated_as_the_headline(self):
        relay = ReasoningProgressRelay()
        assert relay.feed("\n\n**Actual heading**\nbody\n") == "Actual heading"

    def test_blank_text_is_a_noop(self):
        relay = ReasoningProgressRelay()
        assert relay.feed("") is None
        assert relay.feed(None) is None

    def test_line_is_capped_at_120_chars(self):
        relay = ReasoningProgressRelay()
        long_line = "x" * 200
        result = relay.feed(f"{long_line}\n")
        assert result is not None
        assert len(result) <= 120
        assert result.endswith("…")

    def test_second_item_after_a_flushed_item_gets_its_own_line(self):
        now = [0.0]
        relay = ReasoningProgressRelay(now_fn=lambda: now[0])
        assert relay.feed("**First item**\nbody one\n") == "First item"
        now[0] = 10.0  # well past the rate-limit window
        assert relay.feed("**Second item**\nbody two\n") == "Second item"


class TestReasoningProgressRelayDedupeAndRateLimit:
    def test_duplicate_line_is_suppressed(self):
        now = [0.0]
        relay = ReasoningProgressRelay(now_fn=lambda: now[0])
        assert relay.feed("**Same heading**\n") == "Same heading"
        now[0] = 10.0  # outside the rate-limit window, but identical text
        assert relay.feed("**Same heading**\n") is None

    def test_rate_limit_suppresses_rapid_distinct_lines(self):
        now = [0.0]
        relay = ReasoningProgressRelay(now_fn=lambda: now[0])
        assert relay.feed("**First**\n") == "First"
        now[0] = 0.5  # inside the 2s window
        assert relay.feed("**Second**\n") is None

    def test_distinct_line_after_window_is_emitted(self):
        now = [0.0]
        relay = ReasoningProgressRelay(now_fn=lambda: now[0])
        assert relay.feed("**First**\n") == "First"
        now[0] = 2.5  # past the 2s window
        assert relay.feed("**Second**\n") == "Second"

    def test_closed_heading_without_newline_emits_immediately(self):
        # Codex/Responses reasoning summaries arrive as whole "**Heading**"
        # chunks with no trailing newline (captured live from gpt-6-sol).
        relay = ReasoningProgressRelay()
        assert relay.feed("**Heading only, no newline**") == "Heading only, no newline"
        assert relay.flush() is None

    def test_codex_style_repeated_headings_become_separate_lines(self):
        clock = [0.0]
        relay = ReasoningProgressRelay(now_fn=lambda: clock[0])
        out = []
        for chunk in ["**Inspecting the working directory**", "**Inspecting the working directory**",
                      "**Checking workspace contents**", "**Checking workspace contents**"]:
            clock[0] += 3.0
            line = relay.feed(chunk)
            if line:
                out.append(line)
        assert out == ["Inspecting the working directory", "Checking workspace contents"]

    def test_flush_emits_pending_plain_text_with_no_trailing_newline(self):
        relay = ReasoningProgressRelay()
        assert relay.feed("Plain thought, no newline") is None
        assert relay.flush() == "Plain thought, no newline"

    def test_flush_on_empty_buffer_is_noop(self):
        relay = ReasoningProgressRelay()
        assert relay.flush() is None


# ---------------------------------------------------------------------------
# Gateway wiring — end-to-end through TurnRunner.run_sync()
# ---------------------------------------------------------------------------


def _make_mocked_gateway_runner() -> MagicMock:
    """Minimal GatewayRunner mock that lets run_sync() reach agent construction
    and agent.run_conversation() without touching real gateway state (same
    fixture shape as tests/gateway/test_turn_context.py)."""
    gateway_runner = MagicMock()
    gateway_runner.config = SimpleNamespace(streaming=None)
    gateway_runner._provider_routing = {}
    gateway_runner._agent_cache_lock = None
    gateway_runner._agent_cache = {}
    gateway_runner._session_db = None
    gateway_runner._prefill_messages = None
    gateway_runner._pending_model_notes = {}
    gateway_runner._pending_skills_reload_notes = {}
    gateway_runner.session_store._entries = {}
    gateway_runner._get_system_prompt_for_channel.return_value = None
    gateway_runner._resolve_session_agent_runtime.return_value = ("test-model", {})
    gateway_runner._resolve_session_reasoning_config.return_value = None
    gateway_runner._resolve_session_service_tier.return_value = None
    gateway_runner._resolve_turn_agent_config.return_value = {
        "model": "test-model",
        "runtime": {},
    }
    gateway_runner._agent_config_signature.return_value = ("test-signature",)
    gateway_runner._extract_cache_busting_config.return_value = {}
    gateway_runner._refresh_fallback_model.return_value = None
    gateway_runner._consume_pending_native_image_paths.return_value = []
    gateway_runner._consume_pending_turn_sidecar_notes.return_value = []
    gateway_runner._is_telegram_topic_lane.return_value = False
    gateway_runner._is_discord_auto_thread_lane.return_value = False
    gateway_runner._is_relay_discord_channel_lane.return_value = False
    return gateway_runner


class _ReasoningFiringAgent:
    """Fake agent that fires the gateway-assigned reasoning_callback (if any)
    from inside run_conversation(), mirroring where the real provider path
    (agent/chat_completion_helpers.py) fires it during the actual model call —
    not from __init__, before the gateway has wired the callback."""

    _delegate_depth = 0

    def __init__(self, **kwargs):
        self.model = kwargs["model"]
        self.session_id = kwargs["session_id"]
        self.tools = []
        self.context_compressor = SimpleNamespace(
            last_prompt_tokens=0, context_length=200_000,
        )
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.reasoning_callback = None

    def run_conversation(self, _message, **_kwargs):
        if self.reasoning_callback is not None:
            self.reasoning_callback("**Fetching remote updates**\nchecking origin")
        return {
            "final_response": "done",
            "failed": False,
            "messages": [],
        }


class _DelegatedChildAgent(_ReasoningFiringAgent):
    """Same as above, but simulating a delegation subagent."""

    _delegate_depth = 1


def _base_turn_ctx(**overrides) -> TurnContext:
    source = SessionSource(platform=Platform.LOCAL, chat_id="test-chat", user_id="test-user")
    kwargs = dict(
        source=source,
        message="continue",
        history=[],
        session_id="test-session",
        session_key="test-session-key",
        user_config={},
        resolve_display_setting=lambda *_args: False,
        _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
        progress_queue=queue_mod.Queue(),
        needs_progress_queue=True,
        _live_reasoning_enabled=True,
        _summary_thoughts=0,
    )
    kwargs.update(overrides)
    return TurnContext(**kwargs)


class TestLiveReasoningGatewayWiring:
    def test_reasoning_callback_pushes_compact_line_and_counts_thought(self):
        ctx = _base_turn_ctx(AIAgent=_ReasoningFiringAgent)

        from gateway.run import TurnRunner

        result = TurnRunner(_make_mocked_gateway_runner(), ctx).run_sync()

        assert result["final_response"] == "done"
        drained = []
        while True:
            try:
                drained.append(ctx.progress_queue.get_nowait())
            except queue_mod.Empty:
                break
        assert drained == ["🧠 Fetching remote updates"]
        assert ctx._summary_thoughts == 1

    def test_live_reasoning_disabled_yields_nothing(self):
        ctx = _base_turn_ctx(AIAgent=_ReasoningFiringAgent, _live_reasoning_enabled=False)

        from gateway.run import TurnRunner

        TurnRunner(_make_mocked_gateway_runner(), ctx).run_sync()

        assert ctx.progress_queue.empty()
        assert ctx._summary_thoughts == 0

    def test_delegated_child_yields_nothing(self):
        ctx = _base_turn_ctx(AIAgent=_DelegatedChildAgent, _live_reasoning_enabled=True)

        from gateway.run import TurnRunner

        TurnRunner(_make_mocked_gateway_runner(), ctx).run_sync()

        assert ctx.progress_queue.empty()
        assert ctx._summary_thoughts == 0
