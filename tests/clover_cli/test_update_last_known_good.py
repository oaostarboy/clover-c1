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


def test_rollback_keeps_local_checkout_edits(tmp_path, monkeypatch):
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
    real_run = subprocess.run
    def run_without_install(args, **kw):
        if args[0] == "git":
            return real_run(args, **kw)
        if args[:3] == ["/fake/uv", "venv", "venv"]:
            python = tmp_path / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("interpreter", encoding="utf-8")
        return type("Result", (), {"stdout": "", "returncode": 0})()
    monkeypatch.setattr(watcher.subprocess, "run", run_without_install)
    monkeypatch.setattr(watcher.shutil, "which", lambda cmd: "/fake/uv" if cmd == "uv" else None)
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


def test_cli_ready_marker_makes_live_updater_watcher_probe(monkeypatch, tmp_path):
    beacon = watcher.write_beacon(["clover"], clover_home=tmp_path,
                                  repo=str(tmp_path), pre_pull_sha="a" * 40)
    monkeypatch.setattr(watcher, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(watcher, "verify_or_rollback", lambda *a, **kw: "healthy")
    monkeypatch.setattr(watcher.subprocess, "run", lambda *a, **kw: type("R", (),
                        {"returncode": 0, "stdout": "b" * 40})())
    watcher.mark_ready_for_probe(clover_home=tmp_path)
    assert watcher.watch(beacon, poll=0) == "healthy"
    assert not beacon.exists()


def test_cli_reads_rollback_result_without_traceback(monkeypatch, tmp_path, capsys):
    beacon = watcher.write_beacon(["clover"], clover_home=tmp_path,
                                  repo=str(tmp_path), pre_pull_sha="a" * 40)
    receipt = tmp_path / "logs" / "update_receipts" / "latest.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"outcome": "rolled-back", "user_message": watcher.ROLLBACK_MESSAGE}),
                       encoding="utf-8")
    def finish(**kw):
        beacon.unlink()
        return True
    monkeypatch.setattr(watcher, "mark_ready_for_probe", finish)
    assert watcher.wait_for_cli_verdict(clover_home=tmp_path, timeout=0.1) is False
    assert watcher.ROLLBACK_MESSAGE in capsys.readouterr().out


@pytest.mark.asyncio
async def test_gateway_notifies_original_chat_after_rollback(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock
    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    receipt = tmp_path / "logs" / "update_receipts" / "latest.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"started_at": datetime.now(timezone.utc).isoformat(),
                                   "outcome": "rolled-back", "post_update": {"sha": "new"}}),
                       encoding="utf-8")
    pending = tmp_path / ".update_pending.json"
    pending.write_text("{}", encoding="utf-8")
    adapter = type("Adapter", (), {"send": AsyncMock()})()
    runner = object.__new__(gateway_run.GatewayRunner)
    paths = [pending] + [tmp_path / name for name in
                         ("claimed", "output", "exit-code", "prompt")]
    await runner._conclude_update_after_updater_death(
        *paths, adapter=adapter, chat_id="original-chat", session_key=None,
        metadata={}, platform=None)
    assert adapter.send.await_args.args[0] == "original-chat"
    assert adapter.send.await_args.args[1] == watcher.ROLLBACK_MESSAGE


def test_watcher_queues_plain_failure_for_pre_rollback_gateway(tmp_path):
    beacon = tmp_path / watcher.BEACON_NAME
    (tmp_path / ".update_pending.json").write_text("{}", encoding="utf-8")
    watcher.publish_gateway_verdict(beacon, "rolled-back")
    assert (tmp_path / ".update_exit_code").read_text(encoding="utf-8") == "1"
    assert watcher.ROLLBACK_MESSAGE in (tmp_path / ".update_output.txt").read_text(encoding="utf-8")
    assert "Traceback" not in (tmp_path / ".update_output.txt").read_text(encoding="utf-8")


def test_watcher_does_not_create_reply_for_cli_only_update(tmp_path):
    watcher.publish_gateway_verdict(tmp_path / watcher.BEACON_NAME, "rolled-back")
    assert not (tmp_path / ".update_exit_code").exists()


