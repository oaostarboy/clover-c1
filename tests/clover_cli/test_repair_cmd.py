"""One-tap repair's snapshot, safe checks and non-destructive failure report."""
from unittest.mock import patch
import pytest
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


def test_repair_snapshots_before_any_fix_and_reports_safe_failure(tmp_path):
    from clover_cli import repair_cmd

    (tmp_path / "state.db").write_bytes(b"fixture")
    calls = []
    with patch("clover_cli.repair_cmd.get_clover_home", return_value=tmp_path), \
         patch("clover_cli.backup.create_quick_snapshot", side_effect=lambda **kw: calls.append("snapshot") or "snap"), \
         patch("clover_cli.update_cmd._run_post_update_safe_repairs", side_effect=lambda: calls.append("post")), \
         patch("clover_cli.update_cmd._venv_core_imports_healthy", side_effect=lambda: calls.append("venv") or (True, "")), \
         patch("clover_cli.update_cmd._check_and_apply_config_migration", side_effect=lambda **kw: calls.append("migration")), \
         patch("clover_cli.backup.verify_sqlite_integrity", side_effect=lambda *a, **kw: calls.append("db") or {"valid": True}), \
         patch("clover_cli.repair_cmd._repair_doctor_safe_items", side_effect=lambda: calls.append("doctor") or []):
        message = repair_cmd.run_repair()
    assert calls == ["snapshot", "post", "venv", "migration", "doctor", "db"]
    assert "Checked" in message and "chats" in message.lower()
    assert "Traceback" not in message


def test_slash_registered_and_gateway_routes_repair():
    from clover_cli.commands import resolve_command
    from gateway.run import GatewayRunner
    assert resolve_command("repair").name == "repair"
    assert "repair" in GatewayRunner._gateway_plain_command_handlers(GatewayRunner.__new__(GatewayRunner))


@pytest.mark.asyncio
async def test_gateway_repair_denies_unprivileged_without_spawning():
    from gateway.run import GatewayRunner
    from unittest.mock import MagicMock, AsyncMock
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._check_slash_access = MagicMock(return_value="admin only")
    runner._handle_update_command = AsyncMock()
    event = MagicMock()
    assert await runner._handle_repair_command(event) == "admin only"
    runner._check_slash_access.assert_called_once_with(event.source, "repair")
    runner._handle_update_command.assert_not_awaited()


def test_snapshot_failure_never_starts_repairs():
    from clover_cli import repair_cmd
    with patch("clover_cli.backup.create_quick_snapshot", return_value=None), \
         patch("clover_cli.update_cmd._run_post_update_safe_repairs") as fix:
        message = repair_cmd.run_repair()
    fix.assert_not_called()
    assert "nothing was repaired" in message


def test_corrupt_state_reports_valid_snapshot_without_restoring(tmp_path):
    import sqlite3
    from clover_cli import repair_cmd, backup, update_cmd
    home = tmp_path / "home"
    home.mkdir()
    source = home / "state.db"
    source.write_bytes(b"corrupt state that must not be overwritten")
    snapshot = home / "state-snapshots" / "known-good"
    snapshot.mkdir(parents=True)
    with sqlite3.connect(snapshot / "state.db") as db:
        db.execute("create table messages (text varchar)")
        db.execute("insert into messages values ('safe')")
    before = source.read_bytes()
    with patch.object(repair_cmd, "get_clover_home", return_value=home), \
         patch.object(backup, "create_quick_snapshot", return_value="fresh"), \
         patch.object(backup, "_quick_snapshot_root", return_value=snapshot.parent), \
         patch.object(update_cmd, "_run_post_update_safe_repairs"), \
         patch.object(update_cmd, "_venv_core_imports_healthy", return_value=(True, "")), \
         patch.object(update_cmd, "_check_and_apply_config_migration"), \
         patch.object(repair_cmd, "_repair_doctor_safe_items", return_value=[]):
        message = repair_cmd.run_repair()
    assert source.read_bytes() == before
    assert "/snapshot restore known-good" in message
    assert "whole snapshot" in message
    assert "Traceback" not in message


@pytest.mark.asyncio
async def test_gateway_repair_reuses_detached_update_ipc(tmp_path):
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = None
    runner._schedule_update_notification_watch = MagicMock()
    runner._session_key_for_source = MagicMock(return_value="repair-session")
    event = MagicMock()
    event.source.platform = Platform.TELEGRAM
    event.source.chat_type = "dm"
    event.source.chat_id = "12"
    event.source.user_id = "1"
    event.source.thread_id = None
    event.message_id = "34"
    with patch("gateway.run._clover_home", tmp_path), \
         patch("gateway.run._resolve_clover_bin", return_value=["clover"]), \
         patch("shutil.which", return_value=None), \
         patch("subprocess.Popen") as spawn:
        message = await runner._handle_repair_command(event)
    assert "safely repairing" in message
    assert json.loads((tmp_path / ".update_pending.json").read_text(encoding="utf-8"))["action"] == "repair"
    assert "repair" in spawn.call_args.args[0][-1]
    assert "update --gateway" not in spawn.call_args.args[0][-1]


@pytest.mark.asyncio
async def test_repair_watcher_sends_plain_result_then_restarts_after_dependency_fix(tmp_path):
    from tests.gateway.test_update_streaming import _make_runner
    from gateway.config import Platform
    runner = _make_runner()
    adapter = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    pending = {"action": "repair", "platform": "telegram", "chat_id": "12",
               "session_key": "agent:main:telegram:dm:12", "user_id": "1"}
    (tmp_path / ".update_pending.json").write_text(json.dumps(pending), encoding="utf-8")
    (tmp_path / ".update_output.txt").write_text(
        "Checked 6 things. Fixed 1: reinstalled missing packages. Your chats and memories weren't touched.\n",
        encoding="utf-8",
    )
    (tmp_path / ".update_exit_code").write_text("0", encoding="utf-8")
    with patch("gateway.run._clover_home", tmp_path), \
         patch("gateway.run._resolve_clover_bin", return_value=["clover"]), \
         patch("clover_constants.venv_python_path", return_value=Path(sys.executable)), \
         patch("subprocess.Popen") as restart:
        await runner._watch_update_progress(poll_interval=0.01, stream_interval=0.01)
    texts = [call.args[1] for call in adapter.send.await_args_list]
    assert len(texts) == 1
    assert texts[0].startswith("Checked 6 things")
    assert "```" not in texts[0] and "update finished" not in texts[0]
    assert restart.call_args.args[0][-2:] == ["gateway", "restart"]
