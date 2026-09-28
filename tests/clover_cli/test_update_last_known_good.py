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
