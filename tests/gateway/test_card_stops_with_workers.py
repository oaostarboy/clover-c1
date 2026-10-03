"""The subagent card stops with its workers, whatever happens to delivery."""

from __future__ import annotations

import pytest

import gateway.delegation_activity as da
from tests.gateway.test_delegation_activity import (
    FakeClock,
    _child_cb,
    _combined_pub,
    _turn_runner,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    da._LIVE.clear(); da._LAST_OUT.clear(); da._FOLLOW_TIMERS.clear(); da._BOARDS.clear()
    da._INBOX.clear()
    yield
    for h in list(da._FOLLOW_TIMERS.values()):
        h.cancel()
    da._LIVE.clear(); da._LAST_OUT.clear(); da._FOLLOW_TIMERS.clear(); da._BOARDS.clear()
    da._INBOX.clear()


def _board_text(pub, clock):
    board = da._board_for(pub)
    board.clock = clock
    return board


async def _three_workers(monkeypatch, clock):
    pub, adapter = _combined_pub(monkeypatch)
    pub._clock = clock
    pub.tracker._clock = clock
    cbs = [
        _child_cb(_turn_runner(pub), index=i, count=3, subagent_id=f"sa-{i}", title=f"Task {i}")
        for i in range(3)
    ]
    for cb in cbs:
        cb("subagent.start", preview="g")
    await pub.drain()
    pub.end_turn()  # the parent already answered
    return pub, adapter, cbs


@pytest.mark.asyncio
async def test_card_stops_when_workers_are_interrupted_by_new(monkeypatch):
    """``/new`` interrupts the workers; delivery never completes. The card must
    show the final time and stay put instead of counting on."""
    clock = FakeClock()
    pub, _adapter, cbs = await _three_workers(monkeypatch, clock)
    clock.advance(8)
    for cb in cbs:
        cb("subagent.complete", status="interrupted", summary="Operation interrupted",
           duration_seconds=8)
    await pub.drain()
    board = _board_text(pub, clock)

    stopped = board.render()
    clock.advance(40)
    later = board.render()

    assert stopped == later, "the card keeps ticking after its workers stopped"
    assert "⏱ 8s" in stopped
    assert "stopped" in stopped.splitlines()[0]
    await pub.aclose()


@pytest.mark.asyncio
async def test_card_still_ticks_while_a_worker_runs(monkeypatch):
    clock = FakeClock()
    pub, _adapter, cbs = await _three_workers(monkeypatch, clock)
    clock.advance(8)
    for cb in cbs[:2]:
        cb("subagent.complete", status="interrupted", summary="x", duration_seconds=8)
    await pub.drain()
    board = _board_text(pub, clock)

    first = board.render()
    clock.advance(40)

    assert board.render() != first, "a running worker must keep the clock moving"
    assert "stopped" not in board.render().splitlines()[0]
    await pub.aclose()


@pytest.mark.asyncio
async def test_single_cancelled_worker_card_stops_too(monkeypatch):
    clock = FakeClock()
    pub, _adapter = _combined_pub(monkeypatch)
    pub._clock = clock
    pub.tracker._clock = clock
    cb = _child_cb(_turn_runner(pub))
    cb("subagent.start", preview="g")
    await pub.drain()
    pub.end_turn()
    clock.advance(5)
    cb("subagent.complete", status="interrupted", summary="x", duration_seconds=5)
    await pub.drain()
    board = _board_text(pub, clock)

    stopped = board.render()
    clock.advance(60)

    assert board.render() == stopped
    assert "⏱ 5s" in stopped and "stopped" in stopped
    await pub.aclose()
