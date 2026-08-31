"""The post-update restart must survive the updater being killed.

Reported twice in one day on a Windows install: ``clover update`` stopped the
gateway, then vanished. No gateway, no updater, no supervisor, no alert. The
assistant went silent until a human noticed, both times.

The restart was registered with ``atexit``. Measured behaviour of atexit:

    updater finishes normally      handler runs
    updater raises an exception    handler runs
    updater killed with SIGTERM    handler SKIPPED
    updater killed with SIGKILL    handler SKIPPED
    updater hard-exits             handler SKIPPED

So the recovery could not run in precisely the cases that needed it. These
tests drive the watcher through those cases, using real killed processes
rather than a simulated "the updater has died" flag, because simulating the
death is what would hide the bug.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from clover_cli import update_restart_watcher as urw


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    return tmp_path


def _beacon(home: Path, *, pid: int, argv: list[str], age: float = 0.0) -> Path:
    p = home / urw.BEACON_NAME
    p.write_text(json.dumps({
        "updater_pid": pid,
        "refreshed_at": time.time() - age,
        "gateway_argv": argv,
        "cwd": str(home),
    }), encoding="utf-8")
    return p


class TestTheDeathsThatDefeatedAtexit:
    """A killed updater must still result in a running gateway."""

    @pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
    def test_killed_updater_still_gets_the_gateway_restarted(
        self, home, monkeypatch, tmp_path, sig
    ):
        # A real process, really killed. Not a flag that says "pretend it died".
        victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        try:
            proof = tmp_path / "restarted.txt"
            argv = [sys.executable, "-c", f"open({str(proof)!r}, 'w').write('up')"]
            beacon = _beacon(home, pid=victim.pid, argv=argv)

            monkeypatch.setattr(urw, "_gateway_running", lambda: False)

            victim.send_signal(sig)
            victim.wait(timeout=10)

            assert urw.watch(beacon, poll=0.05) == "restarted"

            for _ in range(50):
                if proof.exists():
                    break
                time.sleep(0.1)
            assert proof.exists(), "the watcher reported a restart but none happened"
        finally:
            if victim.poll() is None:
                victim.kill()

    def test_atexit_would_not_have_run_for_those_signals(self, tmp_path):
        """Document the defect this module exists for, by measuring it."""
        marker = tmp_path / "atexit_ran.txt"
        child = tmp_path / "child.py"
        child.write_text(
            "import atexit, os, signal, sys, time\n"
            f"atexit.register(lambda: open({str(marker)!r}, 'w').write('ran'))\n"
            "os.kill(os.getpid(), signal.SIGKILL)\n"
            "time.sleep(5)\n"
        )
        subprocess.run([sys.executable, str(child)], capture_output=True, timeout=20)
        time.sleep(0.2)
        assert not marker.exists(), (
            "atexit ran under SIGKILL: the premise of this module changed"
        )


class TestItDoesNotStartASecondGateway:
    """Restarting a healthy gateway would be its own outage."""

    def test_healthy_gateway_is_left_alone(self, home, monkeypatch, tmp_path):
        proof = tmp_path / "should_not_exist.txt"
        argv = [sys.executable, "-c", f"open({str(proof)!r}, 'w').write('x')"]
        beacon = _beacon(home, pid=999_999, argv=argv, age=urw.BEACON_STALE_SECONDS + 60)

        monkeypatch.setattr(urw, "_gateway_running", lambda: True)

        assert urw.watch(beacon, poll=0.05) == "gateway-healthy"
        time.sleep(0.3)
        assert not proof.exists(), "started a gateway while one was already running"

    def test_unknown_liveness_is_treated_as_alive(self, monkeypatch):
        """A probe that cannot answer must never be read as 'dead'."""
        def boom(_pid):
            raise OSError("cannot query this process")
        monkeypatch.setattr(urw.os, "kill", boom)
        assert urw._pid_alive(os.getpid()) is True


class TestTheHappyPath:
    def test_cleared_beacon_stands_the_watcher_down(self, home):
        beacon = _beacon(home, pid=os.getpid(), argv=["/bin/true"])
        beacon.unlink()
        assert urw.watch(beacon, poll=0.05) == "update-finished"

    def test_live_updater_keeps_the_watcher_waiting(self, home, monkeypatch):
        beacon = _beacon(home, pid=os.getpid(), argv=["/bin/true"])
        monkeypatch.setattr(urw, "WATCHER_MAX_LIFETIME_SECONDS", 0.4)
        # The updater (this test process) is alive and the beacon is fresh, so
        # the watcher should wait rather than act, and hit its ceiling.
        assert urw.watch(beacon, poll=0.05) == "expired"

    def test_refresh_keeps_the_beacon_fresh(self, home):
        urw.write_beacon(["/bin/true"], clover_home=home)
        p = urw.beacon_path(home)
        first = json.loads(p.read_text())["refreshed_at"]
        time.sleep(0.05)
        urw.refresh_beacon(clover_home=home)
        second = json.loads(p.read_text())["refreshed_at"]
        assert second > first

    def test_clear_is_safe_when_already_gone(self, home):
        urw.clear_beacon(clover_home=home)  # must not raise
        urw.write_beacon(["/bin/true"], clover_home=home)
        urw.clear_beacon(clover_home=home)
        assert not urw.beacon_path(home).exists()


class TestDegradedInputs:
    def test_no_restart_command_reports_rather_than_guessing(self, home, monkeypatch):
        beacon = _beacon(home, pid=999_999, argv=[], age=urw.BEACON_STALE_SECONDS + 60)
        monkeypatch.setattr(urw, "_gateway_running", lambda: False)
        assert urw.watch(beacon, poll=0.05) == "no-argv"

    def test_corrupt_beacon_does_not_crash_the_watcher(self, home, monkeypatch):
        beacon = home / urw.BEACON_NAME
        beacon.write_text("{ this is not json", encoding="utf-8")
        monkeypatch.setattr(urw, "WATCHER_MAX_LIFETIME_SECONDS", 0.3)
        assert urw.watch(beacon, poll=0.05) == "expired"

    def test_stale_beacon_acts_even_if_the_pid_was_recycled(self, home, monkeypatch, tmp_path):
        """A live PID plus a long-stale beacon still means the update is gone."""
        proof = tmp_path / "restarted.txt"
        argv = [sys.executable, "-c", f"open({str(proof)!r}, 'w').write('up')"]
        # This process is alive, so liveness alone would say "keep waiting".
        beacon = _beacon(home, pid=os.getpid(), argv=argv,
                         age=urw.BEACON_STALE_SECONDS + 120)
        monkeypatch.setattr(urw, "_gateway_running", lambda: False)
        assert urw.watch(beacon, poll=0.05) == "restarted"
