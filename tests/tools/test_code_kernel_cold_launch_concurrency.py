"""Cold-launch concurrency: a retired kernel must never spawn a live runner.

Probe: caller A selects a cold kernel K1 and is paused before it acquires the
kernel's execution lock. Caller B, same owner, launches with reset=False or
reset=True, which retires K1 from the registry and replaces it with K2. When A
resumes it must not spawn a runner into K1 (which is no longer tracked), and
every spawned runner must remain registered in ``_KERNELS``.

All child processes are test-owned sleepers; finalizers kill and wait on them.
"""

import json
import subprocess
import sys
import threading
from contextlib import contextmanager

import pytest

from tools import code_kernel


@pytest.fixture
def isolated_kernels(monkeypatch):
    """Fresh registry and fixed lifecycle limits; children reaped at teardown."""
    monkeypatch.setattr(code_kernel, "_KERNELS", {})
    monkeypatch.setattr(code_kernel, "_IDLE_REAPED_KEYS", set())
    monkeypatch.setattr(code_kernel, "_lifecycle_limits", lambda: (4, 1800))
    children = []
    try:
        yield children
    finally:
        code_kernel.shutdown_all_kernels()
        for proc in children:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                pass
        for proc in children:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


def _install_fake_spawn(monkeypatch, children, spawned):
    """Replace runner launch with a test-owned sleeper that answers two cells."""

    def fake_spawn(kernel, *, task_id, child_python, child_cwd, sandbox_tools, max_tool_calls):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        children.append(proc)
        kernel.proc = proc
        spawned.append(kernel)
        for execution_count in (1, 2):
            kernel.response_q.put({"status": "ok", "stdout": "", "stderr": "",
                                   "execution_count": execution_count})

    monkeypatch.setattr(code_kernel, "_spawn", fake_spawn)


def _run(task_id, *, reset, is_interrupted=lambda: False):
    return json.loads(code_kernel.execute_in_session_kernel(
        "pass",
        task_id=task_id,
        mode="session",
        child_python=sys.executable,
        child_cwd="",
        sandbox_tools=frozenset(),
        timeout=0,
        max_tool_calls=1,
        reset=reset,
        is_interrupted=is_interrupted,
    ))


@pytest.mark.parametrize("reset", [False, True], ids=["reset_false", "reset_true"])
def test_retired_cold_kernel_does_not_spawn_untracked_runner(
    isolated_kernels, monkeypatch, reset
):
    children = isolated_kernels
    spawned = []
    _install_fake_spawn(monkeypatch, children, spawned)

    paused = threading.Event()
    resume = threading.Event()
    armed = [True]
    real_reserved_lock = code_kernel._reserved_cell_lock

    @contextmanager
    def pausing_reserved_lock(kernel):
        # Pause only the first caller: after it has selected a cold kernel and
        # before it takes that kernel's execution lock.
        if armed[0]:
            armed[0] = False
            paused.set()
            assert resume.wait(timeout=10), "probe resume never signalled"
        with real_reserved_lock(kernel):
            yield

    monkeypatch.setattr(code_kernel, "_reserved_cell_lock", pausing_reserved_lock)

    owner = f"cold-probe-owner-{reset}"
    results = {}
    errors = []

    def caller_a():
        try:
            results["a"] = _run(owner, reset=False)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread_a = threading.Thread(target=caller_a, daemon=True)
    thread_a.start()
    assert paused.wait(timeout=10), "first caller never reached the pause point"

    results["b"] = _run(owner, reset=reset)
    resume.set()
    thread_a.join(timeout=15)
    assert not thread_a.is_alive(), "first caller did not finish"
    assert not errors, errors

    assert results["b"]["status"] == "success"
    assert results["a"]["status"] == "success"

    untracked = [k for k in spawned if code_kernel._KERNELS.get(k.key) is not k]
    assert untracked == [], "runner spawned into a kernel no longer in _KERNELS"

    live_untracked = [
        proc for proc in children
        if proc.poll() is None
        and not any(k.proc is proc for k in code_kernel._KERNELS.values())
    ]
    assert live_untracked == [], "live runner outside _KERNELS"


@pytest.mark.parametrize(("deadline_seconds", "expected_attempts"), [(0.0, 1), (3.0, 3)])
def test_repeated_retirement_stops_after_bounded_reselection(
    isolated_kernels, monkeypatch, deadline_seconds, expected_attempts
):
    monkeypatch.setattr(code_kernel, "KERNEL_RESELECT_DEADLINE_SECONDS", deadline_seconds)
    children = isolated_kernels
    spawned = []
    _install_fake_spawn(monkeypatch, children, spawned)
    real_launch = code_kernel._launch_if_registered
    attempts = []

    def retire_repeatedly(key, kernel, **kwargs):
        attempts.append(kernel)
        if len(attempts) <= 3:
            with code_kernel._KERNELS_LOCK:
                if code_kernel._KERNELS.get(key) is kernel:
                    code_kernel._KERNELS.pop(key)
            return False
        return real_launch(key, kernel, **kwargs)

    monkeypatch.setattr(code_kernel, "_launch_if_registered", retire_repeatedly)
    result = _run("repeat-retirement-owner", reset=False)

    assert len(attempts) == expected_attempts, "reselection exceeded its attempt/deadline bound"
    assert result["status"] == "error"
    assert result["state_reset"] is True
    assert "did not run" in result["stderr"]
    assert spawned == [], "fail-closed retirement must not start a runner"



def test_stale_cleanup_never_unregisters_replacement(isolated_kernels, monkeypatch):
    """Retired callers' cleanup must not remove the kernel that replaced them."""
    key = ("stale-owner", "session", sys.executable, "", ())
    stale = code_kernel.SessionKernel(key)
    replacement = code_kernel.SessionKernel(key)
    code_kernel._KERNELS[key] = replacement

    assert code_kernel._remove_kernel_if_current(key, stale) is False
    assert code_kernel._KERNELS.get(key) is replacement
    assert code_kernel._remove_kernel_if_current(key, replacement) is True
    assert key not in code_kernel._KERNELS
