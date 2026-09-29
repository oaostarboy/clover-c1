"""Tests for gateway /reasoning command and hot reload behavior."""

import asyncio
import inspect
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource


def _make_event(text="/reasoning", platform=Platform.TELEGRAM, user_id="12345", chat_id="67890"):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    """Create a bare GatewayRunner without calling __init__."""
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._session_reasoning_overrides = {}
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    runner._session_db = None
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    return runner


class _CapturingAgent:
    """Fake agent that records init kwargs for assertions."""

    last_init = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(self, user_message: str, conversation_history=None, task_id=None):
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
        }


class TestReasoningCommand:


    def test_parse_reasoning_command_args_accepts_ascii_and_smart_global_flags(self):
        assert gateway_run.GatewayRunner._parse_reasoning_command_args("high --global") == ("high", True)
        assert gateway_run.GatewayRunner._parse_reasoning_command_args("—global xhigh") == ("xhigh", True)

    @pytest.mark.asyncio
    async def test_reasoning_command_reloads_current_state_from_config(self, tmp_path, monkeypatch):
        clover_home = tmp_path / "clover"
        clover_home.mkdir()
        config_path = clover_home / "config.yaml"
        config_path.write_text(
            "agent:\n  reasoning_effort: none\ndisplay:\n  show_reasoning: true\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(gateway_run, "_clover_home", clover_home)

        runner = _make_runner()
        runner._reasoning_config = {"enabled": True, "effort": "xhigh"}
        runner._show_reasoning = False

        result = await runner._handle_reasoning_command(_make_event("/reasoning"))

        assert "**Effort:** `none (disabled)`" in result
        assert "**Display:** on ✓" in result
        assert runner._reasoning_config == {"enabled": False}
        assert runner._show_reasoning is True


    @pytest.mark.asyncio
    @pytest.mark.parametrize("effort", ["max", "ultra"])
    async def test_handle_reasoning_command_accepts_extended_efforts(
        self, tmp_path, monkeypatch, effort
    ):
        clover_home = tmp_path / "clover"
        clover_home.mkdir()
        (clover_home / "config.yaml").write_text(
            "agent:\n  reasoning_effort: medium\n", encoding="utf-8"
        )
        monkeypatch.setattr(gateway_run, "_clover_home", clover_home)

        runner = _make_runner()
        event = _make_event(f"/reasoning {effort}")
        session_key = runner._session_key_for_source(event.source)

        await runner._handle_reasoning_command(event)

        assert runner._session_reasoning_overrides[session_key] == {
            "enabled": True,
            "effort": effort,
        }


    def test_resolve_session_reasoning_prefers_session_override(self, tmp_path, monkeypatch):
        clover_home = tmp_path / "clover"
        clover_home.mkdir()
        (clover_home / "config.yaml").write_text("agent:\n  reasoning_effort: low\n", encoding="utf-8")

        monkeypatch.setattr(gateway_run, "_clover_home", clover_home)

        runner = _make_runner()
        source = _make_event("/reasoning").source
        session_key = runner._session_key_for_source(source)
        runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "xhigh"}

        assert runner._resolve_session_reasoning_config(source=source) == {"enabled": True, "effort": "xhigh"}


    def test_run_agent_includes_enabled_mcp_servers_in_gateway_toolsets(self, tmp_path, monkeypatch):
        clover_home = tmp_path / "clover"
        clover_home.mkdir()
        (clover_home / "config.yaml").write_text(
            "platform_toolsets:\n"
            "  cli: [web, memory]\n"
            "mcp_servers:\n"
            "  exa:\n"
            "    url: https://mcp.exa.ai/mcp\n"
            "  web-search-prime:\n"
            "    url: https://api.z.ai/api/mcp/web_search_prime/mcp\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(gateway_run, "_clover_home", clover_home)
        monkeypatch.setattr(gateway_run, "_env_path", clover_home / ".env")
        monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            gateway_run,
            "_resolve_runtime_agent_kwargs",
            lambda: {
                "provider": "openrouter",
                "api_mode": "chat_completions",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "test-key",
            },
        )
        fake_run_agent = types.ModuleType("run_agent")
        fake_run_agent.AIAgent = _CapturingAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

        _CapturingAgent.last_init = None
        runner = _make_runner()

        source = SessionSource(
            platform=Platform.LOCAL,
            chat_id="cli",
            chat_name="CLI",
            chat_type="dm",
            user_id="user-1",
        )

        result = asyncio.run(
            runner._run_agent(
                message="ping",
                context_prompt="",
                history=[],
                source=source,
                session_id="session-1",
                session_key="agent:main:local:dm",
            )
        )

        assert result["final_response"] == "ok"
        assert _CapturingAgent.last_init is not None
        enabled_toolsets = set(_CapturingAgent.last_init["enabled_toolsets"])
        assert "web" in enabled_toolsets
        assert "memory" in enabled_toolsets
        assert "exa" in enabled_toolsets
        assert "web-search-prime" in enabled_toolsets


