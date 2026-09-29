"""Regression coverage for the restart-stall incident (2026-09-28).

``gateway.run.main()`` used to hand loop teardown to ``asyncio.run()``,
which — on ``start_gateway`` returning — calls ``asyncio.Runner.close()``
and awaits ``loop.shutdown_default_executor(constants.THREAD_JOIN_TIMEOUT)``.
``THREAD_JOIN_TIMEOUT`` is 300 seconds on Python 3.12+, so a single
default-executor thread still busy when ``start_gateway`` returned (e.g. a
stray context-compression LLM call that hadn't timed out yet) blocked
process exit for up to five minutes, even though ``start_gateway``'s own
graceful teardown had already completed and logged "Gateway stopped" — the
observed incident was exactly a 300s gap between that log line and the
process actually exiting.

This test drives the real ``main()`` with a fake ``start_gateway`` that
fires off a 30s job on the loop's default executor and returns immediately,
then asserts the exit path is reached in well under that job's duration.
It must FAIL (time out / exceed the 5s bound) on unmodified ``origin/main``.
"""

from __future__ import annotations

import asyncio
import time
import types
from unittest.mock import Mock

import pytest

import gateway.run as gateway_run


@pytest.mark.timeout(60)
def test_main_reaches_exit_path_despite_busy_default_executor_thread(monkeypatch):
    exit_calls: list[int] = []
    exit_times: list[float] = []

    def _record_exit(exit_code: int) -> None:
        exit_calls.append(exit_code)
        exit_times.append(time.monotonic())

    async def _fake_start_gateway(config=None):
        loop = asyncio.get_running_loop()
        # Fire-and-forget: mirrors a stray in-flight default-executor job
        # (e.g. auxiliary context-compression) that's still busy the moment
        # start_gateway's own graceful teardown finishes and it returns.
        loop.run_in_executor(None, time.sleep, 30)
        return True

    monkeypatch.setattr(gateway_run, "start_gateway", _fake_start_gateway)
    monkeypatch.setattr(gateway_run, "_exit_after_graceful_shutdown", _record_exit)
    monkeypatch.setattr(gateway_run.sys, "argv", ["gateway.run"])
    monkeypatch.setattr(gateway_run.sys, "stdout", types.SimpleNamespace(flush=Mock()))
    monkeypatch.setattr(gateway_run.sys, "stderr", types.SimpleNamespace(flush=Mock()))

    started = time.monotonic()
    gateway_run.main()
    elapsed = exit_times[0] - started if exit_times else float("inf")

    assert exit_calls == [0], (
        "_exit_after_graceful_shutdown must be reached exactly once, with "
        f"exit code 0 for a successful start_gateway; got {exit_calls!r}"
    )
    assert elapsed < 5.0, (
        f"main() took {elapsed:.1f}s to reach the exit path — a busy "
        "default-executor thread must never block process exit "
        "(restart-stall / THREAD_JOIN_TIMEOUT regression)"
    )
