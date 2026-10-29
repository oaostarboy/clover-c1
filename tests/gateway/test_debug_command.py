"""Tests for the gateway /debug command."""

from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text="/debug", platform=Platform.TELEGRAM,
                user_id="12345", chat_id="67890"):
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {}
    return runner


class TestHandleDebugCommand:
    @pytest.mark.asyncio
    async def test_debug_sweeps_expired_pastes_before_upload(self):
        runner = _make_runner()
        event = _make_event()

        with patch("clover_cli.debug._sweep_expired_pastes", return_value=(0, 0)) as mock_sweep, \
             patch("clover_cli.debug._capture_dump", return_value="dump"), \
             patch("clover_cli.debug.collect_debug_report", return_value="report"), \
             patch("clover_cli.debug.upload_to_pastebin", return_value="https://paste.rs/report"), \
             patch("clover_cli.debug._schedule_auto_delete"):
            result = await runner._handle_debug_command(event)

        mock_sweep.assert_called_once()
        assert "https://paste.rs/report" in result

    @pytest.mark.asyncio
    async def test_debug_handles_upload_failure_with_local_save(self, tmp_path, monkeypatch):
        runner = _make_runner()
        event = _make_event()
        monkeypatch.setenv("CLOVER_HOME", str(tmp_path))

        with patch("clover_cli.debug._sweep_expired_pastes", return_value=(0, 0)), \
             patch("clover_cli.debug._capture_dump", return_value="dump"), \
             patch("clover_cli.debug.collect_debug_report", return_value="report text"), \
             patch("clover_cli.debug.upload_to_pastebin", side_effect=RuntimeError("paste services down")):
            result = await runner._handle_debug_command(event)

        assert "paste services down" in result
        assert "debug_report_" in result
        pastes_dir = tmp_path / "pastes"
        saved = list(pastes_dir.glob("debug_report_*.txt"))
        assert len(saved) == 1
        assert saved[0].read_text() == "report text"

    @pytest.mark.asyncio
    async def test_debug_handles_upload_failure_and_sends_document(self, tmp_path, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock
        runner = _make_runner()
        event = _make_event(platform=Platform.TELEGRAM)
        monkeypatch.setenv("CLOVER_HOME", str(tmp_path))

        mock_adapter = MagicMock()
        mock_adapter.send_document = AsyncMock()
        runner.adapters[Platform.TELEGRAM] = mock_adapter

        with patch("clover_cli.debug._sweep_expired_pastes", return_value=(0, 0)), \
             patch("clover_cli.debug._capture_dump", return_value="dump"), \
             patch("clover_cli.debug.collect_debug_report", return_value="report text"), \
             patch("clover_cli.debug.upload_to_pastebin", side_effect=RuntimeError("paste services down")):
            result = await runner._handle_debug_command(event)

        mock_adapter.send_document.assert_awaited_once()
        assert "attached above" in result

