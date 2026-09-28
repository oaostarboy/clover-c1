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

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import patch

from agent.model_substitute import (
    ModelSubstitute,
    configured_default_model,
    known_models_for_provider,
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

    def test_version_match_wins_over_higher_difflib_ratio_tier_match(self):
        """A candidate sharing the requested model's family+version
        ("gemini-3.8") must win even when a DIFFERENT version has a higher
        raw difflib ratio (e.g. shares the tier token "pro"). Tokens split
        on '-'; the family+version match is picked first, difflib is only
        the tiebreak within (or absent) that group."""
        result = resolve_model_substitute(
            "gemini-3.8-pro",
            "gemini-oauth",
            known_models=[
                "gemini-3.8-flash-high", "gemini-3.1-pro-low", "gemini-3-flash",
            ],
            default_model="",
            default_provider="",
        )
        assert result == ModelSubstitute(
            model="gemini-3.8-flash-high", provider="gemini-oauth", source="same_provider",
        )

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


class _ModelsHandler(BaseHTTPRequestHandler):
    """Serves /models with a configurable model list; counts requests."""

    models = [{"id": "model-a"}]
    request_count = 0

    def do_GET(self):
        type(self).request_count += 1
        if self.path.rstrip("/") == "/models":
            body = json.dumps({"data": self.models}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def _start_models_server(models):
    _ModelsHandler.models = models
    _ModelsHandler.request_count = 0
    server = HTTPServer(("127.0.0.1", 0), _ModelsHandler)
    port = server.server_address[1]
    Thread(target=server.serve_forever, daemon=True).start()
    return server, port


class TestKnownModelsForProvider:
    """``known_models_for_provider`` -- the static-catalog lookup a custom
    provider (billing class "custom") skips, plus the live ``/models``
    fallback that closes the gap (#93412 follow-up: a custom provider's
    catalog was silently empty, so the same-provider substitute never ran
    and a typo'd model jumped straight to a cross-provider default)."""

    def test_static_list_matched_by_provider(self):
        with patch("clover_cli.models._PROVIDER_MODELS", {"gemini": ["gemini-3.8-flash-high"]}):
            result = known_models_for_provider("gemini")
        assert result == ["gemini-3.8-flash-high"]

    def test_static_list_matched_by_requested_provider_when_provider_has_none(self):
        """A custom endpoint resolves to the billing class ``"custom"`` in
        ``provider`` -- but a catalog keyed by the user-facing
        ``requested_provider`` name must still be found."""
        with patch(
            "clover_cli.models._PROVIDER_MODELS",
            {"gemini-oauth": ["gemini-3.8-flash-high"]},
        ):
            result = known_models_for_provider("custom", requested_provider="gemini-oauth")
        assert result == ["gemini-3.8-flash-high"]

    def test_dynamic_fetch_used_when_no_static_list(self):
        """No static entry for either name -- probe the endpoint's own
        OpenAI-compatible ``/models`` instead of returning an empty list
        (which used to skip the same-provider substitute entirely)."""
        server, port = _start_models_server([{"id": "gemini-3.8-flash-high"}, {"id": "gemini-3.1-pro-low"}])
        try:
            with patch("clover_cli.models._PROVIDER_MODELS", {}):
                result = known_models_for_provider(
                    "custom",
                    requested_provider="gemini-oauth",
                    base_url=f"http://127.0.0.1:{port}",
                    api_key="test-key",
                )
        finally:
            server.shutdown()
        assert sorted(result) == ["gemini-3.1-pro-low", "gemini-3.8-flash-high"]

    def test_dynamic_fetch_is_cached_per_base_url_for_the_process(self):
        """A second lookup for the same base_url must not re-probe the
        endpoint -- the process-wide cache in agent.model_substitute."""
        import agent.model_substitute as model_substitute_mod

        server, port = _start_models_server([{"id": "solo-model"}])
        base_url = f"http://127.0.0.1:{port}"
        model_substitute_mod._DYNAMIC_MODELS_CACHE.pop(base_url.rstrip("/").lower(), None)
        try:
            with patch("clover_cli.models._PROVIDER_MODELS", {}):
                first = known_models_for_provider("custom", base_url=base_url)
                second = known_models_for_provider("custom", base_url=base_url)
        finally:
            server.shutdown()
        assert first == ["solo-model"]
        assert second == ["solo-model"]
        assert _ModelsHandler.request_count == 1

    def test_no_base_url_returns_empty_list(self):
        with patch("clover_cli.models._PROVIDER_MODELS", {}):
            assert known_models_for_provider("custom") == []
