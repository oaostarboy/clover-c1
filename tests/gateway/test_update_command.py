"""Tests for /update gateway slash command.

Tests both the _handle_update_command handler (spawns update process) and
the _send_update_notification startup hook (sends results after restart).
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text="/update", platform=Platform.TELEGRAM,
                user_id="12345", chat_id="67890", thread_id=None):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
        thread_id=thread_id,
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    """Create a bare GatewayRunner without calling __init__."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._update_prompt_pending = {}
    return runner


# ---------------------------------------------------------------------------
# _handle_update_command
# ---------------------------------------------------------------------------


class TestHandleUpdateCommand:
    """Tests for GatewayRunner._handle_update_command."""

    @pytest.mark.asyncio
    async def test_no_git_directory_is_not_refused(self, tmp_path):
        """A non-git install is let through; the spawned `clover update` adopts it."""
        runner = _make_runner()
        event = _make_event()
        fake_root = tmp_path / "project"
        (fake_root / "gateway").mkdir(parents=True)
        (fake_root / "gateway" / "slash_commands.py").touch()
        clover_home = tmp_path / "clover"
        clover_home.mkdir()
        fake_file = str(fake_root / "gateway" / "slash_commands.py")

        with patch("gateway.run._clover_home", clover_home), \
             patch("gateway.slash_commands.__file__", fake_file), \
             patch("shutil.which", side_effect=lambda x: "/usr/bin/clover" if x == "clover" else "/usr/bin/setsid"), \
             patch("subprocess.Popen") as popen:
            result = await runner._handle_update_command(event)

        assert "Not a git repository" not in result
        assert (clover_home / ".update_pending.json").exists()
        popen.assert_called()


    @pytest.mark.asyncio
    async def test_resolve_clover_bin_fallback(self):
        """_resolve_clover_bin falls back to sys.executable argv when which fails."""
        import sys
        from gateway.run import _resolve_clover_bin

        fake_spec = MagicMock()
        with patch("shutil.which", return_value=None), \
             patch("importlib.util.find_spec", return_value=fake_spec):
            result = _resolve_clover_bin()

        assert result == [sys.executable, "-m", "clover_cli.main"]


    @pytest.mark.asyncio
    async def test_writes_pending_marker(self, tmp_path):
        """Writes .update_pending.json with correct platform and chat info."""
        runner = _make_runner()
        event = _make_event(platform=Platform.TELEGRAM, chat_id="99999")
        event.message_id = "m-update"

        fake_root = tmp_path / "project"
        fake_root.mkdir()
        (fake_root / ".git").mkdir()
        (fake_root / "gateway").mkdir()
        (fake_root / "gateway" / "run.py").touch()
        fake_file = str(fake_root / "gateway" / "run.py")
        clover_home = tmp_path / "clover"
        clover_home.mkdir()

        with patch("gateway.run._clover_home", clover_home), \
             patch("gateway.run.__file__", fake_file), \
             patch("shutil.which", side_effect=lambda x: "/usr/bin/clover" if x == "clover" else "/usr/bin/setsid"), \
             patch("subprocess.Popen"):
            result = await runner._handle_update_command(event)

        pending_path = clover_home / ".update_pending.json"
        assert pending_path.exists()
        data = json.loads(pending_path.read_text(encoding="utf-8"))
        assert data["platform"] == "telegram"
        assert data["chat_id"] == "99999"
        assert data["chat_type"] == "dm"
        assert data["message_id"] == "m-update"
        assert "timestamp" in data
        assert not (clover_home / ".update_exit_code").exists()


    @pytest.mark.asyncio
    async def test_fallback_when_no_setsid(self, tmp_path):
        """Falls back to start_new_session=True when setsid is not available."""
        runner = _make_runner()
        event = _make_event()

        fake_root = tmp_path / "project"
        fake_root.mkdir()
        (fake_root / ".git").mkdir()
        (fake_root / "gateway").mkdir()
        (fake_root / "gateway" / "run.py").touch()
        fake_file = str(fake_root / "gateway" / "run.py")
        clover_home = tmp_path / "clover"
        clover_home.mkdir()

        mock_popen = MagicMock()

        def which_no_setsid(x):
            if x == "clover":
                return "/usr/bin/clover"
            if x == "setsid":
                return None
            return None

        with patch("gateway.run._clover_home", clover_home), \
             patch("gateway.run.__file__", fake_file), \
             patch("shutil.which", side_effect=which_no_setsid), \
             patch("subprocess.Popen", mock_popen):
            result = await runner._handle_update_command(event)

        # Verify plain bash -c fallback (no nohup, no setsid)
        call_args = mock_popen.call_args[0][0]
        assert call_args[0] == "bash"
        assert "nohup" not in call_args[2]
        assert ".update_exit_code" in call_args[2]
        # start_new_session=True should be in kwargs
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("start_new_session") is True
        assert "Starting Clover update" in result


