"""One-tap repair's snapshot, safe checks and non-destructive failure report."""
from unittest.mock import patch
import pytest


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
