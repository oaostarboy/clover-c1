"""Tests for Bug #12905 fixes in agent/anthropic_adapter.py — macOS Keychain support."""

import json
import subprocess
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

from agent import anthropic_credentials
from agent.anthropic_adapter import (
    _read_claude_code_credentials_from_keychain,
    read_claude_code_credentials,
    _refresh_oauth_token,
)
from agent.anthropic_credentials import (
    _keychain_marked_unavailable,
    _mark_keychain_unavailable,
    _read_keychain_generic_password,
)


# This module exercises the reader itself with explicit platform and subprocess
# mocks, so it opts out of the suite-wide guard without touching a real Keychain.
pytestmark = pytest.mark.allow_macos_keychain


@pytest.fixture(autouse=True)
def _reset_keychain_unavailable_cache():
    """Every test starts with a clean cache, and a failure-marking test
    (e.g. a mocked timeout) must not poison the cache for the next test in
    this file/process."""
    anthropic_credentials._keychain_unavailable_until = 0.0
    yield
    anthropic_credentials._keychain_unavailable_until = 0.0


@pytest.mark.macos_only
class TestReadClaudeCodeCredentialsFromKeychain:
    """Bug 4: macOS Keychain support for Claude Code >=2.1.114.

    ``macos_only``: the reader is gated on ``platform.system() == "Darwin"``
    and shells out to the ``security`` CLI. Faking Darwin on Linux selected
    the branch but proved nothing about the host it exists for; on the real
    macOS runner only ``subprocess.run`` is mocked (via the
    ``allow_macos_keychain`` opt-out of the suite-wide guard), so no real
    Keychain is ever touched.
    """



    def test_returns_none_when_security_command_not_found(self):
        """OSError from missing security binary must be handled gracefully."""
        with patch("agent.anthropic_adapter.subprocess.run",
                   side_effect=OSError("security not found")):
            assert _read_claude_code_credentials_from_keychain() is None

    def test_returns_none_on_nonzero_exit_code(self):
        """security returns non-zero when the Keychain entry doesn't exist."""
        with patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            assert _read_claude_code_credentials_from_keychain() is None


class TestKeychainUnavailableCache:
    """The cache primitives take ``now`` as data (see ``hidden_windows_child_options``
    convention in tests/conftest.py), so they are host-independent — no
    ``macos_only`` marker, no faking ``platform.system()``.
    """

    def test_not_marked_before_any_failure(self):
        assert _keychain_marked_unavailable(now=1_000.0) is False

    def test_marked_unavailable_immediately_after_a_failure(self):
        _mark_keychain_unavailable(now=1_000.0)
        assert _keychain_marked_unavailable(now=1_000.0) is True

    def test_still_marked_a_minute_later(self):
        _mark_keychain_unavailable(now=1_000.0)
        assert _keychain_marked_unavailable(now=1_060.0) is True

    def test_cache_expires_after_the_window(self):
        _mark_keychain_unavailable(now=1_000.0)
        assert _keychain_marked_unavailable(now=1_000.0 + 301.0) is False


