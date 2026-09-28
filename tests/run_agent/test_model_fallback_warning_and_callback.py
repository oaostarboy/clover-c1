"""Every fallback activation must be visible (#93412):

  1. A WARNING-level log line naming from -> to and the reason (previously
     INFO, invisible at agent.log's default level).
  2. A structured ``agent.model_fallback_callback`` hook fires with
     from/to model+provider and the reason -- the wire the -z
     --activity-events writer and the subagent/job card key off of.
  3. When no callback is wired (the common case -- interactive CLI /
     gateway sessions reuse the existing status_callback/_emit_status
     notice, unaffected by this change), activation must not raise.
"""

import logging
from unittest.mock import MagicMock, patch

from agent.error_classifier import FailoverReason
from run_agent import AIAgent


def _make_agent(fallback_model):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="gemini-3.8-pro",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _mock_client():
    mock = MagicMock()
    mock.base_url = "https://api.anthropic.com"
    mock.api_key = "fb-key"
    return mock


class TestFallbackWarningLog:
    def test_activation_logs_a_warning_naming_from_and_to(self, caplog):
        agent = _make_agent([{"provider": "anthropic", "model": "claude-opus-5-5"}])
        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "claude-opus-5-5"),
        ):
            with caplog.at_level(logging.WARNING, logger="agent.chat_completion_helpers"):
                assert agent._try_activate_fallback(
                    reason=FailoverReason.model_not_found
                ) is True
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "gemini-3.8-pro" in r.getMessage() and "claude-opus-5-5" in r.getMessage()
            for r in warnings
        ), [r.getMessage() for r in warnings]


class TestModelFallbackCallback:
    def test_callback_invoked_with_from_to_and_reason(self):
        agent = _make_agent([{"provider": "anthropic", "model": "claude-opus-5-5"}])
        calls = []
        agent.model_fallback_callback = lambda **kw: calls.append(kw)
        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "claude-opus-5-5"),
        ):
            assert agent._try_activate_fallback(
                reason=FailoverReason.model_not_found
            ) is True
        assert len(calls) == 1
        assert calls[0]["from_model"] == "gemini-3.8-pro"
        assert calls[0]["to_model"] == "claude-opus-5-5"
        assert calls[0]["to_provider"] == "anthropic"
        assert calls[0]["reason"] == "model_not_found"

    def test_no_callback_wired_is_a_silent_no_op(self):
        agent = _make_agent([{"provider": "anthropic", "model": "claude-opus-5-5"}])
        assert getattr(agent, "model_fallback_callback", None) is None
        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "claude-opus-5-5"),
        ):
            assert agent._try_activate_fallback(
                reason=FailoverReason.model_not_found
            ) is True

    def test_callback_exception_does_not_break_the_switch(self):
        agent = _make_agent([{"provider": "anthropic", "model": "claude-opus-5-5"}])

        def _boom(**kw):
            raise RuntimeError("consumer blew up")

        agent.model_fallback_callback = _boom
        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "claude-opus-5-5"),
        ):
            assert agent._try_activate_fallback(
                reason=FailoverReason.model_not_found
            ) is True
        assert agent.model == "claude-opus-5-5"
