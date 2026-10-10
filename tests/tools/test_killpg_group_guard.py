"""Kill paths must signal a process group only when the child leads it.

``os.killpg(os.getpgid(child.pid), sig)`` is unsafe when the child did NOT
start in its own session: its pgid is then the caller's own group, and the
group signal takes the caller (Clover) down with it.  The safe rule is
``pgid == pid`` (the same check ``agent/deadline.py`` uses); otherwise signal
the direct child only.

Each test runs the real Clover kill path inside a harness subprocess that is
its own session leader, so the harness is the only member of its group.  The
child under test is spawned WITHOUT a new session and therefore shares the
harness's group.  If the kill path signals the group, the harness dies and
never prints ``SURVIVED``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_PRELUDE = textwrap.dedent(
    """
    import os, subprocess, sys
    sys.path.insert(0, {repo!r})

    _RealPopen = subprocess.Popen

    class _SharedGroupPopen(_RealPopen):
        # Force every child the code under test spawns into OUR process group.
        def __init__(self, *args, **kwargs):
            kwargs["start_new_session"] = False
            kwargs.pop("process_group", None)
            super().__init__(*args, **kwargs)

    subprocess.Popen = _SharedGroupPopen

    # Child that shares the harness's process group (pgid == harness pid,
    # child pid != pgid).  The code under test must not killpg it.
    child = _RealPopen(["sleep", "30"])
    assert os.getpgid(child.pid) == os.getpgid(0)
    assert os.getpgid(child.pid) != child.pid
    """
)

_EPILOGUE = textwrap.dedent(
    """
    print("SURVIVED", flush=True)
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
    print("CHILD_RC", child.returncode, flush=True)
    """
)


def _run_site(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    script = _PRELUDE.format(repo=str(REPO_ROOT)) + textwrap.dedent(body) + _EPILOGUE
    env = dict(os.environ)
    env["CLOVER_HOME"] = str(tmp_path / ".clover")
    # start_new_session=True makes the harness its own group leader, so a
    # buggy killpg can only ever hit the harness's group, never pytest's.
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        start_new_session=True,
    )


def _assert_harness_survived(result: subprocess.CompletedProcess) -> None:
    assert "SURVIVED" in result.stdout, (
        "Clover kill path signalled the process group shared with the caller "
        f"and took the caller down.\nrc={result.returncode}\n"
        f"stdout={result.stdout[-800:]}\nstderr={result.stderr[-800:]}"
    )
    # The shared child must actually be signalled (not orphaned): killed by a signal.
    rc_line = [ln for ln in result.stdout.splitlines() if ln.startswith("CHILD_RC")]
    assert rc_line, result.stdout
    assert int(rc_line[-1].split()[1]) < 0, rc_line[-1]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_process_registry_spawn_failure_does_not_killpg_shared_group(tmp_path):
    body = """
    import threading
    from tools.process_registry import ProcessRegistry

    def _boom(self, *a, **k):
        raise RuntimeError("forced post-Popen failure")
    threading.Thread.start = _boom  # reader thread start fails after Popen

    try:
        ProcessRegistry().spawn_local("sleep 30", use_pty=False)
    except RuntimeError:
        pass
    """
    _assert_harness_survived(_run_site(tmp_path, body))


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_verify_runner_terminate_does_not_killpg_shared_group(tmp_path):
    body = """
    from agent.verify.runner import _terminate_process_group
    _terminate_process_group(child)
    """
    _assert_harness_survived(_run_site(tmp_path, body))


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_browser_kill_process_tree_does_not_killpg_shared_group(tmp_path):
    body = """
    # Force the legacy fallback: agent.deadline (the already-safe delegate)
    # raises ImportError, so _kill_process_tree runs _legacy_kill_process_tree.
    sys.modules["agent.deadline"] = None
    from tools.browser_tool import _kill_process_tree
    _kill_process_tree(child)
    """
    _assert_harness_survived(_run_site(tmp_path, body))


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_cron_script_terminate_does_not_killpg_shared_group(tmp_path):
    body = """
    from cron.scheduler import _terminate_cron_script_process
    _terminate_cron_script_process(child)
    """
    _assert_harness_survived(_run_site(tmp_path, body))


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_secret_command_timeout_does_not_killpg_shared_group(tmp_path):
    body = """
    from agent.secret_sources.command import _run_helper
    # Helper runs under the shared-group Popen; the 0.5s timeout forces the
    # hard-kill branch at command.py:223.
    _run_helper("sleep 30", "K", 0.5, 1024)
    """
    _assert_harness_survived(_run_site(tmp_path, body))


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_photon_sidecar_stop_does_not_killpg_shared_group(tmp_path):
    body = """
    import asyncio
    from plugins.platforms.photon.adapter import PhotonAdapter

    adapter = PhotonAdapter.__new__(PhotonAdapter)
    adapter._sidecar_proc = child
    adapter._sidecar_supervisor_task = None  # real attr read in the finally block
    adapter._inbound_running = False
    adapter._http_client = None  # skip polite HTTP shutdown -> wait 3s -> kill path
    adapter._sidecar_bind = "127.0.0.1"
    adapter._sidecar_port = 1
    adapter._sidecar_token = "test-token"
    asyncio.run(adapter._stop_sidecar())
    """
    _assert_harness_survived(_run_site(tmp_path, body))


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_pty_bridge_close_does_not_killpg_shared_group(tmp_path):
    body = """
    from clover_cli.pty_bridge import PtyBridge

    class _FakePtyProc:
        def __init__(self, proc):
            self._proc = proc
            self.pid = proc.pid
        def isalive(self):
            return self._proc.poll() is None
        def kill(self, sig):
            self._proc.send_signal(sig)
        def close(self, force=False):
            pass

    bridge = PtyBridge.__new__(PtyBridge)
    bridge._closed = False
    bridge._proc = _FakePtyProc(child)
    bridge.close()
    """
    _assert_harness_survived(_run_site(tmp_path, body))