# ---------------------------------------------------------------------------
# Platform allowlist gate
# ---------------------------------------------------------------------------


class TestUpdateCommandPlatformGate:
    """Tests for the platform-allowlist gate at the top of
    ``_handle_update_command``.  Built-in messaging platforms are listed in
    ``_UPDATE_ALLOWED_PLATFORMS``; plugin-migrated platforms (discord,
    mattermost, teams, …) are NOT in the frozenset and rely on the
    registry's ``allow_update_command=True`` fallback.  Programmatic
    interfaces (ACP, API server, webhooks) must be blocked.
    """


    @pytest.mark.asyncio
    async def test_allows_plugin_platform_via_registry_fallback(self, monkeypatch):
        """A plugin-migrated platform (DISCORD) is no longer in
        ``_UPDATE_ALLOWED_PLATFORMS`` but must still pass the gate via
        the registry's ``allow_update_command=True`` flag.

        This test is the empirical guarantee that removing DISCORD from
        the hardcoded frozenset does not regress the /update command for
        Discord users.
        """
        from gateway.run import GatewayRunner

        # Precondition: DISCORD is NOT in the hardcoded set anymore.
        assert Platform.DISCORD not in GatewayRunner._UPDATE_ALLOWED_PLATFORMS

        # Make sure the plugin registry is populated so the fallback fires.
        from clover_cli.plugins import PluginManager
        PluginManager().discover_and_load(force=True)
        from gateway.platform_registry import platform_registry
        discord_entry = platform_registry.get("discord")
        assert discord_entry is not None
        assert discord_entry.allow_update_command is True

        runner = _make_runner()
        event = _make_event(platform=Platform.DISCORD)
        monkeypatch.setenv("CLOVER_MANAGED", "")

        with patch("subprocess.Popen"):
            result = await runner._handle_update_command(event)

        # The gate must NOT have rejected us — anything other than the
        # ``platform_not_messaging`` rejection string is acceptable here.
        # Later steps may legitimately return success ("Starting Clover
        # update…") or fail for environment reasons.
        assert "only available from messaging platforms" not in result


    @pytest.mark.asyncio
    async def test_allows_homeassistant_via_registry_fallback(self, monkeypatch):
        """Same as DISCORD/MATTERMOST: HOMEASSISTANT is now plugin-migrated
        (PR #40709) and not in the hardcoded frozenset; the registry must
        keep /update working via ``allow_update_command=True``.
        """
        from gateway.run import GatewayRunner

        assert Platform.HOMEASSISTANT not in GatewayRunner._UPDATE_ALLOWED_PLATFORMS

        from clover_cli.plugins import PluginManager
        PluginManager().discover_and_load(force=True)
        from gateway.platform_registry import platform_registry
        ha_entry = platform_registry.get("homeassistant")
        assert ha_entry is not None
        assert ha_entry.allow_update_command is True

        runner = _make_runner()
        event = _make_event(platform=Platform.HOMEASSISTANT)
        monkeypatch.setenv("CLOVER_MANAGED", "")

        with patch("subprocess.Popen"):
            result = await runner._handle_update_command(event)

        assert "only available from messaging platforms" not in result


# ---------------------------------------------------------------------------
# _send_update_notification
# ---------------------------------------------------------------------------


