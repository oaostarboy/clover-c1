"""Unified helper card for EVERY dispatch -- with or without a ``handoff``
summary.  The old 🔀 roster card must never appear while ``display.helper_card``
is unified (the default); classic keeps it."""

from __future__ import annotations

import asyncio

import pytest

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl
from agent.delegation_activity import bind_activity_sink
from agent.handoff_title import dispatch_work_title, task_title
from gateway import delegation_activity as da
from tests.gateway.test_delegation_activity import (
    FakeTelegramAdapter,
    _child_cb,
    _make_publisher,
    _turn_runner,
)

pytestmark = pytest.mark.unified_card

GOAL = "Count to 200 slowly. Use the terminal tool with sleep 2 between calls."


@pytest.fixture(autouse=True)
def _clean():
    ac.reset_for_tests()
    ac.set_style_for_tests("unified")
    da._ACK_EDITORS.clear()
    tok = bind_activity_sink(None)
    yield
    ac.reset_for_tests()
    da._ACK_EDITORS.clear()


class _Agent:
    pass


async def _settle(pub):
    for _ in range(20):
        await asyncio.sleep(0)
    await pub.drain()
    for _ in range(20):
        await asyncio.sleep(0)


def _dispatch(pub, did, goals, handoff=None, titles=(), task_steps=()):
    """What delegate_task does around a background dispatch."""
    bind_activity_sink(pub)
    fl.reserve_ack_part(did, handoff, goals)
    fl.note_own_dispatch(
        _Agent(), did, goals=goals, handoff=handoff, titles=titles, task_steps=task_steps,
    )


# -- titles -------------------------------------------------------------------


def test_task_title_uses_the_tasks_own_title_first():
    assert task_title(GOAL, "Count slowly") == "Count slowly"


def test_task_title_is_built_from_the_goal():
    assert task_title(GOAL) == "counting to 200 slowly"
    assert task_title(
        "You are building the DND Security website. Create the services page with Next.js."
    ).startswith("creating the services page")


def test_task_title_never_empty_never_raises():
    assert task_title("") and task_title(None)  # type: ignore[arg-type]


def test_work_title_one_task_or_n_subagents():
    assert dispatch_work_title([GOAL]) == "counting to 200 slowly"
    assert dispatch_work_title(["a b c", "d e f"]) == "2 subagents"
    assert dispatch_work_title(["a", "b", "c"]) == "3 subagents"


# -- no handoff summary -----------------------------------------------------------


@pytest.mark.asyncio
async def test_single_dispatch_without_a_summary_gets_a_unified_card_titled_from_the_goal():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    _dispatch(pub, "d-one", [GOAL])
    assert ac.owns_delegation("d-one"), "claimed before any helper event"
    await _settle(pub)
    assert len(adapter.sends) == 1 and adapter.status_calls == []
    text = adapter.sends[0]["content"]
    assert "counting to 200 slowly" in text
    assert "Goal:" not in text, "no outcome is known: no Goal line"
    part = ac._PARTS["d-one"]
    assert part.checklist.say == "", "no opener line is invented"
    await pub.aclose()


@pytest.mark.asyncio
async def test_single_dispatch_uses_the_tasks_title_and_steps():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    _dispatch(pub, "d-t", [GOAL], titles=["Count slowly"], task_steps=["start", "count", "report"])
    await _settle(pub)
    text = adapter.sends[0]["content"]
    assert "Count slowly" in text and "start" in text and "report" in text
    await pub.aclose()


@pytest.mark.asyncio
async def test_two_task_dispatch_without_a_summary_gets_a_unified_card():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    _dispatch(pub, "d-two", [GOAL, "Summarise the logs of the gateway"])
    await _settle(pub)
    assert len(adapter.sends) == 1 and adapter.status_calls == []
    assert "2 subagents" in adapter.sends[0]["content"]
    assert len(ac._PARTS["d-two"].checklist.kids) == 2
    await pub.aclose()


