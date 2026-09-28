"""Strict tool-provider selection: the `clover tools` choice always wins.

Policy (owner decision): the provider string stored in config.yaml is what
runs at call time. A vendor name → that vendor direct with the user's own
credentials; a legacy "clover" selection is treated like any other
unrecognized vendor name (direct path, honest selection-naming error); no
key ever written ⇒ today's credential autodetect. Credential presence must
NEVER select or reroute; a selected-but-broken provider produces an honest
error naming the selection and pointing at `clover tools`.

Per category these tests pin the strict behaviors:
  (a) vendor selection + key missing ⇒ selection-naming error
  (b) never-configured ⇒ legacy autodetect unchanged
"""

from unittest.mock import MagicMock, patch

import pytest

from tools import tool_backend_helpers as tbh


# ---------------------------------------------------------------------------
# read_selection — the shared helper
# ---------------------------------------------------------------------------


class TestReadSelection:
    def _with_raw(self, raw):
        return patch(
            "clover_cli.config.read_raw_config_readonly",
            return_value=raw,
        )

    def test_never_configured_returns_none(self):
        with self._with_raw({}):
            assert tbh.read_selection("image_gen") is None

    def test_vendor_provider_returned(self):
        with self._with_raw({"image_gen": {"provider": "fal"}}):
            assert tbh.read_selection("image_gen") == "fal"

    def test_clover_provider_returned(self):
        with self._with_raw({"image_gen": {"provider": "clover"}}):
            assert tbh.read_selection("image_gen") == "clover"

    def test_legacy_use_gateway_true_maps_to_clover(self):
        """Old configs stored use_gateway: true beside a vendor name — only
        the managed picker row ever wrote it, so it means 'clover'."""
        with self._with_raw({"video_gen": {"provider": "fal", "use_gateway": True}}):
            assert tbh.read_selection("video_gen") == "clover"

    def test_legacy_use_gateway_false_keeps_vendor(self):
        with self._with_raw({"tts": {"provider": "openai", "use_gateway": False}}):
            assert tbh.read_selection("tts") == "openai"

    def test_empty_string_backend_is_no_selection(self):
        """DEFAULT_CONFIG's seeded empty strings are not selections."""
        with self._with_raw({"web": {"backend": ""}}):
            assert tbh.read_selection("web") is None

    def test_raw_stt_local_is_a_selection(self):
        """A raw config.yaml ``stt.provider: local`` is a genuine pick: the
        DEFAULT_CONFIG seed never reached disk (save_config strips schema
        defaults), and the current picker's Local Whisper row writes exactly
        this shape (provider only, legacy use_gateway popped). Treating it
        as no-selection would silently discard the user's choice."""
        with self._with_raw({"stt": {"provider": "local"}}):
            assert tbh.read_selection("stt") == "local"

    def test_stt_local_with_use_gateway_key_is_a_selection(self):
        """A picker-written stt section (use_gateway key present) means
        local was a genuine choice."""
        with self._with_raw({"stt": {"provider": "local", "use_gateway": False}}):
            assert tbh.read_selection("stt") == "local"

    def test_browser_backend_key_is_not_the_cloud_selection(self):
        """browser.backend is the driver choice (browser-use CLI vs built-in
        tools), not the cloud provider selection."""
        with self._with_raw({"browser": {"backend": "browser-use"}}):
            assert tbh.read_selection("browser") is None

    def test_web_per_capability_keys_mark_configured(self):
        with self._with_raw({"web": {"search_backend": "searxng"}}):
            assert tbh.read_selection("web") is None
            assert tbh.selection_exists("web") is True


# ---------------------------------------------------------------------------
# Image generation (FAL)
# ---------------------------------------------------------------------------