class TestSendUpdateNotification:
    """Tests for GatewayRunner._send_update_notification."""


    @pytest.mark.asyncio
    async def test_defers_notification_while_update_still_running(self, tmp_path):
        """Returns False and keeps marker files when the update has not exited yet."""
        runner = _make_runner()
        clover_home = tmp_path / "clover"
        clover_home.mkdir()

        pending_path = clover_home / ".update_pending.json"
        pending_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "67890", "user_id": "12345",
        }))
        (clover_home / ".update_output.txt").write_text("still running", encoding="utf-8")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._clover_home", clover_home):
            result = await runner._send_update_notification()

        assert result is False
        mock_adapter.send.assert_not_called()
        assert pending_path.exists()

    @pytest.mark.asyncio
    async def test_recovers_from_claimed_pending_file(self, tmp_path):
        """A claimed pending file from a crashed notifier is still deliverable."""
        runner = _make_runner()
        clover_home = tmp_path / "clover"
        clover_home.mkdir()

        claimed_path = clover_home / ".update_pending.claimed.json"
        claimed_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "67890", "user_id": "12345",
        }))
        (clover_home / ".update_output.txt").write_text("done", encoding="utf-8")
        (clover_home / ".update_exit_code").write_text("0", encoding="utf-8")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._clover_home", clover_home):
            result = await runner._send_update_notification()

        assert result is True
        mock_adapter.send.assert_called_once()
        assert not claimed_path.exists()

    @pytest.mark.asyncio
    async def test_sends_notification_with_output(self, tmp_path):
        """Sends update output to the correct platform and chat."""
        runner = _make_runner()
        clover_home = tmp_path / "clover"
        clover_home.mkdir()

        # Write pending marker
        pending = {
            "platform": "telegram",
            "chat_id": "67890",
            "user_id": "12345",
            "timestamp": "2026-03-04T21:00:00",
        }
        (clover_home / ".update_pending.json").write_text(json.dumps(pending), encoding="utf-8")
        (clover_home / ".update_output.txt").write_text(
            "→ Found 3 new commit(s)\n✓ Code updated!\n✓ Update complete!"
        )
        (clover_home / ".update_exit_code").write_text("0", encoding="utf-8")

        # Mock the adapter
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._clover_home", clover_home):
            await runner._send_update_notification()

        mock_adapter.send.assert_called_once()
        call_args = mock_adapter.send.call_args
        assert call_args[0][0] == "67890"  # chat_id
        assert "Update complete" in call_args[0][1] or "update finished" in call_args[0][1].lower()


    @pytest.mark.asyncio
    async def test_cleans_up_on_error(self, tmp_path):
        """Files are cleaned up even if notification fails."""
        runner = _make_runner()
        clover_home = tmp_path / "clover"
        clover_home.mkdir()

        pending_path = clover_home / ".update_pending.json"
        output_path = clover_home / ".update_output.txt"
        exit_code_path = clover_home / ".update_exit_code"
        pending_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "111", "user_id": "222",
        }))
        output_path.write_text("✓ Done", encoding="utf-8")
        exit_code_path.write_text("0", encoding="utf-8")

        # Adapter send raises
        mock_adapter = AsyncMock()
        mock_adapter.send.side_effect = RuntimeError("network error")
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._clover_home", clover_home):
            await runner._send_update_notification()

        # Files should still be cleaned up (finally block)
        assert not pending_path.exists()
        assert not output_path.exists()
        assert not exit_code_path.exists()


    @pytest.mark.asyncio
    async def test_no_adapter_for_platform_preserves_markers(self, tmp_path):
        """A finished update whose platform is offline keeps its markers.

        When the target platform's adapter has not reconnected yet, dropping
        the completion markers would silently lose the notification. Instead the
        call defers (returns False) and leaves every marker on disk so a later
        retry can deliver once the platform is back.
        """
        runner = _make_runner()
        clover_home = tmp_path / "clover"
        clover_home.mkdir()

        pending = {"platform": "discord", "chat_id": "111", "user_id": "222"}
        pending_path = clover_home / ".update_pending.json"
        output_path = clover_home / ".update_output.txt"
        exit_code_path = clover_home / ".update_exit_code"
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        output_path.write_text("Done", encoding="utf-8")
        exit_code_path.write_text("0", encoding="utf-8")

        # Only telegram adapter available, but pending says discord
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._clover_home", clover_home):
            result = await runner._send_update_notification()

        # No send (wrong platform offline) and the result is deferred.
        assert result is False
        mock_adapter.send.assert_not_called()
        # Markers are preserved for a later retry — NOT cleaned up.
        assert pending_path.exists()
        assert output_path.exists()
        assert exit_code_path.exists()
        # The marker stays in its canonical pending location (claim restored).
        assert not (clover_home / ".update_pending.claimed.json").exists()

    @pytest.mark.asyncio
    async def test_deferred_notification_delivers_after_reconnect(self, tmp_path):
        """A deferred completion is delivered once the platform reconnects.

        Regression for the late-reconnect /update bug: the update finishes while
        the target platform is offline, the markers survive the deferral, and
        the next call (after the adapter is registered) delivers the result and
        cleans up — exactly once.
        """
        runner = _make_runner()
        clover_home = tmp_path / "clover"
        clover_home.mkdir()

        pending = {"platform": "discord", "chat_id": "111", "user_id": "222"}
        pending_path = clover_home / ".update_pending.json"
        output_path = clover_home / ".update_output.txt"
        exit_code_path = clover_home / ".update_exit_code"
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        output_path.write_text("✓ Update complete!", encoding="utf-8")
        exit_code_path.write_text("0", encoding="utf-8")

        # First pass: target platform (discord) is still offline → defer.
        with patch("gateway.run._clover_home", clover_home):
            first = await runner._send_update_notification()

        assert first is False
        assert pending_path.exists()

        # Platform reconnects: the reconnect watcher adds the adapter back.
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.DISCORD: mock_adapter}

        with patch("gateway.run._clover_home", clover_home):
            second = await runner._send_update_notification()

        assert second is True
        mock_adapter.send.assert_called_once()
        sent_text = mock_adapter.send.call_args[0][1]
        assert "Update complete" in sent_text
        # Now everything is cleaned up — no duplicate deliveries possible.
        assert not pending_path.exists()
        assert not output_path.exists()
        assert not exit_code_path.exists()
        assert not (clover_home / ".update_pending.claimed.json").exists()

    @pytest.mark.asyncio
    async def test_completion_notification_tolerates_invalid_utf8_output(self, tmp_path):
        """Completion-only update notifications must not crash on bad bytes."""
        runner = _make_runner()
        clover_home = tmp_path / "clover"
        clover_home.mkdir()

        pending = {"platform": "discord", "chat_id": "111", "user_id": "222"}
        pending_path = clover_home / ".update_pending.json"
        output_path = clover_home / ".update_output.txt"
        exit_code_path = clover_home / ".update_exit_code"
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        output_path.write_bytes(b"ok before\ninvalid byte: \x96\ncontinued after\n")
        exit_code_path.write_text("0", encoding="utf-8")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.DISCORD: mock_adapter}

        with patch("gateway.run._clover_home", clover_home):
            delivered = await runner._send_update_notification()

        assert delivered is True
        mock_adapter.send.assert_called_once()
        sent_text = mock_adapter.send.call_args[0][1]
        assert "ok before" in sent_text
        assert "invalid byte" in sent_text
        assert "continued after" in sent_text
        assert "Clover update finished" in sent_text
        assert not pending_path.exists()
        assert not output_path.exists()
        assert not exit_code_path.exists()


