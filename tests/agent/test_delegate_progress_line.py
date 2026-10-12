"""The ``delegate_task`` tool-progress line never pastes the task goal."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unified_card

from agent import ack_checklist as ac
from agent.display import delegate_progress_line

GOAL = "Find the 10 longest Python files in the repo and list them"
HANDOFF = {"work": "checking the repo", "outcome": "three counts"}


@pytest.fixture(autouse=True)
def _style():
    ac.reset_for_tests()
    yield
    ac.reset_for_tests()


def test_no_line_when_the_unified_card_will_be_shown():
    ac.set_style_for_tests("unified")
    tasks = [{"goal": GOAL}, {"goal": "b"}, {"goal": "c"}]
    assert delegate_progress_line({"tasks": tasks, "handoff": HANDOFF}) == ""
    assert delegate_progress_line({"goal": GOAL, "handoff": HANDOFF}) == ""


def test_without_a_handoff_the_line_stays_but_has_no_goal():
    ac.set_style_for_tests("unified")
    line = delegate_progress_line({"tasks": [{"goal": GOAL}, {"goal": "b"}, {"goal": "c"}]})
    assert line == "Delegating 3 subagents" and "Find" not in line
    assert delegate_progress_line({"goal": GOAL}) == "Delegating a subagent"


def test_classic_style_keeps_the_line_even_with_a_handoff():
    ac.set_style_for_tests("classic")
    line = delegate_progress_line({"tasks": [{"goal": GOAL}, {"goal": "b"}], "handoff": HANDOFF})
    assert line == "Delegating 2 subagents"


def test_list_steer_stop_use_the_generic_path():
    ac.set_style_for_tests("unified")
    assert delegate_progress_line({"action": "stop", "subagent_id": "sa-1"}) is None


def _drain(args, *, style, mode="all"):
    from tests.gateway.test_live_reasoning_progress import _base_turn_ctx, _make_mocked_gateway_runner
    from gateway.run import TurnRunner

    ac.set_style_for_tests(style)
    ctx = _base_turn_ctx(tool_progress_enabled=True, progress_mode=mode)
    TurnRunner(_make_mocked_gateway_runner(), ctx).progress_callback(
        "tool.started", "delegate_task", GOAL, args,
    )
    out = []
    while not ctx.progress_queue.empty():
        out.append(ctx.progress_queue.get_nowait())
    return out


def test_gateway_drops_the_line_for_a_card_and_never_pastes_the_goal_otherwise():
    tasks = [{"goal": GOAL}, {"goal": GOAL + " 2"}, {"goal": GOAL + " 3"}]
    assert _drain({"tasks": tasks, "handoff": HANDOFF}, style="unified") == []
    lines = _drain({"tasks": tasks}, style="unified")
    assert len(lines) == 1 and lines[0].endswith("Delegating 3 subagents") and "Find" not in lines[0]
    classic = _drain({"tasks": tasks, "handoff": HANDOFF}, style="classic")
    assert len(classic) == 1 and "Delegating 3 subagents" in classic[0] and "Find" not in classic[0]
