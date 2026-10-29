"""Regression test for Emilio's Windows update bug report (2026-10-29).

Bug report: `clover update` aborts with "PID <n>: no owner-bound gateway status"
on Windows after git pull because:
1. The gateway on Windows has an extra venv launcher process layer
   (venv/Scripts/python.exe wrapping uv-managed cpython python.exe).
2. The guard in update_delivery_guard.py did not recognize this legitimate
   gateway process tree, treating the launcher parent as an unmapped unknown gateway.
3. During rollback watcher arming on Windows, update_cmd.py attempted to look up
   _gateway_restart_argv_for_resume on clover_cli.main where it was not exported.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import clover_cli.gateway as c_gw
from clover_cli import main as clover_main, update_cmd, update_delivery_guard as guard
from clover_constants import get_clover_home
import gateway.status as status
import psutil


def test_windows_venv_launcher_wrapping_uv_interpreter_admitted(monkeypatch, tmp_path):
    """A Windows venv launcher parent wrapping the uv-managed gateway worker is admitted."""
    monkeypatch.setattr("sys.platform", "win32")

    launcher_pid = 17608
    worker_pid = 5944
    venv_python = str(tmp_path / "venv/Scripts/python.exe")
    uv_python = str(tmp_path / "uv/python/cpython-3.11.16/bin/python.exe")

    launcher_cmd = [venv_python, "-m", "clover_cli.main", "gateway", "run", "--replace"]
    worker_cmd = [uv_python, "-m", "clover_cli.main", "gateway", "run", "--replace"]

    starts = {launcher_pid: 100, worker_pid: 102}
    parents = {worker_pid: launcher_pid, launcher_pid: 1}
    commands = {launcher_pid: launcher_cmd, worker_pid: worker_cmd}

    row = {
        "pid": worker_pid,
        "kind": "clover-gateway",
        "start_time": 102,
        "clover_home": str(tmp_path),
        "update_restart_capability": guard.CAPABILITY,
    }
    state_file = tmp_path / "gateway_state.json"
    state_file.write_text(json.dumps(row), encoding="utf-8")

    discovered = [launcher_pid, worker_pid]
    mock_profiles = lambda **kw: [
        SimpleNamespace(profile="default", path=tmp_path, pid=worker_pid, create_time=102)
    ]
    mock_pids = lambda **kw: discovered

    monkeypatch.setattr("clover_cli.gateway.find_profile_gateway_processes", mock_profiles)
    monkeypatch.setattr(c_gw, "find_profile_gateway_processes", mock_profiles)
    monkeypatch.setattr("clover_cli.gateway.find_gateway_pids", mock_pids)
    monkeypatch.setattr(c_gw, "find_gateway_pids", mock_pids)
    monkeypatch.setattr("clover_cli.update_cmd._gateway_pid_install_verdict", lambda pid: "unknown")
    monkeypatch.setattr(update_cmd, "_gateway_pid_install_verdict", lambda pid: "unknown")
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: True)
    monkeypatch.setattr(status, "_pid_exists", lambda pid: True)
    monkeypatch.setattr("gateway.status._get_process_start_time", lambda pid: starts.get(pid))
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: starts.get(pid))

    def mock_process(pid):
        if pid not in commands:
            raise psutil.NoSuchProcess(pid)
        return SimpleNamespace(
            cmdline=lambda: commands[pid],
            ppid=lambda: parents.get(pid, 1),
            environ=lambda: {"CLOVER_HOME": str(tmp_path)},
            pid=pid,
        )

    monkeypatch.setattr(psutil, "Process", mock_process)

    # Both launcher and worker are running; guard must admit the launcher and report no unsafe gateways
    reasons = guard._unsafe_gateways()
    assert reasons == []

    # require_delivery_safe_update should succeed
    record = guard.require_delivery_safe_update()
    assert record["status"] == "capable"
    assert record["reasons"] == []


def test_windows_unknown_gateway_without_admitted_child_fails_closed(monkeypatch, tmp_path):
    """An unknown gateway process on Windows that does NOT wrap an admitted child fails closed."""
    monkeypatch.setattr("sys.platform", "win32")

    unknown_pid = 12740
    venv_python = str(tmp_path / "venv/Scripts/python.exe")
    unknown_cmd = [venv_python, "-m", "clover_cli.main", "gateway", "run", "--replace"]

    starts = {unknown_pid: 200}
    commands = {unknown_pid: unknown_cmd}

    mock_profiles = lambda **kw: []
    mock_pids = lambda **kw: [unknown_pid]

    monkeypatch.setattr("clover_cli.gateway.find_profile_gateway_processes", mock_profiles)
    monkeypatch.setattr(c_gw, "find_profile_gateway_processes", mock_profiles)
    monkeypatch.setattr("clover_cli.gateway.find_gateway_pids", mock_pids)
    monkeypatch.setattr(c_gw, "find_gateway_pids", mock_pids)
    monkeypatch.setattr("clover_cli.update_cmd._gateway_pid_install_verdict", lambda pid: "unknown")
    monkeypatch.setattr(update_cmd, "_gateway_pid_install_verdict", lambda pid: "unknown")
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: True)
    monkeypatch.setattr(status, "_pid_exists", lambda pid: True)
    monkeypatch.setattr("gateway.status._get_process_start_time", lambda pid: starts.get(pid))
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: starts.get(pid))

    def mock_process(pid):
        if pid not in commands:
            raise psutil.NoSuchProcess(pid)
        return SimpleNamespace(
            cmdline=lambda: commands[pid],
            ppid=lambda: 1,
            environ=lambda: {},
            pid=pid,
        )

    monkeypatch.setattr(psutil, "Process", mock_process)

    reasons = guard._unsafe_gateways()
    assert len(reasons) == 1
    assert "PID 12740: no owner-bound gateway status" in reasons[0]

    with pytest.raises(SystemExit) as exc:
        guard.require_delivery_safe_update()
    assert exc.value.code == 1


def test_rollback_watcher_resume_argv_resolution_on_main():
    """Ensure _gateway_restart_argv_for_resume can be resolved from clover_cli.main without AttributeError."""
    assert hasattr(clover_main, "_gateway_restart_argv_for_resume")
    fn = getattr(clover_main, "_gateway_restart_argv_for_resume")
    assert callable(fn)
    token = {
        "profiles": {"default": 17608},
        "unmapped": [{"pid": 17608, "argv": ["python", "-m", "clover_cli.main", "gateway", "run"]}],
    }
    result = fn(token)
    assert isinstance(result, list)


def test_managed_uv_skip_delivery_guard_flag(monkeypatch):
    """managed_uv.update_managed_uv honors skip_delivery_guard=True."""
    from clover_cli import managed_uv
    import clover_cli.update_delivery_guard as c_guard

    guard_called = []
    mock_guard = lambda: guard_called.append(True) or {"status": "capable", "reasons": []}
    monkeypatch.setattr(c_guard, "require_delivery_safe_update", mock_guard)
    monkeypatch.setattr("clover_cli.update_delivery_guard.require_delivery_safe_update", mock_guard)
    if hasattr(managed_uv, "require_delivery_safe_update"):
        monkeypatch.setattr(managed_uv, "require_delivery_safe_update", mock_guard)

    monkeypatch.setattr("sys.argv", ["clover", "update"])
    monkeypatch.setattr(managed_uv, "resolve_uv", lambda: None)
    monkeypatch.setattr(managed_uv, "ensure_uv", lambda **kw: "uv")

    managed_uv.update_managed_uv(skip_delivery_guard=True)
    assert guard_called == [], "require_delivery_safe_update should not be called when skip_delivery_guard=True"

    managed_uv.update_managed_uv(skip_delivery_guard=False)
    assert guard_called == [True], "require_delivery_safe_update should be called when skip_delivery_guard=False"
