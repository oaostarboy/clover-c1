"""The restart watcher must never spawn a real gateway during a test run.

WHAT THIS PREVENTS (observed, not hypothetical)
-----------------------------------------------
On 2026-08-30 a full ``pytest tests/clover_cli/`` run left NINE live
``python -m clover_cli.main gateway run --replace`` processes on the developer's
machine. Their ``/proc/<pid>/environ`` still carried ``PYTEST_CURRENT_TEST``,
which is how they were identified as test residue rather than real gateways.

Two properties of the spawn chain combine into the fault:

1. ``_spawn_gateway_restart_watcher`` detaches the child
   (``start_new_session=True`` on POSIX). A detached grandchild is NOT in
   pytest's process group, so nothing reaps it when the test session ends. It
   outlives the run indefinitely.

2. The spawn inherits ``os.environ``. A test that points ``CLOVER_HOME`` at a
   ``tmp_path`` is isolated only for Clover state. Every OTHER variable still
   leaks in from the developer's shell, and the spawned argv carries
   ``--replace`` — an instruction to terminate the gateway already running and
   take its place.

The result: a unit test reached out of its sandbox and killed the developer's
live gateway, then left nine orphans behind.

THE CONTRACT
------------
No test may reach a real ``subprocess.Popen`` in this chain. Any test that
exercises the restart path stubs the launcher. This test fails loudly if the
chain is ever refactored so the stubs no longer intercept it, which is exactly
the silent regression that produced the incident.
"""

from __future__ import annotations

import clover_cli.gateway as gateway


def test_watcher_rejects_a_missing_pid_before_any_spawn():
    """The cheap guards must reject bad input BEFORE reaching Popen.

    Both arguments are validated up front. If a refactor ever moves the spawn
    ahead of these guards, this call starts a real detached gateway and the
    test hangs or leaks, which is the signal we want.
    """
    assert gateway._spawn_gateway_restart_watcher(0, ["python", "-m", "x"]) is False
    assert gateway._spawn_gateway_restart_watcher(-1, ["python", "-m", "x"]) is False


def test_watcher_rejects_an_empty_argv_before_any_spawn():
    assert gateway._spawn_gateway_restart_watcher(12345, []) is False


def test_watcher_spawn_is_interceptable_by_monkeypatch(monkeypatch):
    """Stubbing ``subprocess.Popen`` in this module must stop the real spawn.

    This is the property every other test in the suite relies on. If the module
    is refactored to call Popen through another path (a local import, a helper
    in another module), the stub silently stops working and real gateways spawn
    again. Asserting it here makes that refactor fail a test instead of leaking
    processes onto a developer's machine.
    """
    calls: list[list[str]] = []

    class _FakeProc:
        pid = 999999

        def poll(self):
            return None

    def _fake_popen(argv, *a, **kw):
        calls.append(list(argv) if isinstance(argv, (list, tuple)) else [str(argv)])
        return _FakeProc()

    monkeypatch.setattr(gateway.subprocess, "Popen", _fake_popen)

    gateway._spawn_gateway_restart_watcher(12345, ["python", "-m", "clover_cli.main"])

    assert calls, (
        "subprocess.Popen in clover_cli.gateway was not the spawn path. "
        "The restart watcher now escapes monkeypatch interception, so tests "
        "exercising this chain will spawn REAL detached gateways that survive "
        "the pytest session. See this module's docstring."
    )


def test_spawned_watcher_argv_carries_replace_which_is_why_isolation_matters():
    """Document the blast radius: the respawn argv says ``--replace``.

    ``--replace`` tells the new gateway to terminate whichever gateway is
    already running and take over. That is correct in production and dangerous
    in a test, because the victim is whatever gateway the developer has live.
    Pinning it here keeps the danger visible to anyone editing this chain.
    """
    argv = gateway._gateway_run_command()

    assert "--replace" in argv
    assert "gateway" in argv and "run" in argv
