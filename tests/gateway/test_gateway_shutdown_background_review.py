"""Gateway shutdown must cancel in-flight background reviews and never wait
around them past a bounded grace.

Regression coverage for the restart-stall incident (2026-09-28, macOS/Suni):
``runner.stop()`` logged "Gateway stopped" while a background self-improvement
review kept running unsupervised for ~5 more minutes before the process
finally exited. These tests exercise the real ``GatewayRunner.stop()`` path
and must FAIL on unmodified ``origin/main`` (nothing there cancels
background reviews during shutdown).
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

import agent.background_review as bg
from tests.gateway.restart_test_helpers import make_restart_runner


class _FakeReviewAgent:
    """Minimal stand-in for the forked AIAgent a background review runs."""

    def __init__(self):
        self.interrupt_calls: list = []

    def interrupt(self, message=None, **kwargs):
        self.interrupt_calls.append(message)


class _BareReviewOwner:
    """Minimal stand-in for the parent agent that spawned the review —
    only the attributes prepare_background_review_run touches are needed."""

    def __init__(self):
        self._background_review_lock = threading.Lock()
        self._background_review_run = None


@pytest.mark.asyncio
async def test_gateway_stop_cancels_in_flight_background_review():
    """runner.stop() must request cancellation of a live background review
    that was admitted (mid provider-call phase) before shutdown began."""
    runner, _adapter = make_restart_runner()
    runner._restart_drain_timeout = 0.0

    owner = _BareReviewOwner()
    run = bg.prepare_background_review_run(owner)
    assert run is not None
    review_agent = _FakeReviewAgent()
    assert run.begin_request(review_agent) is True
    assert bg.live_background_review_count() == 1

    try:
        with (
            patch("gateway.status.remove_pid_file"),
            patch("gateway.status.write_runtime_status"),
            patch("agent.auxiliary_client.shutdown_cached_clients"),
        ):
            await runner.stop()

        # The interrupt thread background_review.py spawns is fire-and-forget
        # (daemon, off-thread) — give it a moment to land.
        deadline = time.monotonic() + 2.0
        while not review_agent.interrupt_calls and time.monotonic() < deadline:
            time.sleep(0.01)

        assert review_agent.interrupt_calls, (
            "gateway shutdown never cancelled the in-flight background review"
        )
    finally:
        # This review never acknowledges (we never call
        # finish_background_review_run) — clean the registry so it doesn't
        # leak into other tests in the same process.
        bg.finish_background_review_run(owner, run)


@pytest.mark.asyncio
async def test_gateway_stop_does_not_wait_out_a_wedged_review():
    """A review that never acknowledges cancellation must not delay
    runner.stop() past the bounded cancellation grace."""
    runner, _adapter = make_restart_runner()
    runner._restart_drain_timeout = 0.0

    owner = _BareReviewOwner()
    run = bg.prepare_background_review_run(owner)
    review_agent = _FakeReviewAgent()
    run.begin_request(review_agent)

    try:
        started = time.monotonic()
        with (
            patch("gateway.status.remove_pid_file"),
            patch("gateway.status.write_runtime_status"),
            patch("agent.auxiliary_client.shutdown_cached_clients"),
        ):
            await runner.stop()
        elapsed = time.monotonic() - started

        # Bounded to the cancellation grace (default a few seconds), not an
        # unbounded/minutes-long wait for a review that never acknowledges.
        assert elapsed < 5.0
    finally:
        bg.finish_background_review_run(owner, run)
