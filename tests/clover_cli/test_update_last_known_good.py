"""Post-restart rollback contracts (no actual gateway restarts)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from clover_cli import update_restart_watcher as watcher


def test_probe_requires_stable_service_and_core_imports(monkeypatch, tmp_path):
    from itertools import cycle
    ticks = cycle([False, True, True, False])
    monkeypatch.setattr(watcher, "_gateway_running", lambda: next(ticks))
    monkeypatch.setattr(watcher, "_core_imports_healthy", lambda root: True)
    assert watcher.probe_gateway(tmp_path, timeout=0.05, stable_seconds=2, poll=0) is False
    monkeypatch.setattr(watcher, "_gateway_running", lambda: True)
    monkeypatch.setattr(watcher, "_core_imports_healthy", lambda root: False)
    assert watcher.probe_gateway(tmp_path, timeout=0, stable_seconds=0, poll=0) is False


@pytest.mark.parametrize("healthy", [True, False])
def test_rollback_only_once_when_probe_fails(monkeypatch, tmp_path, healthy):
    events = []
    probes = iter([healthy, True])
    monkeypatch.setattr(watcher, "probe_gateway", lambda *a, **kw: next(probes))
    monkeypatch.setattr(watcher, "_rollback_checkout", lambda *a, **kw: events.append("rollback"))
    monkeypatch.setattr(watcher, "_restart_from_beacon", lambda *a, **kw: events.append("restart"))
    data = {"pre_pull_sha": "a" * 40, "repo": str(tmp_path), "gateway_argv": ["clover"]}
    assert watcher.verify_or_rollback(data, tmp_path / "beacon.json", timeout=0, stable_seconds=0) == ("healthy" if healthy else "rolled-back")
    assert events == ([] if healthy else ["rollback", "restart"])


def test_dead_updater_uses_watcher_rollback(monkeypatch, tmp_path):
    beacon = tmp_path / "beacon.json"
    beacon.write_text(json.dumps({"updater_pid": 999999, "refreshed_at": 0,
        "gateway_argv": ["clover"], "cwd": str(tmp_path),
        "repo": str(tmp_path), "pre_pull_sha": "b" * 40}), encoding="utf-8")
    monkeypatch.setattr(watcher, "_pid_alive", lambda pid: False)
    probes = iter([False, True])
    monkeypatch.setattr(watcher, "probe_gateway", lambda *a, **kw: next(probes))
    actions = []
    monkeypatch.setattr(watcher, "_rollback_checkout", lambda *a, **kw: actions.append("rollback"))
    monkeypatch.setattr(watcher, "_restart_from_beacon", lambda *a, **kw: actions.append("restart"))
    assert watcher.watch(beacon, poll=0) == "rolled-back"
    assert actions == ["rollback", "restart"]


def test_rollback_message_never_leaks_traceback():
    assert "Traceback" not in watcher.ROLLBACK_MESSAGE
    assert "version you had before" in watcher.ROLLBACK_MESSAGE


def test_windows_service_rollback_uses_existing_restore_path(monkeypatch, tmp_path):
    calls = []
    from types import SimpleNamespace
    monkeypatch.setattr(watcher, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(watcher.subprocess, "run", lambda args, **kw: calls.append(args))
    watcher._restart_from_beacon({"repo": str(tmp_path), "windows_services": ["clover-gateway"]})
    assert "_restore_windows_gateway_service" in calls[0][2]
    assert calls[0][-1] == "clover-gateway"


def test_probe_rejects_restarting_gateway_even_if_always_running(monkeypatch, tmp_path):
    from itertools import cycle
    monkeypatch.setattr(watcher, "_gateway_running", lambda: True)
    monkeypatch.setattr(watcher, "_core_imports_healthy", lambda root: True)
    monkeypatch.setattr(watcher, "_gateway_identity", lambda: next(cycle_ids))
    cycle_ids = cycle([1, 2])
    assert not watcher.probe_gateway(tmp_path, timeout=0.05, stable_seconds=0.02, poll=0)


def test_receipt_preserves_current_run_when_updater_dies(tmp_path, monkeypatch):
    from clover_cli import update_receipt
    monkeypatch.setattr(update_receipt, "_receipt_dir", lambda: tmp_path)
    update_receipt.begin_update_receipt()
    try:
        update_receipt.save_pending_receipt("a" * 40, "snap-1")
        record = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
        assert record["outcome"] == "running"
        assert record["pre_pull_sha"] == "a" * 40
        assert record["pre_update_snapshot_id"] == "snap-1"
    finally:
        update_receipt._current = None


def test_rollback_keeps_local_checkout_edits(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout.strip()
    git("init", "-q")
    git("config", "user.email", "tester")
    git("config", "user.name", "Test")
    source = tmp_path / "file.txt"
    source.write_text("old", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "old")
    old_sha = git("rev-parse", "HEAD")
    source.write_text("new", encoding="utf-8")
    git("commit", "-qam", "new")
    source.write_text("my edit", encoding="utf-8")
    watcher._rollback_checkout({"repo": str(tmp_path), "pre_pull_sha": old_sha}, tmp_path / "beacon")
    assert git("rev-parse", "HEAD") == old_sha
    assert source.read_text(encoding="utf-8") == "my edit" or "stash@" in git("stash", "list")


def test_probe_rejects_old_gateway_identity_even_if_process_is_up(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "gateway_state.json").write_text(json.dumps({"code_sha": "old"}), encoding="utf-8")
    monkeypatch.setattr(watcher, "_gateway_running", lambda: True)
    monkeypatch.setattr(watcher, "_core_imports_healthy", lambda root: True)
    monkeypatch.setattr(watcher, "_gateway_identity", lambda: (123, 1.0))
    monkeypatch.setattr(watcher, "_current_head", lambda root: "new")
    assert not watcher.probe_gateway(tmp_path, home=home, timeout=0.03, stable_seconds=0, poll=0)


@pytest.mark.parametrize("supervisor,helper", [
    ("systemd", "_restart_systemd_gateway_units_best_effort"),
    ("launchd", "_restart_macos_launchd_gateways"),
])
def test_rollback_restarts_via_existing_service_manager(monkeypatch, tmp_path, supervisor, helper):
    calls = []
    monkeypatch.setattr(watcher.subprocess, "run", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(watcher.subprocess, "Popen", lambda *a, **kw: pytest.fail("manual duplicate gateway"))
    watcher._restart_from_beacon({"repo": str(tmp_path), "supervisor": supervisor,
                                  "gateway_argv": ["clover"]})
    assert helper in calls[0][2]
