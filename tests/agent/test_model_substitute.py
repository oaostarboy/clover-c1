"""Pure substitute-selection logic for a pinned model that doesn't exist on
its provider (#93412 follow-up).

Order (stop at the first hit):
  (a) closest real model on the SAME provider (difflib, cutoff ~0.6)
  (b) the user's own configured default model, when it differs
  (c) nothing -- caller keeps the existing stop-with-error behaviour

These are unit tests of ``agent/model_substitute.py`` in isolation -- no
AIAgent, no network, no real provider catalog (catalog contents change
constantly; tests here inject synthetic ``known_models`` instead, per the
project's "no change-detector tests" rule).
"""

from __future__ import annotations

from agent.model_substitute import (
    ModelSubstitute,
    configured_default_model,
    resolve_model_substitute,
    substitute_unknown_models_enabled,
)


class TestSameProviderClosestMatch:
    def test_close_match_on_same_provider_wins(self):
        result = resolve_model_substitute(
            "gemini-3.8-pro",
            "gemini",
            known_models=["gemini-3.8-flash-high", "totally-unrelated"],
            default_model="claude-opus-5-5",
            default_provider="anthropic",
        )
        assert result == ModelSubstitute(
            model="gemini-3.8-flash-high", provider="gemini", source="same_provider",
        )

    def test_same_provider_match_takes_priority_over_configured_default(self):
        """Even when a configured default is also available, a good
        same-provider match is preferred -- staying on the plan the user is
        already paying for beats hopping providers."""
        result = resolve_model_substitute(
            "gpt-5.6-sol",
            "openai-codex",
            known_models=["gpt-5.6-sol-900k", "gpt-5.6-terra"],
            default_model="claude-opus-5-5",
            default_provider="anthropic",
        )
        assert result.source == "same_provider"
        assert result.provider == "openai-codex"

    def test_no_close_enough_match_falls_through(self):
        """Below the 0.6 cutoff, a same-provider guess is not confident
        enough to run -- fall through to the next tier instead."""
        result = resolve_model_substitute(
            "zzz-completely-different-name",
            "gemini",
            known_models=["gemini-3.8-flash-high"],
            default_model="claude-opus-5-5",
            default_provider="anthropic",
        )
        assert result == ModelSubstitute(
            model="claude-opus-5-5", provider="anthropic", source="configured_default",
        )


class TestConfiguredDefaultFallback:
    def test_used_when_no_provider_catalog_available(self):
        result = resolve_model_substitute(
            "gemini-3.8-pro",
            "gemini",
            known_models=[],
            default_model="claude-opus-5-5",
            default_provider="anthropic",
        )
        assert result == ModelSubstitute(
            model="claude-opus-5-5", provider="anthropic", source="configured_default",
        )

    def test_default_provider_missing_reuses_requested_provider(self):
        """A bare-string ``model: <name>`` config has no explicit provider --
        assume it's meant for the same provider as the pinned model."""
        result = resolve_model_substitute(
            "gemini-3.8-pro",
            "gemini",
            known_models=[],
            default_model="gemini-3.1-pro-preview",
            default_provider="",
        )
        assert result == ModelSubstitute(
            model="gemini-3.1-pro-preview", provider="gemini", source="configured_default",
        )

    def test_default_identical_to_requested_does_not_count(self):
        """The user's default IS the model that doesn't exist -- there is
        nothing to substitute to."""
        result = resolve_model_substitute(
            "gemini-3.8-pro",
            "gemini",
            known_models=[],
            default_model="gemini-3.8-pro",
            default_provider="gemini",
        )
        assert result is None


class TestNoSubstituteQualifies:
    def test_nothing_available_returns_none(self):
        result = resolve_model_substitute(
            "gemini-3.8-pro",
            "gemini",
            known_models=[],
            default_model="",
            default_provider="",
        )
        assert result is None

    def test_empty_known_models_and_no_default_returns_none(self):
        result = resolve_model_substitute(
            "gemini-3.8-pro", "gemini", known_models=None, default_model=None, default_provider=None,
        )
        assert result is None


class TestSubstituteUnknownModelsEnabled:
    def test_defaults_true_when_unset(self):
        assert substitute_unknown_models_enabled({}) is True
        assert substitute_unknown_models_enabled({"model": "gemini-3.8-pro"}) is True
        assert substitute_unknown_models_enabled(None) is True

    def test_explicit_false_disables_it(self):
        assert substitute_unknown_models_enabled({"model": {"substitute_unknown": False}}) is False

    def test_explicit_true_is_a_noop(self):
        assert substitute_unknown_models_enabled({"model": {"substitute_unknown": True}}) is True


class TestConfiguredDefaultModel:
    def test_bare_string_model_config(self):
        assert configured_default_model({"model": "claude-opus-5-5"}) == ("claude-opus-5-5", "")

    def test_dict_model_config_with_provider(self):
        cfg = {"model": {"default": "claude-opus-5-5", "provider": "anthropic"}}
        assert configured_default_model(cfg) == ("claude-opus-5-5", "anthropic")

    def test_dict_default_embeds_its_own_provider(self):
        """A dict-valued ``model.default`` pairs its own provider with the
        model string -- that must win over an unrelated top-level
        ``model.provider`` (mirrors split_model_config_default's contract)."""
        cfg = {
            "model": {
                "default": {"model": "claude-opus-5-5", "provider": "anthropic"},
                "provider": "auto",
            },
        }
        assert configured_default_model(cfg) == ("claude-opus-5-5", "anthropic")

    def test_unset_returns_empty_strings(self):
        assert configured_default_model({}) == ("", "")
        assert configured_default_model(None) == ("", "")