class TestReadKeychainGenericPasswordCaching:
    """``_read_keychain_generic_password`` makes no platform check itself
    (the Darwin gate lives one layer up in
    ``_read_claude_code_credentials_from_keychain``), so its subprocess/cache
    behavior is directly testable without needing real or faked macOS.
    """

    def test_repeated_timeout_only_calls_subprocess_once_within_the_cache_window(self):
        """A headless launchd gateway must not re-pay the 5s timeout on every
        refresh — only once per cache window."""
        with patch(
            "agent.anthropic_credentials.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="security", timeout=5),
        ) as mock_run:
            first = _read_keychain_generic_password()
            second = _read_keychain_generic_password()

        assert first is None
        assert second is None
        mock_run.assert_called_once()

    def test_retries_once_the_cache_window_expires(self):
        with patch(
            "agent.anthropic_credentials.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="security", timeout=5),
        ) as mock_run:
            _read_keychain_generic_password()

        # Simulate the cache window having elapsed.
        anthropic_credentials._keychain_unavailable_until = 0.0

        with patch("agent.anthropic_credentials.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            assert _read_keychain_generic_password() is None
            mock_run.assert_called_once()

    def test_a_successful_lookup_does_not_mark_the_cache(self):
        with patch("agent.anthropic_credentials.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            _read_keychain_generic_password()
            _read_keychain_generic_password()

        assert mock_run.call_count == 2







@pytest.mark.macos_only
class TestReadClaudeCodeCredentialsPriority:
    """Bug 4: Keychain must be checked before the JSON file."""

    def test_keychain_takes_priority_over_json_file(self, tmp_path, monkeypatch):
        """When both Keychain and JSON file have credentials, Keychain wins."""
        # Set up JSON file with "older" token
        json_cred_file = tmp_path / ".claude" / ".credentials.json"
        json_cred_file.parent.mkdir(parents=True)
        json_cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "json-token",
                "refreshToken": "json-refresh",
                "expiresAt": 9999999999999,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        # Mock Keychain to return a "newer" token
        with patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({
                    "claudeAiOauth": {
                        "accessToken": "keychain-token",
                        "refreshToken": "keychain-refresh",
                        "expiresAt": 9999999999999,
                    }
                }),
                stderr="",
            )
            creds = read_claude_code_credentials()

        # Keychain token should be returned, not JSON file token
        assert creds is not None
        assert creds["accessToken"] == "keychain-token"
        assert creds["source"] == "macos_keychain"

    def test_falls_back_to_json_when_keychain_returns_none(self, tmp_path, monkeypatch):
        """When Keychain has no entry, JSON file is used as fallback."""
        json_cred_file = tmp_path / ".claude" / ".credentials.json"
        json_cred_file.parent.mkdir(parents=True)
        json_cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "json-fallback-token",
                "refreshToken": "json-refresh",
                "expiresAt": 9999999999999,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        with patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            # Simulate Keychain entry not found
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            creds = read_claude_code_credentials()

        assert creds is not None
        assert creds["accessToken"] == "json-fallback-token"
        assert creds["source"] == "claude_code_credentials_file"

    def test_returns_none_when_neither_keychain_nor_json_has_creds(self, tmp_path, monkeypatch):
        """No credentials anywhere — must return None cleanly."""
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        with patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            creds = read_claude_code_credentials()

        assert creds is None


@pytest.mark.macos_only
class TestReadClaudeCodeCredentialsDesync:
    """Reconciliation when Keychain and JSON file disagree.

    Observed in the wild on Claude Code 2.1.x: a refresh updates one source
    (commonly the JSON file) but leaves the other holding an expired token.
    The reader must not blindly return whichever source it consulted first;
    it must prefer the non-expired credential.
    """

    # Far-future ms-epoch — comfortably valid under is_claude_code_token_valid.
    _FRESH = 9_999_999_999_999
    # Past ms-epoch — comfortably expired (with the 60s buffer).
    _EXPIRED = 1

    def _setup(self, tmp_path, monkeypatch, *, file_expires_at, file_token="json-token"):
        json_cred_file = tmp_path / ".claude" / ".credentials.json"
        json_cred_file.parent.mkdir(parents=True)
        json_cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": file_token,
                "refreshToken": "json-refresh",
                "expiresAt": file_expires_at,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

    def _keychain_payload(self, *, access_token, expires_at, refresh_token="kc-refresh"):
        return MagicMock(
            returncode=0,
            stdout=json.dumps({
                "claudeAiOauth": {
                    "accessToken": access_token,
                    "refreshToken": refresh_token,
                    "expiresAt": expires_at,
                }
            }),
            stderr="",
        )

    def test_keychain_expired_file_fresh_returns_file(self, tmp_path, monkeypatch):
        """Regression: when the Keychain holds an expired token but the JSON
        file has a valid one, callers must receive the valid file token rather
        than None. (Pre-fix behavior returned the expired Keychain token, and
        downstream validity checks then yielded None — surfacing the misleading
        ``No Anthropic credentials found`` error.)
        """
        self._setup(tmp_path, monkeypatch, file_expires_at=self._FRESH, file_token="fresh-file-token")
        with patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = self._keychain_payload(
                access_token="stale-keychain-token", expires_at=self._EXPIRED,
            )
            creds = read_claude_code_credentials()

        assert creds is not None
        assert creds["accessToken"] == "fresh-file-token"
        assert creds["source"] == "claude_code_credentials_file"



    def test_both_expired_prefers_later_expiry(self, tmp_path, monkeypatch):
        """When both are expired, return the one with the later ``expiresAt``;
        its ``refresh_token`` is the most recently issued and most likely to
        succeed at the OAuth refresh endpoint.
        """
        self._setup(tmp_path, monkeypatch, file_expires_at=self._EXPIRED + 5, file_token="newer-expired-file")
        with patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = self._keychain_payload(
                access_token="older-expired-keychain", expires_at=self._EXPIRED,
            )
            creds = read_claude_code_credentials()

        assert creds is not None
        assert creds["accessToken"] == "newer-expired-file"


class TestRefreshOAuthTokenAdoptsFreshCredential:
    """``_refresh_oauth_token`` should adopt a credential Claude Code has
    already refreshed rather than POSTing a (possibly already-rotated)
    single-use refresh token and racing Claude Code into ``invalid_grant``.
    """

    _FRESH = 9_999_999_999_999

    def test_adopts_already_refreshed_token_without_posting(self, tmp_path, monkeypatch):
        """When a live source already holds a valid token, return it and skip
        the network refresh entirely.
        """
        monkeypatch.setattr(
            "agent.anthropic_credentials.claude_code_credentials_path",
            lambda: tmp_path / ".claude" / ".credentials.json",
        )
        fresh = {
            "accessToken": "already-refreshed-token",
            "refreshToken": "live-refresh",
            "expiresAt": self._FRESH,
        }
        monkeypatch.setattr(
            "agent.anthropic_credentials.read_claude_code_credentials",
            lambda: fresh,
        )

        def _should_not_be_called(*args, **kwargs):  # pragma: no cover - guard
            raise AssertionError("refresh_anthropic_oauth_pure must not be called")

        monkeypatch.setattr(
            "agent.anthropic_credentials.refresh_anthropic_oauth_pure",
            _should_not_be_called,
        )

        # Stale creds passed in by the caller — should be ignored in favor
        # of the live, already-refreshed token.
        result = _refresh_oauth_token({"refreshToken": "stale", "expiresAt": 1})
        assert result == "already-refreshed-token"

    def test_falls_back_to_network_refresh_when_no_fresh_credential(self, tmp_path, monkeypatch):
        """When no live source has a valid token, fall back to refreshing
        ourselves using the freshest available refresh token.
        """
        monkeypatch.setattr(
            "agent.anthropic_credentials.claude_code_credentials_path",
            lambda: tmp_path / ".claude" / ".credentials.json",
        )
        # Live read returns an expired credential carrying a refresh token.
        monkeypatch.setattr(
            "agent.anthropic_credentials.read_claude_code_credentials",
            lambda: {"accessToken": "expired", "refreshToken": "live-refresh", "expiresAt": 1},
        )
        captured = {}

        def _fake_refresh(refresh_token, **kwargs):
            captured["refresh_token"] = refresh_token
            return {
                "access_token": "newly-minted",
                "refresh_token": "rotated",
                "expires_at_ms": self._FRESH,
            }

        monkeypatch.setattr(
            "agent.anthropic_credentials.refresh_anthropic_oauth_pure", _fake_refresh
        )
        monkeypatch.setattr(
            "agent.anthropic_credentials._write_claude_code_credentials",
            lambda *a, **k: None,
        )

        result = _refresh_oauth_token({"refreshToken": "caller-refresh", "expiresAt": 1})
        assert result == "newly-minted"
        # Prefers the live source's refresh token over the caller's stale copy.
        assert captured["refresh_token"] == "live-refresh"

    def test_concurrent_refreshes_use_one_shared_credentials_lock(self, tmp_path, monkeypatch):
        """Direct resolver refreshes must not spend one Claude token twice."""
        shared_credentials_path = tmp_path / ".claude" / ".credentials.json"
        monkeypatch.setattr(
            "agent.anthropic_credentials.claude_code_credentials_path",
            lambda: shared_credentials_path,
        )

        state = {
            "accessToken": "stale-access",
            "refreshToken": "stale-refresh",
            "expiresAt": 1,
        }
        state_lock = threading.Lock()
        calls = []

        def read_credentials():
            with state_lock:
                return dict(state)

        def write_credentials(access_token, refresh_token, expires_at_ms, **_kwargs):
            with state_lock:
                state.update(
                    accessToken=access_token,
                    refreshToken=refresh_token,
                    expiresAt=expires_at_ms,
                )

        def refresh(refresh_token, **_kwargs):
            calls.append(refresh_token)
            # Without the production shared lock, both callers read the stale
            # pair before either fake network request commits its rotation.
            time.sleep(0.05)
            with state_lock:
                if state["refreshToken"] != refresh_token:
                    raise ValueError("invalid_grant: refresh token already used")
                return {
                    "access_token": "fresh-access",
                    "refresh_token": "fresh-refresh",
                    "expires_at_ms": self._FRESH,
                }

        monkeypatch.setattr("agent.anthropic_credentials.read_claude_code_credentials", read_credentials)
        monkeypatch.setattr("agent.anthropic_credentials._write_claude_code_credentials", write_credentials)
        monkeypatch.setattr("agent.anthropic_credentials.refresh_anthropic_oauth_pure", refresh)

        results = {}
        errors = {}
        start = threading.Barrier(2)

        def run(name):
            try:
                start.wait(timeout=5)
                results[name] = _refresh_oauth_token(
                    {
                        "accessToken": "stale-access",
                        "refreshToken": "stale-refresh",
                        "expiresAt": 1,
                    }
                )
            except BaseException as exc:  # pragma: no cover - failure diagnostics
                errors[name] = exc

        threads = [threading.Thread(target=run, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert not [thread for thread in threads if thread.is_alive()]
        assert not errors, errors
        assert results == {"a": "fresh-access", "b": "fresh-access"}
        assert calls == ["stale-refresh"], calls

