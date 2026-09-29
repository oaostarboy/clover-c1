"""Exactly one gateway after a Windows update (no double relaunch).

Emilio (Windows 11, 2026-09-29, C1 at c7ec2d0): ``clover update`` succeeded and
logged "Restarting Windows gateway profile(s): default". The restarted gateway
connected to Telegram, then 8 s later received a SECOND planned stop ("Gateway
restart requested") from another process. The follow-up spawns never became a
gateway: Telegram down, ``gateway_state.json`` stuck at "stopped /
restart_requested".

The sequence: the clover.exe parent paused the gateway, pulled, then handed
the dependency sync to a venv child (``_reexec_dependency_sync_off_windows_shim``)
and relaunched the gateway itself on the way out. The child re-ran the update,
found that fresh gateway running, and paused it over the control socket.

Contract pinned here: ONE owner per post-update relaunch.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from clover_cli import update_restart_watcher as urw

AUTO_ENV = "CLOVER_GATEWAY_AUTORELAUNCH"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / ".clover"
    h.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(h))
    monkeypatch.delenv("CLOVER_UPDATE_REEXEC", raising=False)
    monkeypatch.delenv(AUTO_ENV, raising=False)
    return h


def _paused_token():
    return {
        "resume_needed": True,
        "profiles": {"default": 34128},
        "unmapped_pids": [],
        "unmapped": [],
    }


class _GatewayWorld:
    """A fake Windows box: which gateway PIDs run, and who started/stopped them."""

    def __init__(self):
        self.running: list[int] = []
        self.starts: list[str] = []
        self.pauses: list[tuple[str, int]] = []
        self._next = 5000

    def start(self, who: str) -> int:
        self._next += 1
        self.running.append(self._next)
        self.starts.append(who)
        return self._next

    def pause_all(self, who: str) -> dict | None:
        """What the update's pause does to whatever gateway is running."""
        if not self.running:
            return None
        token = {"resume_needed": True, "profiles": {"default": self.running[0]},
                 "unmapped_pids": [], "unmapped": []}
        for pid in self.running:
            self.pauses.append((who, pid))
        self.running = []
        return token


def test_shim_handoff_relaunches_exactly_once_and_never_pauses_the_fresh_gateway(home, monkeypatch):
    """Parent (clover.exe) + hand-off child: one start, no pause of the fresh PID."""
    from clover_cli import main, update_cmd

    world = _GatewayWorld()

    def fake_resume(token):
        if token and token.get("resume_needed"):
            world.start("resume")
            token["resume_needed"] = False

    monkeypatch.setattr(main, "_resume_windows_gateways_after_update", fake_resume)
    monkeypatch.setattr(main, "_detect_self_loaded_native_modules", lambda: [])
    monkeypatch.setattr(main, "_reexec_dependency_sync_off_windows_shim", lambda **_: True)

    # --- parent: paused the original gateway, pulled, hands the sync off.
    world.running = [34128]
    parent_token = world.pause_all("parent")
    with pytest.raises(SystemExit) as exc:
        update_cmd._abort_dependency_sync_if_self_locked(parent_token)
    assert exc.value.code == 0
    # The parent's atexit resume runs on the way out too.
    fake_resume(parent_token)
    parent_started = list(world.starts)

    # --- child: re-runs the update flow.
    monkeypatch.setenv("CLOVER_UPDATE_REEXEC", "1")
    fresh = list(world.running)
    child_token = world.pause_all("child")  # _pause_windows_gateways_for_update
    adopt = getattr(main, "_adopt_handed_off_gateway_resume", lambda t: (t, False))
    child_token, adopted = adopt(child_token)
    fake_resume(child_token)

    assert parent_started == [], "the hand-off parent must not relaunch"
    assert fresh == [], "nothing may be running for the child to pause"
    assert [p for p in world.pauses if p[0] == "child"] == []
    assert adopted is True
    assert world.starts == ["resume"], "exactly one post-update start"
    assert len(world.running) == 1


def test_handoff_falls_back_to_resume_when_token_cannot_be_saved(home, monkeypatch):
    from clover_cli import main, update_cmd

    calls = []
    monkeypatch.setattr(main, "_resume_windows_gateways_after_update", lambda t: calls.append(t))
    monkeypatch.setattr(main, "_detect_self_loaded_native_modules", lambda: [])
    monkeypatch.setattr(main, "_reexec_dependency_sync_off_windows_shim", lambda **_: True)
    monkeypatch.setattr(main, "_hand_off_windows_gateway_resume", lambda t: False)
    with pytest.raises(SystemExit):
        update_cmd._abort_dependency_sync_if_self_locked(_paused_token())
    assert len(calls) == 1, "a gateway must never be left down"


