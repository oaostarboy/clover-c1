"""tts.openai.api_key / base_url from config.yaml drive the OpenAI audio client.

Regression coverage for issue #26175: the resolver was env-only
(VOICE_TOOLS_OPENAI_KEY / OPENAI_API_KEY) and always returned the default
OpenAI base URL, ignoring the tts.openai config block. Resolution order now
mirrors the STT resolver: config -> env.
"""
from unittest.mock import patch

import pytest

from tools import tts_tool


class TestResolveOpenaiAudioClientConfig:
    def test_prefers_tts_config_credentials_and_base_url(self):
        config = {
            "provider": "openai",
            "openai": {
                "api_key": "cfg-key",
                "base_url": "http://localhost:4003/v1",
            },
        }

        with patch.object(tts_tool, "_load_tts_config", return_value=config), \
             patch.object(tts_tool, "read_selection", return_value="openai"), \
             patch.object(tts_tool, "resolve_openai_audio_api_key", return_value="env-key"):
            assert tts_tool._resolve_openai_audio_client_config() == (
                "cfg-key",
                "http://localhost:4003/v1",
            )

    def test_config_without_base_url_falls_back_to_default_openai_base(self):
        config = {"openai": {"api_key": "cfg-key"}}

        with patch.object(tts_tool, "_load_tts_config", return_value=config), \
             patch.object(tts_tool, "read_selection", return_value=None):
            assert tts_tool._resolve_openai_audio_client_config() == (
                "cfg-key",
                tts_tool.DEFAULT_OPENAI_BASE_URL,
            )

    def test_stale_clover_selection_requires_direct_credentials(self):
        """A legacy stored 'clover' selection is now treated like any other
        vendor name — direct credentials only, no managed gateway."""
        config = {"openai": {"api_key": "cfg-key", "base_url": "http://localhost:4003/v1"}}

        with patch.object(tts_tool, "_load_tts_config", return_value=config), \
             patch.object(tts_tool, "read_selection", return_value="clover"), \
             patch.object(tts_tool, "resolve_openai_audio_api_key", return_value="env-key"):
            assert tts_tool._resolve_openai_audio_client_config() == (
                "cfg-key",
                "http://localhost:4003/v1",
            )

    def test_stale_clover_selection_missing_key_raises_selection_error(self):
        """A stored legacy 'clover' selection with no credentials errors by
        name — never a silent fallback."""
        with patch.object(tts_tool, "_load_tts_config", return_value={}), \
             patch.object(tts_tool, "read_selection", return_value="clover"), \
             patch.object(tts_tool, "resolve_openai_audio_api_key", return_value=""):
            with pytest.raises(ValueError) as exc:
                tts_tool._resolve_openai_audio_client_config()
        assert "clover" in str(exc.value)
        assert "clover tools" in str(exc.value)

    def test_vendor_selection_missing_key_raises_selection_error(self):
        """A stored vendor selection with no credentials errors by name."""
        with patch.object(tts_tool, "_load_tts_config", return_value={"provider": "openai"}), \
             patch.object(tts_tool, "read_selection", return_value="openai"), \
             patch.object(tts_tool, "resolve_openai_audio_api_key", return_value=""):
            with pytest.raises(ValueError) as exc:
                tts_tool._resolve_openai_audio_client_config()
        assert "openai" in str(exc.value)
        assert "clover tools" in str(exc.value)

    def test_missing_config_and_env_raises_updated_error(self):
        with patch.object(tts_tool, "_load_tts_config", return_value={}), \
             patch.object(tts_tool, "read_selection", return_value=None), \
             patch.object(tts_tool, "resolve_openai_audio_api_key", return_value=""):
            with pytest.raises(ValueError) as exc:
                tts_tool._resolve_openai_audio_client_config()

        assert (
            str(exc.value)
            == "Neither tts.openai.api_key in config nor VOICE_TOOLS_OPENAI_KEY/OPENAI_API_KEY is set"
        )

    def test_config_api_key_counts_as_available_backend(self):
        config = {"openai": {"api_key": "cfg-key"}}
        with patch.object(tts_tool, "_load_tts_config", return_value=config):
            assert tts_tool._has_openai_audio_backend() is True