# ---------------------------------------------------------------------------
# /update in help and known_commands
# ---------------------------------------------------------------------------


class TestUpdateInHelp:
    """Verify /update appears in help text and known commands set."""


    def test_update_is_known_command(self):
        """/update dispatches through the gateway's plain-command handler table.

        (Was an inspect.getsource() check for the literal '"update"' in
        _handle_message — a banned source-reading test. The if-chain was
        replaced by _gateway_plain_command_handlers(), so assert the real
        dispatch contract: the table maps "update" to the update handler.)
        """
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        handlers = runner._gateway_plain_command_handlers()
        assert handlers.get("update") == runner._handle_update_command

class TestWatchUpdateProgress:
    @pytest.mark.asyncio
    async def test_invalid_utf8_update_output_does_not_crash_watcher(self, tmp_path):
        runner = _make_runner()
        clover_home = tmp_path / "clover"
        clover_home.mkdir()

        (clover_home / ".update_pending.json").write_text(json.dumps({
            "platform": "telegram",
            "chat_id": "67890",
            "user_id": "12345",
        }))
        (clover_home / ".update_output.txt").write_bytes(
            b"ok before\n\xe2\x9c invalid-continuation: \x96\ncontinued after\n"
        )
        (clover_home / ".update_exit_code").write_text("0", encoding="utf-8")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._clover_home", clover_home):
            await runner._watch_update_progress(poll_interval=0.01, stream_interval=0.01, timeout=1.0)

        sent = "\n".join(call.args[1] for call in mock_adapter.send.call_args_list)
        assert "ok before" in sent
        assert "continued after" in sent
        assert "Clover update finished" in sent
        assert not (clover_home / ".update_pending.json").exists()