class TestImageFalStrictSelection:
    def test_clover_selection_missing_key_raises_selection_error(self):
        """A stale 'clover' selection is now a plain vendor name — missing
        FAL_KEY is a selection-naming error, never a managed reroute."""
        from tools import image_generation_tool as it

        with patch.object(it, "read_selection", return_value="clover"), \
             patch.object(it, "fal_key_is_configured", return_value=False), \
             patch.object(it, "_load_fal_client"):
            with pytest.raises(ValueError) as exc:
                it._submit_fal_request("fal-ai/some-model", {})
        assert "image_gen is configured to use clover" in str(exc.value)
        assert "FAL_KEY" in str(exc.value)
        assert "clover tools" in str(exc.value)

    def test_fal_selection_missing_key_errors(self):
        from tools import image_generation_tool as it

        with patch.object(it, "read_selection", return_value="fal"), \
             patch.object(it, "fal_key_is_configured", return_value=False), \
             patch.object(it, "_load_fal_client"):
            with pytest.raises(ValueError) as exc:
                it._submit_fal_request("fal-ai/some-model", {})
        assert "FAL_KEY" in str(exc.value)
        assert "image_gen is configured to use fal" in str(exc.value)
        assert "clover tools" in str(exc.value)

    def test_fal_selection_with_key_calls_fal_client_directly(self):
        from tools import image_generation_tool as it

        mock_client = MagicMock()
        with patch.object(it, "read_selection", return_value="fal"), \
             patch.object(it, "fal_key_is_configured", return_value=True), \
             patch.object(it, "_load_fal_client"), \
             patch.object(it, "fal_client", mock_client):
            it._submit_fal_request("fal-ai/some-model", {"prompt": "x"})
        mock_client.submit.assert_called_once()
        assert mock_client.submit.call_args[0][0] == "fal-ai/some-model"

    def test_never_configured_calls_fal_client_directly(self):
        from tools import image_generation_tool as it

        mock_client = MagicMock()
        with patch.object(it, "read_selection", return_value=None), \
             patch.object(it, "fal_key_is_configured", return_value=True), \
             patch.object(it, "_load_fal_client"), \
             patch.object(it, "fal_client", mock_client):
            it._submit_fal_request("fal-ai/some-model", {})
        mock_client.submit.assert_called_once()

    def test_check_fal_api_key_reflects_key_presence(self):
        from tools import image_generation_tool as it

        with patch.object(it, "fal_key_is_configured", return_value=False):
            assert it.check_fal_api_key() is False
        with patch.object(it, "fal_key_is_configured", return_value=True):
            assert it.check_fal_api_key() is True


# ---------------------------------------------------------------------------
# Video generation (FAL plugin)
# ---------------------------------------------------------------------------


class TestVideoFalStrictSelection:
    def test_clover_selection_missing_key_raises_selection_error(self):
        from plugins.video_gen import fal as vf

        with patch("tools.tool_backend_helpers.read_selection", return_value="clover"), \
             patch("tools.tool_backend_helpers.fal_key_is_configured", return_value=False), \
             patch.object(vf, "_load_fal_client"):
            with pytest.raises(ValueError) as exc:
                vf._submit_fal_video_request("fal-ai/some-model", {})
        assert "video_gen is configured to use clover" in str(exc.value)
        assert "FAL_KEY" in str(exc.value)

    def test_fal_selection_missing_key_errors(self):
        from plugins.video_gen import fal as vf

        with patch("tools.tool_backend_helpers.read_selection", return_value="fal"), \
             patch("tools.tool_backend_helpers.fal_key_is_configured", return_value=False), \
             patch.object(vf, "_load_fal_client"):
            with pytest.raises(ValueError) as exc:
                vf._submit_fal_video_request("fal-ai/some-model", {})
        assert "video_gen is configured to use fal" in str(exc.value)
        assert "FAL_KEY" in str(exc.value)

    def test_never_configured_autodetect_unchanged(self):
        from plugins.video_gen import fal as vf

        mock_client = MagicMock()
        with patch("tools.tool_backend_helpers.read_selection", return_value=None), \
             patch("tools.tool_backend_helpers.fal_key_is_configured", return_value=True), \
             patch.object(vf, "_load_fal_client"), \
             patch.object(vf, "_fal_client", mock_client):
            vf._submit_fal_video_request("fal-ai/some-model", {})
        mock_client.submit.assert_called_once()


# ---------------------------------------------------------------------------
# STT (OpenAI audio resolver — previously ignored the stored intent entirely)
# ---------------------------------------------------------------------------


