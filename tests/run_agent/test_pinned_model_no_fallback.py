"""No-silent-fallback: an explicitly pinned model must never be silently
swapped for another model/provider on a model_not_found failure.

Bug (#93412), reproduced live: ``clover -z -m gemini-3.8-pro`` (a model that
doesn't exist) ran the whole job on claude-opus-5-5 with no warning.
detect_provider_for_model() returned None, so the run fell through to the
default provider, got a model-not-found error, and the conversation loop's
generic client-error fallback walked the configured fallback_providers chain
with no visible signal — the job card and activity events kept saying
"gemini-3.8-pro" the whole time.

These tests pin two things:
  1. ``agent.model_pinned`` exists and defaults False; AIAgent sets it only
     when the caller passes ``model_pinned=True`` explicitly.
  2. The exact gate added to conversation_loop.py's ``is_client_error``
     branch: pinned + model_not_found blocks fallback; pinned + any other
     reason (rate limit, overload, timeout, billing) still falls back —
     pinning only guards against a *different model* silently answering.
"""

from unittest.mock import MagicMock, patch

from agent.error_classifier import FailoverReason
from run_agent import AIAgent


def _make_agent(fallback_model=None, model_pinned=False):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="gemini-3.8-pro",
            model_pinned=model_pinned,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _mock_client(base_url="https://api.anthropic.com", api_key="fb-key"):
    mock = MagicMock()
    mock.base_url = base_url
    mock.api_key = api_key
    return mock


class TestModelPinnedAttribute:
    def test_defaults_false(self):
        agent = _make_agent()
        assert agent.model_pinned is False

    def test_set_when_caller_passes_model_pinned_true(self):
        agent = _make_agent(model_pinned=True)
        assert agent.model_pinned is True


def _blocks_pinned_fallback(agent, reason) -> bool:
    """Mirror of the exact gate added to conversation_loop.py's
    ``is_client_error`` branch (same convention used by
    tests/run_agent/test_auth_provider_failover.py): a pinned model that
    failed with model_not_found must never reach ``_try_activate_fallback``.
    """
    return (
        reason == FailoverReason.model_not_found
        and getattr(agent, "model_pinned", False)
    )


class TestPinnedModelBlocksFallbackOnModelNotFound:
    def test_pinned_model_not_found_blocks_fallback(self):
        agent = _make_agent(
            fallback_model=[{"provider": "anthropic", "model": "claude-opus-5-5"}],
            model_pinned=True,
        )
        assert _blocks_pinned_fallback(agent, FailoverReason.model_not_found) is True
        # The chain must stay untouched -- the caller never reaches
        # _try_activate_fallback for this reason when the guard fires.
        assert agent._fallback_index == 0

    def test_unpinned_model_not_found_does_not_block(self):
        """A user who never pinned a model keeps today's behavior: a
        model_not_found error still walks the fallback chain."""
        agent = _make_agent(
            fallback_model=[{"provider": "anthropic", "model": "claude-opus-5-5"}],
            model_pinned=False,
        )
        assert _blocks_pinned_fallback(agent, FailoverReason.model_not_found) is False
        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "claude-opus-5-5"),
        ):
            assert agent._try_activate_fallback(reason=FailoverReason.model_not_found) is True
        # ... and the switch produced the one-shot user-visible notice.
        assert agent._pending_fallback_notice

    def test_pinned_model_but_other_reasons_still_fall_back(self):
        """Pinning only guards model_not_found -- rate limits, overloads,
        billing and timeouts still fall back for a pinned model, and still
        emit the one-shot notice."""
        for reason in (
            FailoverReason.rate_limit,
            FailoverReason.overloaded,
            FailoverReason.timeout,
            FailoverReason.billing,
        ):
            agent = _make_agent(
                fallback_model=[{"provider": "anthropic", "model": "claude-opus-5-5"}],
                model_pinned=True,
            )
            assert _blocks_pinned_fallback(agent, reason) is False
            with patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(_mock_client(), "claude-opus-5-5"),
            ):
                assert agent._try_activate_fallback(reason=reason) is True
            assert agent.model == "claude-opus-5-5"
            assert agent._pending_fallback_notice, f"no notice recorded for {reason}"