class TestOfflineReplayedLifecycleCommands:
    """D7 (Windows 11 report): a /update queued while the bot was offline was
    replayed at boot and re-ran the update, taking the gateway down again."""

    def _event(self, text, age_seconds):
        from datetime import datetime, timedelta, timezone
        event = _make_event(text)
        event.timestamp = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        return event

    @pytest.mark.asyncio
    async def test_old_update_from_before_boot_is_skipped(self, tmp_path):
        import time
        runner = _make_runner()
        runner._startup_time = time.time() - 5  # booted 5 s ago
        clover_home = tmp_path / "clover"
        clover_home.mkdir()
        with patch("gateway.run._clover_home", clover_home), patch("subprocess.Popen") as popen:
            result = await runner._handle_update_command(self._event("/update", 300))
        assert "skipped an old /update" in result
        assert not (clover_home / ".update_pending.json").exists()
        popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_old_restart_from_before_boot_is_skipped(self, tmp_path):
        import time
        runner = _make_runner()
        runner._startup_time = time.time() - 5
        with patch("gateway.run._clover_home", tmp_path):
            result = await runner._handle_restart_command(self._event("/restart", 300))
        assert "skipped an old /restart" in result

    def test_fresh_or_post_boot_commands_still_run(self):
        import time
        runner = _make_runner()
        runner._startup_time = time.time() - 600  # up for 10 minutes
        assert runner._is_stale_offline_command(self._event("/update", 300)) is False  # sent after boot
        runner._startup_time = time.time() - 5
        assert runner._is_stale_offline_command(self._event("/update", 30)) is False  # recent
        assert runner._is_stale_offline_command(_make_event("/update")) is False  # no platform date


class TestChatUpdateTranscriptAndRefusal:
    """D8 (Windows 11 report): /update from chat left no transcript, and the
    refusal notice blamed "the running gateway" whatever the holder was."""

    async def _notify(self, clover_home, *, output, exit_code, refusal=None):
        runner = _make_runner()
        (clover_home / ".update_pending.json").write_text(json.dumps(
            {"platform": "telegram", "chat_id": "67890", "user_id": "12345",
             "timestamp": "2026-03-04T21:00:00"}))
        (clover_home / ".update_output.txt").write_text(output, encoding="utf-8")
        (clover_home / ".update_exit_code").write_text(str(exit_code), encoding="utf-8")
        if refusal is not None:
            (clover_home / ".update_refusal.json").write_text(json.dumps(refusal), encoding="utf-8")
        adapter = AsyncMock()
        adapter.send = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: adapter}
        with patch("gateway.run._clover_home", clover_home):
            await runner._send_update_notification()
        return adapter.send.call_args[0][1]

    @pytest.mark.asyncio
    async def test_transcript_is_kept_after_the_notification(self, tmp_path):
        home = tmp_path / "clover"
        home.mkdir()
        await self._notify(home, output="→ Found 3 new commit(s)\n✓ Update complete!", exit_code=0)
        kept = home / "logs" / "update-output.last.txt"
        assert kept.exists() and "Found 3 new commit" in kept.read_text(encoding="utf-8")
        await self._notify(home, output="second run", exit_code=0)
        assert (home / "logs" / "update-output.last.txt").read_text(encoding="utf-8") == "second run"
        assert "Found 3 new commit" in (home / "logs" / "update-output.last.1.txt").read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_refusal_names_the_real_holder(self, tmp_path):
        home = tmp_path / "clover"
        home.mkdir()
        msg = await self._notify(home, output="", exit_code=2, refusal={"holders": [
            {"pid": 7704, "name": "python.exe",
             "cmdline": "venv\\\\Scripts\\\\python.exe -m clover_cli.update_restart_watcher beacon"}]})
        assert "7704" in msg and "update_restart_watcher" in msg
        assert "The running gateway still holds" not in msg
        assert not (home / ".update_refusal.json").exists()


def test_gateway_mode_update_writes_a_transcript(tmp_path, monkeypatch):
    import sys
    from clover_cli import main as cli_main
    import clover_cli.config as config

    monkeypatch.setattr(config, "get_clover_home", lambda: tmp_path)
    state = cli_main._install_hangup_protection(gateway_mode=True)
    try:
        print("→ Fetching updates...")
    finally:
        cli_main._finalize_update_output(state)
    log = (tmp_path / "logs" / "update.log").read_text(encoding="utf-8")
    assert "clover update --gateway (from chat) started" in log
    assert "→ Fetching updates..." in log
    assert sys.stdout is state["prev_stdout"]