@pytest.mark.asyncio
async def test_gateway_defers_success_reply_until_rollback_probe_finishes(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    watcher.write_beacon(["clover"], clover_home=tmp_path,
                         repo=str(tmp_path), pre_pull_sha="a" * 40)
    pending = tmp_path / ".update_pending.json"
    pending.write_text(json.dumps({"platform": "telegram", "chat_id": "original-chat"}),
                       encoding="utf-8")
    (tmp_path / ".update_exit_code").write_text("0", encoding="utf-8")
    from unittest.mock import AsyncMock
    runner = object.__new__(gateway_run.GatewayRunner)
    adapter = type("Adapter", (), {"send": AsyncMock()})()
    runner.adapters = {gateway_run.Platform.TELEGRAM: adapter}
    assert await runner._send_update_notification() is False
    assert pending.exists()
    adapter.send.assert_not_awaited()
    watcher.publish_gateway_verdict(tmp_path / watcher.BEACON_NAME, "rolled-back")
    (tmp_path / watcher.BEACON_NAME).unlink()
    assert await runner._send_update_notification() is True
    assert watcher.ROLLBACK_MESSAGE in adapter.send.await_args.args[1]
    assert "Traceback" not in adapter.send.await_args.args[1]


def test_cli_command_waits_for_rollback_verdict_and_exits_nonzero(monkeypatch):
    from types import SimpleNamespace
    from clover_cli import main, config, update_contract, update_lock
    monkeypatch.setattr(config, "is_managed", lambda: False)
    monkeypatch.setattr(update_contract, "evaluate_update_admission", lambda root: None)
    monkeypatch.setattr(main, "_install_hangup_protection", lambda **kw: None)
    monkeypatch.setattr(main, "_finalize_update_output", lambda state: None)
    monkeypatch.setattr(main, "_cmd_update_impl", lambda *a, **kw: None)
    monkeypatch.setattr(update_lock, "UpdateLock", lambda: SimpleNamespace(
        acquire=lambda: True, release=lambda: None))
    results = []
    monkeypatch.setattr(watcher, "wait_for_cli_verdict", lambda: results.append("wait") or False)
    with pytest.raises(SystemExit) as exc:
        main.cmd_update(SimpleNamespace(gateway=False, plan=False, check=False))
    assert exc.value.code == 1
    assert results == ["wait"]


def test_missing_managed_venv_is_recreated_before_dependency_repair(monkeypatch, tmp_path):
    calls = []
    def fake_run(args, **kw):
        calls.append(args)
        if args[:3] == ["/fake/uv", "venv", "venv"]:
            python = tmp_path / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("interpreter", encoding="utf-8")
        return type("Result", (), {"stdout": "", "returncode": 0})()
    monkeypatch.setattr(watcher.subprocess, "run", fake_run)
    monkeypatch.setattr(watcher.shutil, "which", lambda cmd: "/fake/uv" if cmd == "uv" else None)
    watcher._rollback_checkout({"repo": str(tmp_path), "pre_pull_sha": "a" * 40},
                               tmp_path / "beacon")
    assert ["/fake/uv", "venv", "venv"] in calls
    assert any("_install_python_dependencies_with_optional_fallback" in " ".join(cmd)
               for cmd in calls)


def test_watcher_reports_unexpected_recovery_error_instead_of_hanging(monkeypatch, tmp_path):
    beacon = watcher.write_beacon(["clover"], clover_home=tmp_path,
                                  repo=str(tmp_path), pre_pull_sha="a" * 40)
    monkeypatch.setattr(watcher, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(watcher.subprocess, "run", lambda *a, **kw: type("R", (),
                        {"returncode": 0, "stdout": "b" * 40})())
    monkeypatch.setattr(watcher, "verify_or_rollback", lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("internal failure")))
    assert watcher.watch(beacon, poll=0) == "rollback-failed"
    assert not beacon.exists()
    receipt = json.loads((tmp_path / "logs" / "update_receipts" / "latest.json")
                         .read_text(encoding="utf-8"))
    assert receipt["outcome"] == "rollback-failed"
    assert "internal failure" not in receipt["user_message"]


def test_probe_uses_updaters_external_venv_interpreter(monkeypatch, tmp_path):
    python = tmp_path / "external" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("interpreter", encoding="utf-8")
    calls = []
    monkeypatch.setattr(watcher.subprocess, "run", lambda args, **kw: calls.append(args) or
                        type("Result", (), {"returncode": 0})())
    monkeypatch.setattr(watcher, "_gateway_running", lambda: True)
    assert watcher.probe_gateway(tmp_path / "checkout", python=python,
                                 timeout=0, stable_seconds=0)
    assert any(args[0] == str(python) and "import clover_cli.main, gateway.run" in args
               for args in calls)


def test_windows_service_is_stopped_before_repairing_locked_venv(monkeypatch, tmp_path):
    from types import SimpleNamespace
    monkeypatch.setattr(watcher, "os", SimpleNamespace(name="nt"))
    python = tmp_path / "venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("interpreter", encoding="utf-8")
    calls = []
    def service_run(args, **kw):
        calls.append(args)
        return type("Result", (), {"returncode": 0,
                                    "stdout": "STATE : 1 STOPPED" if args[:2] == ["sc", "query"] else ""})()
    monkeypatch.setattr(watcher.subprocess, "run", service_run)
    watcher._rollback_checkout({"repo": str(tmp_path), "pre_pull_sha": "a" * 40,
                                "windows_services": ["clover-gateway"]}, tmp_path / "beacon")
    assert ["sc", "stop", "clover-gateway"] in calls
    assert calls.index(["sc", "stop", "clover-gateway"]) < next(
        i for i, cmd in enumerate(calls) if cmd[:3] == ["git", "reset", "--hard"])


def test_repair_failure_still_attempts_one_gateway_recovery(monkeypatch, tmp_path):
    actions = []
    monkeypatch.setattr(watcher, "probe_gateway", lambda *a, **kw: actions.append("probe") or
                        len(actions) >= 3)
    monkeypatch.setattr(watcher, "_rollback_checkout", lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("dependency repair failed")))
    monkeypatch.setattr(watcher, "_current_head", lambda root: "a" * 40)
    monkeypatch.setattr(watcher, "_restart_from_beacon", lambda data: actions.append("restart"))
    assert watcher.verify_or_rollback({"repo": str(tmp_path), "pre_pull_sha": "a" * 40,
                                       "gateway_argv": ["clover"]}, tmp_path / "beacon",
                                      timeout=0, stable_seconds=0) == "rolled-back"
    assert actions == ["probe", "restart", "probe"]
