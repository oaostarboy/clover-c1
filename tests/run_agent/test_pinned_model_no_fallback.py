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

import logging

from unittest.mock import MagicMock, patch

from agent.error_classifier import FailoverReason
from run_agent import AIAgent


def _make_agent(fallback_model=None, model_pinned=False, provider=None):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            provider=provider,
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


class TestSubstituteUnknownModel:
    """``agent._try_substitute_unknown_model`` -- the substitute a pinned
    model_not_found now tries BEFORE giving up (#93412 follow-up).

    Deliberately separate from ``_try_activate_fallback`` / the configured
    ``fallback_providers`` chain: every case here asserts the chain state
    (``_fallback_index`` / ``_fallback_activated``) is never touched, since
    that chain is for outage recovery, not a typo'd model name.
    """

    def test_case_a_same_provider_closest_match_switches_in_place(self):
        agent = _make_agent(model_pinned=True, provider="gemini")
        agent.model = "gemini-3.8-pro"
        agent.provider = "gemini"
        calls = []
        agent.model_fallback_callback = lambda **kw: calls.append(kw)
        with (
            patch("clover_cli.models._PROVIDER_MODELS", {"gemini": ["gemini-3.8-flash-high"]}),
            patch("clover_cli.config.load_config", return_value={}),
        ):
            substitute = agent._try_substitute_unknown_model(
                requested_model="gemini-3.8-pro", provider="gemini",
            )
        assert substitute is not None
        assert substitute.model == "gemini-3.8-flash-high"
        assert substitute.provider == "gemini"
        assert substitute.source == "same_provider"
        assert agent.model == "gemini-3.8-flash-high"
        assert agent.provider == "gemini"
        # The fallback_providers chain must never be touched for this.
        assert agent._fallback_index == 0
        assert agent._fallback_activated is False
        # Same visibility bookkeeping try_activate_fallback uses, so the
        # job-card relabel logic (_model_label_for_child) picks it up with
        # no new plumbing.
        assert agent._provider_fallback_active is True
        assert calls and calls[0]["reason"] == "model_not_found_substituted"
        assert calls[0]["from_model"] == "gemini-3.8-pro"
        assert calls[0]["to_model"] == "gemini-3.8-flash-high"
        assert agent._model_substitution["requested_model"] == "gemini-3.8-pro"
        assert agent._model_substitution["actual_model"] == "gemini-3.8-flash-high"

    def test_case_a_logs_a_warning_naming_both_models(self, caplog):
        agent = _make_agent(model_pinned=True, provider="gemini")
        agent.model = "gemini-3.8-pro"
        agent.provider = "gemini"
        with (
            patch("clover_cli.models._PROVIDER_MODELS", {"gemini": ["gemini-3.8-flash-high"]}),
            patch("clover_cli.config.load_config", return_value={}),
            caplog.at_level(logging.WARNING, logger="agent.chat_completion_helpers"),
        ):
            agent._try_substitute_unknown_model(requested_model="gemini-3.8-pro", provider="gemini")
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "gemini-3.8-pro" in r.getMessage() and "gemini-3.8-flash-high" in r.getMessage()
            for r in warnings
        ), [r.getMessage() for r in warnings]

    def test_case_b_falls_back_to_configured_default_across_providers(self):
        """No usable same-provider match -- the user's own configured
        default model (a different provider here) is used instead."""
        agent = _make_agent(model_pinned=True, provider="gemini")
        agent.model = "gemini-3.8-pro"
        agent.provider = "gemini"
        cfg = {"model": {"default": "claude-opus-5-5", "provider": "anthropic"}}
        with (
            patch("clover_cli.models._PROVIDER_MODELS", {"gemini": []}),
            patch("clover_cli.config.load_config", return_value=cfg),
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(_mock_client(), "claude-opus-5-5"),
            ),
        ):
            substitute = agent._try_substitute_unknown_model(
                requested_model="gemini-3.8-pro", provider="gemini",
            )
        assert substitute is not None
        assert substitute.model == "claude-opus-5-5"
        assert substitute.provider == "anthropic"
        assert substitute.source == "configured_default"
        assert agent.model == "claude-opus-5-5"
        assert agent.provider == "anthropic"
        assert agent._fallback_index == 0
        assert agent._fallback_activated is False
        assert agent._provider_fallback_active is True

    def test_case_b_cross_provider_rebuild_updates_base_url_and_api_mode(self):
        """The bug this closes (#93412 follow-up): a cross-provider
        substitute switched ``agent.model``/``agent.provider`` but kept the
        OLD provider's client/base_url/api_mode, so the retry 404'd again
        against the wrong endpoint. The rebuild must mirror
        ``try_activate_fallback`` and update client, base_url, AND
        api_mode together."""
        agent = _make_agent(model_pinned=True, provider="custom")
        agent.model = "gemini-3.8-pro"
        agent.provider = "custom"
        agent.base_url = "http://127.0.0.1:8317/v1"
        agent.api_mode = "chat_completions"
        cfg = {"model": {"default": "gpt-6-astra-900k", "provider": "openai-codex"}}
        new_client = _mock_client(base_url="https://chatgpt.com/backend-api/codex/")
        with (
            patch("agent.model_substitute.known_models_for_provider", return_value=([], False)),
            patch("clover_cli.config.load_config", return_value=cfg),
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(new_client, "gpt-6-astra-900k"),
            ),
        ):
            substitute = agent._try_substitute_unknown_model(
                requested_model="gemini-3.8-pro", provider="custom",
            )
        assert substitute is not None
        assert substitute.provider == "openai-codex"
        assert agent.provider == "openai-codex"
        assert agent.requested_provider == "openai-codex"
        # The client/base_url must reflect the NEW provider, not the old
        # custom endpoint the pinned model 404'd against.
        assert agent.base_url == "https://chatgpt.com/backend-api/codex/"
        assert agent.base_url != "http://127.0.0.1:8317/v1"
        assert agent.client is new_client
        # openai-codex always speaks the Responses API -- api_mode must be
        # re-derived for the new provider, not left on the old provider's
        # chat_completions.
        assert agent.api_mode == "codex_responses"

    def test_case_c_nothing_qualifies_returns_none_and_leaves_agent_untouched(self):
        agent = _make_agent(model_pinned=True, provider="gemini")
        agent.model = "gemini-3.8-pro"
        agent.provider = "gemini"
        with (
            patch("clover_cli.models._PROVIDER_MODELS", {"gemini": []}),
            patch("clover_cli.config.load_config", return_value={}),
        ):
            substitute = agent._try_substitute_unknown_model(
                requested_model="gemini-3.8-pro", provider="gemini",
            )
        assert substitute is None
        assert agent.model == "gemini-3.8-pro"
        assert agent.provider == "gemini"
        assert agent._fallback_index == 0
        assert agent._fallback_activated is False
        assert getattr(agent, "_model_substitution", None) is None

    def test_case_a_and_b_never_touch_the_fallback_chain_even_when_configured(self):
        """A fully configured fallback_providers chain must stay completely
        unwalked -- substitution is not the same mechanism, even when a
        chain IS available (the exact typo->Opus bug: a chain existing is
        what let the old code silently walk it)."""
        agent = _make_agent(
            model_pinned=True,
            provider="gemini",
            fallback_model=[{"provider": "anthropic", "model": "claude-opus-5-5"}],
        )
        agent.model = "gemini-3.8-pro"
        agent.provider = "gemini"
        assert len(agent._fallback_chain) == 1
        with (
            patch("clover_cli.models._PROVIDER_MODELS", {"gemini": ["gemini-3.8-flash-high"]}),
            patch("clover_cli.config.load_config", return_value={}),
        ):
            substitute = agent._try_substitute_unknown_model(
                requested_model="gemini-3.8-pro", provider="gemini",
            )
        assert substitute is not None
        assert substitute.model == "gemini-3.8-flash-high"
        # The chain is still fully intact and unwalked.
        assert agent._fallback_index == 0
        assert agent._fallback_activated is False
