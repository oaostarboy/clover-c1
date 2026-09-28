"""Unit tests for tools/tool_backend_helpers.py.

Tests cover:
- normalize_browser_cloud_provider() coercion
- coerce_modal_mode() / normalize_modal_mode() validation
- has_direct_modal_credentials() detection
- resolve_modal_backend_state() backend selection matrix
- resolve_openai_audio_api_key() priority chain
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from tools.tool_backend_helpers import (
    coerce_modal_mode,
    has_direct_modal_credentials,
    normalize_browser_cloud_provider,
    normalize_modal_mode,
    resolve_modal_backend_state,
    resolve_openai_audio_api_key,
)


# ---------------------------------------------------------------------------
# normalize_browser_cloud_provider
# ---------------------------------------------------------------------------
class TestNormalizeBrowserCloudProvider:
    """Coerce arbitrary input to a lowercase browser provider key."""

    def test_none_returns_default(self):
        assert normalize_browser_cloud_provider(None) == "local"


    def test_integer_coerced(self):
        result = normalize_browser_cloud_provider(42)
        assert isinstance(result, str)
        assert result == "42"


# ---------------------------------------------------------------------------
# coerce_modal_mode / normalize_modal_mode
# ---------------------------------------------------------------------------
class TestCoerceModalMode:
    """Validate and coerce the requested modal execution mode."""

    @pytest.mark.parametrize("value", ["auto", "direct"])
    def test_valid_modes_passthrough(self, value):
        assert coerce_modal_mode(value) == value

    def test_none_returns_auto(self):
        assert coerce_modal_mode(None) == "auto"


    def test_strips_whitespace(self):
        assert coerce_modal_mode("  direct  ") == "direct"


class TestNormalizeModalMode:
    """normalize_modal_mode is an alias for coerce_modal_mode."""

    def test_delegates_to_coerce(self):
        assert normalize_modal_mode("direct") == coerce_modal_mode("direct")
        assert normalize_modal_mode(None) == coerce_modal_mode(None)
        assert normalize_modal_mode("bogus") == coerce_modal_mode("bogus")


# ---------------------------------------------------------------------------
# has_direct_modal_credentials
# ---------------------------------------------------------------------------
class TestHasDirectModalCredentials:
    """Detect Modal credentials via env vars or config file."""

    def test_no_env_no_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
        with patch.object(Path, "home", return_value=tmp_path):
            assert has_direct_modal_credentials() is False


    def test_only_token_secret_not_enough(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        monkeypatch.setenv("MODAL_TOKEN_SECRET", "sec-456")
        with patch.object(Path, "home", return_value=tmp_path):
            assert has_direct_modal_credentials() is False


    def test_env_vars_take_priority_over_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MODAL_TOKEN_ID", "id-123")
        monkeypatch.setenv("MODAL_TOKEN_SECRET", "sec-456")
        (tmp_path / ".modal.toml").touch()
        with patch.object(Path, "home", return_value=tmp_path):
            assert has_direct_modal_credentials() is True

    def test_home_dir_permission_denied(self, monkeypatch):
        """PermissionError on Path.home() should not crash (issue #33525)."""
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
        with patch.object(Path, "home", side_effect=PermissionError("denied")):
            assert has_direct_modal_credentials() is False

    def test_home_dir_permission_denied_with_env_vars(self, monkeypatch):
        """PermissionError on Path.home() should not prevent env var detection."""
        monkeypatch.setenv("MODAL_TOKEN_ID", "id-123")
        monkeypatch.setenv("MODAL_TOKEN_SECRET", "sec-456")
        with patch.object(Path, "home", side_effect=PermissionError("denied")):
            assert has_direct_modal_credentials() is True


# ---------------------------------------------------------------------------
# resolve_modal_backend_state
# ---------------------------------------------------------------------------
class TestResolveModalBackendState:
    """Direct-only Modal backend selection."""

    # --- auto mode ---

    def test_auto_selects_direct_when_available(self):
        result = resolve_modal_backend_state("auto", has_direct=True)
        assert result["selected_backend"] == "direct"

    def test_auto_none_when_no_credentials(self):
        result = resolve_modal_backend_state("auto", has_direct=False)
        assert result["selected_backend"] is None

    # --- direct mode ---

    def test_direct_selects_direct_when_available(self):
        result = resolve_modal_backend_state("direct", has_direct=True)
        assert result["selected_backend"] == "direct"

    def test_direct_none_when_no_credentials(self):
        result = resolve_modal_backend_state("direct", has_direct=False)
        assert result["selected_backend"] is None

    # --- invalid mode falls back to auto ---

    def test_invalid_mode_treated_as_auto(self):
        result = resolve_modal_backend_state("bogus", has_direct=True)
        assert result["requested_mode"] == "auto"
        assert result["mode"] == "auto"


# ---------------------------------------------------------------------------
# resolve_openai_audio_api_key
# ---------------------------------------------------------------------------
class TestResolveOpenaiAudioApiKey:
    """Priority: VOICE_TOOLS_OPENAI_KEY > OPENAI_API_KEY."""

    def test_voice_key_preferred(self, monkeypatch):
        monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "voice-key")
        monkeypatch.setenv("OPENAI_API_KEY", "general-key")
        assert resolve_openai_audio_api_key() == "voice-key"


    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "  voice-key  ")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert resolve_openai_audio_api_key() == "voice-key"


# ---------------------------------------------------------------------------
# resolve_openai_audio_api_key — profile secret scope
# ---------------------------------------------------------------------------
class TestResolveOpenaiAudioApiKeyIsProfileScoped:
    """The key this returns authenticates the TTS/STT client.

    In a multiplex gateway ``os.environ`` holds whichever profile's ``.env``
    loaded at boot, not the profile the current turn belongs to — so a raw
    read here would let one profile's voice reply or voice-note transcription
    run on (and be billed to) another profile's OpenAI account. Same contract
    ``agent/vertex_adapter`` and the WeChat send path already follow.
    """

    @pytest.fixture(autouse=True)
    def _reset_multiplex(self):
        from agent import secret_scope as ss

        ss.set_multiplex_active(False)
        yield
        ss.set_multiplex_active(False)

    def test_scope_wins_over_another_profiles_environ(self, monkeypatch):
        from agent import secret_scope as ss

        monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-other-profile")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"OPENAI_API_KEY": "sk-this-profile"})
        try:
            assert resolve_openai_audio_api_key() == "sk-this-profile", (
                "voice/STT authenticated with another profile's OpenAI key"
            )
        finally:
            ss.reset_secret_scope(token)


    def test_single_profile_still_reads_environ(self, monkeypatch):
        """Control: no multiplexing, no scope — unchanged behaviour."""
        monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-plain")
        assert resolve_openai_audio_api_key() == "sk-plain"