def test_child_without_handoff_keeps_its_own_token(home, monkeypatch):
    from clover_cli import main

    monkeypatch.setenv("CLOVER_UPDATE_REEXEC", "1")
    token = {"resume_needed": True, "profiles": {}, "unmapped": [], "cold_start_if_installed": True}
    assert main._adopt_handed_off_gateway_resume(token) == (token, False)


def test_parent_beacon_follows_the_child_and_parent_exit_keeps_it(home):
    urw.write_beacon(["python", "-m", "clover_cli.main", "gateway", "run"])
    assert urw.hand_off_beacon(424242) is True
    urw.clear_beacon()  # the parent's atexit
    data = json.loads(urw.beacon_path().read_text(encoding="utf-8"))
    assert data["updater_pid"] == 424242


def _stale_beacon(home: Path) -> Path:
    beacon = urw.write_beacon([sys.executable, "-m", "clover_cli.main", "gateway", "run"])
    data = json.loads(beacon.read_text(encoding="utf-8"))
    data["updater_pid"] = 0  # updater died
    data["refreshed_at"] = time.time() - 10 * urw.BEACON_STALE_SECONDS
    beacon.write_text(json.dumps(data), encoding="utf-8")
    return beacon


def test_watcher_stands_down_while_the_updater_relaunch_is_starting(home, monkeypatch):
    """Updater relaunched (claim written), gateway still importing: no 2nd start."""
    beacon = _stale_beacon(home)
    urw.write_relaunch_marker(["default"])
    polls = {"n": 0}

    def gateway_running():
        polls["n"] += 1
        return polls["n"] > 4  # the relaunched gateway shows up a bit later

    spawned = []
    monkeypatch.setattr(urw, "_gateway_running", gateway_running)
    monkeypatch.setattr(urw.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    assert urw.watch(beacon, poll=0.01) == "gateway-healthy"
    assert spawned == []


def test_watcher_still_relaunches_when_updater_died_and_nothing_is_up(home, monkeypatch):
    beacon = _stale_beacon(home)
    spawned = []
    monkeypatch.setattr(urw, "_gateway_running", lambda: False)
    monkeypatch.setattr(urw.subprocess, "Popen", lambda argv, **k: spawned.append((argv, k)))
    assert urw.watch(beacon, poll=0.01) == "restarted"
    assert len(spawned) == 1
    assert spawned[0][1]["env"][AUTO_ENV] == "1"


def test_watcher_relaunches_once_the_relaunch_claim_expires(home, monkeypatch):
    beacon = _stale_beacon(home)
    urw.write_relaunch_marker(["default"])
    marker = urw.relaunch_marker_path()
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["at"] = time.time() - urw.RELAUNCH_GRACE_SECONDS - 5
    marker.write_text(json.dumps(data), encoding="utf-8")
    spawned = []
    monkeypatch.setattr(urw, "_gateway_running", lambda: False)
    monkeypatch.setattr(urw.subprocess, "Popen", lambda argv, **k: spawned.append(argv))
    assert urw.watch(beacon, poll=0.01) == "restarted"
    assert len(spawned) == 1


def test_fresh_gateway_detection_uses_the_relaunch_claim(home):
    urw.write_relaunch_marker(["default"])
    time.sleep(0.05)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        assert urw.is_fresh_post_update_gateway(child.pid) is True
        # A process that predates the claim is the pre-update gateway shape.
        if os.name != "nt":
            assert urw.is_fresh_post_update_gateway(1) is False
    finally:
        child.kill()
        child.wait()


def test_resume_writes_the_relaunch_claim(home, monkeypatch):
    from clover_cli import main, update_cmd

    monkeypatch.setattr(main, "_is_windows", lambda: True)
    monkeypatch.setattr(main, "_refresh_windows_gateway_launchers", lambda: None)
    import clover_cli.gateway as gw

    monkeypatch.setattr(gw, "launch_detached_profile_gateway_restart", lambda p, pid: True)
    update_cmd._resume_windows_gateways_after_update(_paused_token())
    assert urw.recent_relaunch() is not None


def _run_replace(monkeypatch, *, fresh: bool, auto: bool):
    from gateway import run as gateway_run

    calls = {"terminate": 0, "marker": 0}
    if auto:
        monkeypatch.setenv(AUTO_ENV, "1")
    with (
        patch("gateway.status.get_running_pid", return_value=34128),
        patch.object(gateway_run, "_is_fresh_post_update_gateway", return_value=fresh),
        patch.object(gateway_run, "_replace_target_belongs_to_other_profile", return_value=True),
        patch("gateway.status.terminate_pid",
              side_effect=lambda *a, **k: calls.__setitem__("terminate", calls["terminate"] + 1)),
        patch("gateway.status.write_takeover_marker",
              side_effect=lambda *a, **k: calls.__setitem__("marker", calls["marker"] + 1)),
    ):
        result = asyncio.run(gateway_run.start_gateway(replace=True))
    return result, calls


def test_auto_replace_never_takes_over_the_fresh_post_update_gateway(home, monkeypatch):
    result, calls = _run_replace(monkeypatch, fresh=True, auto=True)
    assert result is True  # stood down cleanly: no supervisor retry
    assert calls == {"terminate": 0, "marker": 0}
    assert AUTO_ENV not in os.environ


def test_human_replace_is_not_blocked_by_the_relaunch_claim(home, monkeypatch):
    # Reaches the existing ownership gate (mocked to refuse) instead of the
    # stand-down: a person's explicit --replace keeps its old behaviour.
    result, _ = _run_replace(monkeypatch, fresh=True, auto=False)
    assert result is False


def test_clean_start_clears_a_stale_restart_requested(home):
    from gateway import status

    status.write_runtime_status(gateway_state="stopped", restart_requested=True,
                                exit_reason="Gateway restart requested")
    status.record_gateway_starting()
    data = json.loads((home / "gateway_state.json").read_text(encoding="utf-8"))
    assert data["restart_requested"] is False
    assert data["gateway_state"] == "starting"


def test_runner_startup_uses_the_clearing_start_record():
    import inspect

    from gateway import run as gateway_run

    assert "record_gateway_starting()" in inspect.getsource(gateway_run.GatewayRunner)


def test_captured_gateway_argv_is_replayed_on_the_current_interpreter(monkeypatch):
    """A captured interpreter can vanish when the update rebuilds the runtime."""
    from clover_cli import update_cmd
    import clover_cli.gateway as gw

    monkeypatch.setattr(gw, "get_python_path", lambda: r"C:\clover\venv\Scripts\python.exe")
    old = [r"C:\old-runtime\python.exe", "-m", "clover_cli.main", "gateway", "run", "--replace"]
    assert update_cmd._with_current_gateway_interpreter(old) == [
        r"C:\clover\venv\Scripts\python.exe", "-m", "clover_cli.main", "gateway", "run", "--replace"]
    other = ["pythonw.exe", "some_script.py"]
    assert update_cmd._with_current_gateway_interpreter(other) == other


def _fake_windows(monkeypatch, *, running_pid):
    from clover_cli import main, update_cmd
    import gateway.status as gs

    monkeypatch.setattr(gs, "get_running_pid", lambda *a, **k: running_pid)
    cold = []
    monkeypatch.setattr(main, "_cold_start_windows_gateway_after_update",
                        lambda: cold.append(1) or True)
    monkeypatch.setattr(update_cmd.sys, "platform", "win32")
    return cold


def test_relaunch_that_never_comes_up_falls_back_to_a_verified_start(monkeypatch):
    """Windows runner, 69a75bb -> fix: the replayed relaunch died silently, no gateway."""
    from clover_cli import update_cmd

    cold = _fake_windows(monkeypatch, running_pid=None)
    assert update_cmd._verify_windows_gateway_relaunch(timeout=0) is True
    assert cold == [1]


def test_relaunch_that_came_up_is_not_started_again(monkeypatch):
    from clover_cli import update_cmd

    cold = _fake_windows(monkeypatch, running_pid=34128)
    assert update_cmd._verify_windows_gateway_relaunch(timeout=0) is True
    assert cold == []


def test_resume_verifies_its_relaunch(home, monkeypatch):
    from clover_cli import main, update_cmd
    import clover_cli.gateway as gw

    monkeypatch.setattr(main, "_is_windows", lambda: True)
    monkeypatch.setattr(main, "_refresh_windows_gateway_launchers", lambda: None)
    monkeypatch.setattr(gw, "launch_detached_profile_gateway_restart", lambda p, pid: True)
    verified = []
    monkeypatch.setattr(main, "_verify_windows_gateway_relaunch",
                        lambda **k: verified.append(k) or True)
    update_cmd._resume_windows_gateways_after_update(_paused_token())
    assert verified == [{"current_profile": True}]