class TestLoadShowReasoningCoercion:
    """Regression: display.show_reasoning must be coerced, not bool()'d."""

    def _load_with_config(self, tmp_path, monkeypatch, yaml_body: str) -> bool:
        clover_home = tmp_path / "clover"
        clover_home.mkdir()
        (clover_home / "config.yaml").write_text(yaml_body, encoding="utf-8")
        monkeypatch.setattr(gateway_run, "_clover_home", clover_home)
        return gateway_run.GatewayRunner._load_show_reasoning()

    def test_quoted_false_is_false(self, tmp_path, monkeypatch):
        assert self._load_with_config(
            tmp_path, monkeypatch,
            'display:\n  show_reasoning: "false"\n',
        ) is False


    def test_bare_true_is_true(self, tmp_path, monkeypatch):
        assert self._load_with_config(
            tmp_path, monkeypatch,
            'display:\n  show_reasoning: true\n',
        ) is True








def _reasoning_prepend_source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-2001",
        chat_type="private",
        user_id="55555",
    )


def _reasoning_prepend_event():
    return MessageEvent(text="hi", source=_reasoning_prepend_source(), message_id="msg-1")


def _reasoning_prepend_runner(monkeypatch, tmp_path):
    """Runner harness for exercising the final-message reasoning prepend at
    the end of ``_handle_message_with_agent`` (gateway/run.py ~L21595)."""
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:private:-2001:55555",
        session_id="sess-reasoning",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="private",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()

    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    return runner


def _reasoning_agent_result(*, last_reasoning, reasoning_relayed_live):
    return {
        "final_response": "The answer is 42.",
        "last_reasoning": last_reasoning,
        "reasoning_relayed_live": reasoning_relayed_live,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "The answer is 42."},
        ],
        "tools": [],
        "history_offset": 0,
        "last_prompt_tokens": 0,
        "api_calls": 1,
        "failed": False,
    }


class TestReasoningPrependDeduplication:
    """Regression for the live-Telegram duplicate: when the turn already
    surfaced the model's thinking (live thinking_progress relay, or a
    collapsed turn-summary card counting thoughts), the final reply must
    NOT prepend the same reasoning again. These tests reproduce the bug by
    driving the real ``_handle_message_with_agent`` prepend site with a
    mocked ``_run_agent`` result — they FAIL on a0255ac1."""

    @pytest.mark.asyncio
    async def test_relay_active_this_turn_suppresses_prepend(self, monkeypatch, tmp_path):
        """thinking relay ON for the turn -> no duplicate 'Reasoning:' block."""
        runner = _reasoning_prepend_runner(monkeypatch, tmp_path)
        runner._run_agent = AsyncMock(return_value=_reasoning_agent_result(
            last_reasoning="**Fetching remote updates**",
            reasoning_relayed_live=True,
        ))

        response = await runner._handle_message_with_agent(
            _reasoning_prepend_event(), _reasoning_prepend_source(),
            "agent:main:telegram:private:-2001:55555", 1,
        )

        assert "💭 **Reasoning:**" not in response
        assert response == "The answer is 42."

    @pytest.mark.asyncio
    async def test_relay_inactive_still_prepends_cleanly(self, monkeypatch, tmp_path):
        """thinking relay OFF this turn, show_reasoning on -> still prepends,
        but the code-fence style must not leak literal ** markers."""
        runner = _reasoning_prepend_runner(monkeypatch, tmp_path)
        # show_reasoning is opt-in again (b20781d8 reverted), so opt in.
        runner._show_reasoning = True
        monkeypatch.setattr(
            gateway_run, "_resolve_gateway_display_bool",
            lambda cfg, pk, setting, default=False, **kw: True if setting == "show_reasoning" else default,
        )
        runner._run_agent = AsyncMock(return_value=_reasoning_agent_result(
            last_reasoning="**Fetching remote updates**\nchecking origin/main",
            reasoning_relayed_live=False,
        ))

        response = await runner._handle_message_with_agent(
            _reasoning_prepend_event(), _reasoning_prepend_source(),
            "agent:main:telegram:private:-2001:55555", 1,
        )

        assert "💭 **Reasoning:**" in response
        assert response.endswith("The answer is 42.")
        # The heading line must be unwrapped, not left as literal asterisks
        # inside the fenced code block.
        fence_body = response.split("```\n", 1)[1].split("\n```", 1)[0]
        assert "**" not in fence_body
        assert "Fetching remote updates" in fence_body

    @pytest.mark.asyncio
    async def test_explicit_show_reasoning_false_hides_regardless_of_relay(
        self, monkeypatch, tmp_path
    ):
        """An explicit display.show_reasoning: false always wins, whether or
        not the relay was active this turn."""
        clover_home = tmp_path / "clover"
        clover_home.mkdir()
        (clover_home / "config.yaml").write_text(
            "display:\n  show_reasoning: false\n", encoding="utf-8",
        )

        runner = _reasoning_prepend_runner(monkeypatch, clover_home)
        runner._run_agent = AsyncMock(return_value=_reasoning_agent_result(
            last_reasoning="**Fetching remote updates**",
            reasoning_relayed_live=False,
        ))

        response = await runner._handle_message_with_agent(
            _reasoning_prepend_event(), _reasoning_prepend_source(),
            "agent:main:telegram:private:-2001:55555", 1,
        )

        assert "💭" not in response
        assert response == "The answer is 42."

