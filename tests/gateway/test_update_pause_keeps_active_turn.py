"""D5 (Emilio's Windows report): an update pause must not amputate a running turn.

The updater's socket ``pause-for-update`` deferred the restart until the
active turn finished, but the planned-stop marker it wrote a millisecond
earlier ran a plain ``stop()`` with a 0 s drain and interrupted the turn
anyway. The pause ACK also advertised only that 0 s drain, so the updater
force-killed the gateway 10 s later even when the deferral held.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.run import GatewayRunner
from tests.gateway.restart_test_helpers import make_restart_runner

SESSION = "agent:main:telegram:dm:123"


def _runner_with_active_turn(cap: float):
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()  # the restart's own stop(restart=True)
    runner._restart_after_turn_timeout = cap
    runner._restart_drain_timeout = 0.0  # the shipped default on Emilio's install
    runner._interrupt_running_agents = MagicMock()
    runner._running_agents[SESSION] = MagicMock()
    return runner


@pytest.mark.asyncio
async def test_active_turn_at_pause_time_survives_until_it_finishes():
    runner = _runner_with_active_turn(cap=30.0)

    # The socket pause and the planned-stop marker, in either order.
    assert runner.request_restart(detached=False, via_service=True) is True
    marker_stop = asyncio.create_task(GatewayRunner.stop(runner))

    await asyncio.sleep(0.3)
    runner._interrupt_running_agents.assert_not_called()
    assert not marker_stop.done(), "the plain stop() cut in ahead of the deferred restart"
    runner.stop.assert_not_awaited()
    assert SESSION in runner._running_agents

    del runner._running_agents[SESSION]  # the turn completes
    await asyncio.wait_for(marker_stop, timeout=5)
    runner._interrupt_running_agents.assert_not_called()
    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=False, service_restart=True
    )


@pytest.mark.asyncio
async def test_active_turn_is_only_interrupted_at_the_cap():
    runner = _runner_with_active_turn(cap=0.3)

    assert runner.request_restart(detached=False, via_service=True) is True
    marker_stop = asyncio.create_task(GatewayRunner.stop(runner))
    await asyncio.sleep(0.1)
    assert not marker_stop.done()

    await asyncio.wait_for(marker_stop, timeout=5)
    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=False, service_restart=True
    )


@pytest.mark.asyncio
async def test_update_marker_alone_defers_for_the_active_turn():
    """An updater that could not reach the socket still must not kill the turn."""
    runner = _runner_with_active_turn(cap=30.0)
    runner.request_restart = MagicMock(return_value=True)

    runner._handle_planned_stop("update")

    runner.request_restart.assert_called_once_with(detached=False, via_service=True)
    runner.stop.assert_not_awaited()


def test_pause_ack_budget_covers_the_after_turn_wait():
    runner = _runner_with_active_turn(cap=1800.0)
    budget = runner.update_pause_budget(0.0)
    assert budget["active_work"] == 1
    assert budget["drain_timeout"] >= 1800.0

    del runner._running_agents[SESSION]
    assert runner.update_pause_budget(0.0)["drain_timeout"] == 0.0