class TestSttStrictSelection:
    def test_stale_clover_selection_requires_direct_credentials(self):
        from tools import transcription_tools as tt

        with patch.object(tt, "_load_stt_config", return_value={"openai": {"api_key": "sk-direct"}}), \
             patch("tools.tool_backend_helpers.read_selection", return_value="clover"):
            api_key, base_url = tt._resolve_openai_audio_client_config()
        assert api_key == "sk-direct"

    def test_vendor_selection_missing_key_errors(self):
        from tools import transcription_tools as tt

        with patch.object(tt, "_load_stt_config", return_value={}), \
             patch("tools.tool_backend_helpers.read_selection", return_value="openai"), \
             patch.object(tt, "resolve_openai_audio_api_key", return_value=""):
            with pytest.raises(ValueError) as exc:
                tt._resolve_openai_audio_client_config()
        assert "stt is configured to use openai" in str(exc.value)
        assert "clover tools" in str(exc.value)

    def test_never_configured_keeps_legacy_ladder(self):
        from tools import transcription_tools as tt

        with patch.object(tt, "_load_stt_config", return_value={}), \
             patch("tools.tool_backend_helpers.read_selection", return_value=None), \
             patch.object(tt, "resolve_openai_audio_api_key", return_value="sk-env"):
            api_key, base_url = tt._resolve_openai_audio_client_config()
        assert api_key == "sk-env"


# ---------------------------------------------------------------------------
# Browser Use provider
# ---------------------------------------------------------------------------


class TestBrowserUseStrictSelection:
    def _provider(self):
        from plugins.browser.browser_use.provider import BrowserUseBrowserProvider

        return BrowserUseBrowserProvider()

    def test_vendor_selection_missing_key_errors(self):
        provider = self._provider()
        with patch("plugins.browser.browser_use.provider.get_secret", return_value=""), \
             patch("tools.tool_backend_helpers.read_selection", return_value="browser-use"):
            with pytest.raises(ValueError) as exc:
                provider._get_config()
        assert "browser is configured to use browser-use" in str(exc.value)
        assert "BROWSER_USE_API_KEY" in str(exc.value)

    def test_never_configured_key_still_routes_direct(self):
        provider = self._provider()
        with patch("plugins.browser.browser_use.provider.get_secret", return_value="bu-key"), \
             patch("tools.tool_backend_helpers.read_selection", return_value=None):
            config = provider._get_config_or_none()
        assert config["api_key"] == "bu-key"


# ---------------------------------------------------------------------------
# Camofox: selection over env var
# ---------------------------------------------------------------------------


class TestCamofoxSelection:
    def test_camofox_selection_activates_mode(self, monkeypatch):
        from tools import browser_camofox as bc

        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
        with patch.object(bc, "_config_cdp_url", return_value=""), \
             patch("tools.tool_backend_helpers.read_selection", return_value="camofox"):
            assert bc.is_camofox_mode() is True

    def test_other_selection_beats_camofox_url_env(self, monkeypatch):
        """CAMOFOX_URL is the ADDRESS, not the choice: an explicit different
        browser selection wins."""
        from tools import browser_camofox as bc

        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
        with patch.object(bc, "_config_cdp_url", return_value=""), \
             patch.object(bc, "get_camofox_url", return_value="http://localhost:9377"), \
             patch("tools.tool_backend_helpers.read_selection", return_value="local"):
            assert bc.is_camofox_mode() is False

    def test_never_configured_env_url_still_activates(self, monkeypatch):
        from tools import browser_camofox as bc

        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
        with patch.object(bc, "_config_cdp_url", return_value=""), \
             patch.object(bc, "get_camofox_url", return_value="http://localhost:9377"), \
             patch("tools.tool_backend_helpers.read_selection", return_value=None):
            assert bc.is_camofox_mode() is True


# ---------------------------------------------------------------------------
# tools_config writers: one provider string per row, no use_gateway writes
# ---------------------------------------------------------------------------


class TestWriteProviderConfig:
    def test_byok_row_writes_vendor_and_clears_legacy_flag(self):
        from clover_cli.tools_config import _write_provider_config

        config = {"web": {"backend": "clover", "use_gateway": True}}
        provider = {"name": "Tavily", "web_backend": "tavily"}
        _write_provider_config(provider, config)
        assert config["web"]["backend"] == "tavily"
        assert "use_gateway" not in config["web"]

    def test_plugin_injected_byok_row_clears_stale_use_gateway(self):
        """Plugin-injected rows are not in TOOL_CATEGORIES' hardcoded
        provider lists; the legacy clear-loop skipped them."""
        from clover_cli.tools_config import _write_provider_config

        config = {"stt": {"provider": "clover", "use_gateway": True}}
        provider = {"name": "Groq Whisper", "stt_provider": "groq"}
        _write_provider_config(provider, config)
        assert config["stt"]["provider"] == "groq"
        assert "use_gateway" not in config["stt"]