@pytest.mark.asyncio
async def test_helper_events_never_post_a_roster_card_without_a_summary():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    _dispatch(pub, "deleg_aaaa0001", [GOAL])
    cb = _child_cb(runner, delegation_id="deleg_aaaa0001", goal=GOAL)
    cb("subagent.start", preview=GOAL)
    cb("tool.started", "terminal", "sleep 2", {"command": "sleep 2"})
    cb("tool.completed", "terminal", None, None, duration=2.0, is_error=False, result="ok")
    cb("subagent.complete", status="completed", summary="Plain summary: counted.", duration_seconds=5)
    await _settle(pub)
    pub.end_turn()
    await _settle(pub)
    assert adapter.status_calls == [], "no 🔀 roster card on any path"
    assert pub.tracker.group_ids() == []
    cl = ac._PARTS["deleg_aaaa0001"].checklist
    assert cl.state == ac.DONE and cl.calls >= 1
    await pub.aclose()


@pytest.mark.asyncio
async def test_a_helper_nobody_opened_a_card_for_gets_one_from_its_first_event():
    """A nested helper / a dispatch path that skipped the card step."""
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    cb = _child_cb(runner, delegation_id="deleg_nested", goal=GOAL, title="Count slowly")
    cb("subagent.start", preview=GOAL)
    cb("tool.started", "terminal", "sleep 2", {"command": "sleep 2"})
    await _settle(pub)
    assert adapter.status_calls == []
    assert len(adapter.sends) == 1 and "Count slowly" in adapter.sends[0]["content"]
    assert ac.owns_delegation("deleg_nested")
    await pub.aclose()


@pytest.mark.asyncio
async def test_a_late_complete_event_does_not_revive_a_card():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    cb = _child_cb(runner, delegation_id="deleg_late")
    cb("subagent.complete", status="completed", summary="Plain summary: x.", duration_seconds=1)
    await _settle(pub)
    assert adapter.sends == [] and adapter.status_calls == [] and not ac.owns_delegation("deleg_late")
    await pub.aclose()


@pytest.mark.asyncio
async def test_a_second_dispatch_joins_the_running_card():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    _dispatch(pub, "d-a", [GOAL])
    await _settle(pub)
    _dispatch(pub, "d-b", ["Summarise the gateway logs for today"])
    await _settle(pub)
    assert len(adapter.sends) == 1, "one live card per chat"
    assert ac._PARTS["d-a"].card is ac._PARTS["d-b"].card
    await pub.aclose()


def test_a_dispatch_without_a_chat_opens_nothing():
    """CLI / cron: no sink bound -> nothing to claim, nothing to show."""
    bind_activity_sink(None)
    assert fl.reserve_ack_part("d-cli", None, [GOAL]) is False
    fl.note_own_dispatch(_Agent(), "d-cli", goals=[GOAL])
    assert not ac.owns_delegation("d-cli")


def test_agent_job_without_a_title_still_gets_a_card():
    card = ac.open_agent_job_part("job.0", "")
    assert card is not None and "agent job" in card.render()


# -- classic is unchanged ----------------------------------------------------------


@pytest.mark.asyncio
async def test_classic_keeps_the_roster_card_and_posts_no_unified_card():
    ac.set_style_for_tests("classic")
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    _dispatch(pub, "deleg_aaaa0001", [GOAL])
    assert not ac.owns_delegation("deleg_aaaa0001")
    cb = _child_cb(runner, delegation_id="deleg_aaaa0001", goal=GOAL)
    cb("subagent.start", preview=GOAL)
    cb("tool.started", "terminal", "sleep 2", {"command": "sleep 2"})
    await _settle(pub)
    assert adapter.status_calls, "classic: the 🔀 roster card"
    assert adapter.sends == []
    await pub.aclose()


@pytest.mark.asyncio
async def test_a_summary_dispatch_is_unchanged():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    handoff = {"work": "fixing the bugs", "outcome": "test build", "steps": ["find", "fix"]}
    _dispatch(pub, "d-sum", [GOAL], handoff=handoff)
    part = ac._PARTS["d-sum"]
    assert "fixing the bugs" in part.checklist.header and len(part.checklist.steps) == 2
    await pub.aclose()
