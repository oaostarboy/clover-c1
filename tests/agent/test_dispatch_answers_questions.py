"""A dispatch reply must still answer the user's OTHER questions.

Live bug: "yes, and what about the other fixes?" got a single-helper plan whose
note said "end your turn with one short line saying what is running"; the agent
obeyed and the question was silently dropped.  Every non-direct work plan, the
hand-off text, the tools-off opener turn and the stable prompt guidance now say
to answer the rest of the message first.
"""
from __future__ import annotations

import pytest

from agent import delegation_router as dr
from agent import handoff_notes as hn
from tests.agent.test_delegation_router import T, aux, verdict_json  # noqa: F401
from tests.agent.test_foreground_time_limit_loop import (  # noqa: F401
    make_agent,
    provider,
    text,
    tool,
)
from tests.agent.test_router_backstop import (  # noqa: F401
    _clean_registry,
    _wait_started,
    dispatches,
)
from tests.agent.test_router_e2e import (  # noqa: F401
    MARKER,
    _unbind_session_context,
    _wire_users,
    children,
)

pytestmark = pytest.mark.real_delegation_router

ANSWER = "first answer any question or other point in the user's message that the helpers are not doing"
RUNNING = "then one short line on what is running"


def _tasks(*goals):
    return tuple(dr.PlanTask(goal=g) for g in goals)


def _verdict(mode, tasks, reason="because"):
    return dr.Verdict(mode=mode, tasks=tuple(tasks), reason=reason)


@pytest.mark.parametrize("verdict", [
    _verdict("single", _tasks("build it")),
    _verdict("parallel", _tasks("audit A", "audit B")),
    _verdict("staged", [dr.PlanTask(goal="write it"), dr.PlanTask(goal="review it", depends_on=(0,))]),
])
def test_every_dispatch_plan_tells_the_agent_to_answer_the_question(verdict):
    note = dr.work_plan_note(verdict)
    assert ANSWER in note and RUNNING in note
    assert "end your turn with one short line" not in note
    assert note.startswith(dr.RUNTIME_NOTE_MARKER)
    assert note == dr.work_plan_note(verdict), "byte-stable per turn"


def test_the_direct_plan_is_unchanged():
    assert dr.work_plan_note(_verdict("direct", ())) == (
        f"{dr.RUNTIME_NOTE_MARKER} work plan: direct (because). Handle it here.")


def test_the_stable_prompt_guidance_says_to_answer_the_rest():
    from agent.prompt_builder import DELEGATION_MODE_GUIDANCE, TODO_DELEGATION_DECISION_GUIDANCE

    assert "answer what the helpers aren't covering, end your turn" in DELEGATION_MODE_GUIDANCE
    assert "answer" in DELEGATION_MODE_GUIDANCE
    assert "answers anything else the user asked" in TODO_DELEGATION_DECISION_GUIDANCE


def test_the_hand_off_result_tells_the_agent_to_answer_the_rest():
    from agent import router_backstop as rb

    text_ = rb._handoff_text({"subagent_ids": ["sa-1"], "delegation_id": "d1",
                              "next_stage": None, "later": []})
    assert "Answer any question" in text_ and "not covering" in text_


def test_the_delegate_task_say_field_asks_for_the_answer_too():
    from tools.delegate_tool import DELEGATE_TASK_SCHEMA

    say = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["handoff"]["properties"]["say"]
    assert "Answer anything else the user asked" in say["description"]
    from agent.ack_checklist import MAX_SAY_CHARS
    assert say["maxLength"] == MAX_SAY_CHARS


def test_the_opener_turn_note_asks_for_the_answer_and_keeps_its_bounds(monkeypatch):
    """The tools-off opener model turn gets a runtime note that covers the
    user's other question; its reply is kept whole (question answer + status)."""
    import agent.chat_completion_helpers as cch
    from types import SimpleNamespace

    seen = {}

    def fake(agent, history, n, reason="", request_text="", max_tokens=None, quiet=False):
        seen["note"], seen["max_tokens"] = request_text, max_tokens
        return ("The other fixes are already merged. Helper is on the restart now, "
                "I'll tell you when it lands.")

    monkeypatch.setattr(cch, "handle_max_iterations", fake)
    msgs = [{"role": "user", "content": "yes, and what about the other fixes?"}]
    line = hn.voice_line(SimpleNamespace(), msgs, "restart the gateway",
                         message="yes, and what about the other fixes?")
    note = seen["note"]
    assert note.startswith(dr.RUNTIME_NOTE_MARKER)
    assert "asked a question" in note and "answer that first" in note
    assert "do not call tools" in note
    assert line.startswith("The other fixes are already merged")
    assert seen["max_tokens"] == hn.OPENER_MAX_TOKENS >= 100


def test_replay_yes_and_what_about_the_other_fixes(provider, aux, tmp_path, children, dispatches):
    """The live message, with a forced single plan: the note the model reads on
    that turn carries the answer-the-question instruction."""
    aux.replies[:] = [verdict_json("single", [T("Restart the gateway once the ZIP is done")])]
    provider.queue[:] = [
        tool("write_file", {"path": str(tmp_path / "a.txt"), "content": "a"}, "c1"),
        tool("write_file", {"path": str(tmp_path / "b.txt"), "content": "b"}, "c2"),
        tool("write_file", {"path": str(tmp_path / "c.txt"), "content": "c"}, "c3"),
    ]
    agent = make_agent(provider, session_id="e2e-answers-questions")
    message = "yes, and what about the other fixes?"
    agent.run_conversation(message, conversation_history=[], task_id="e2e-aq")

    first_user = _wire_users(provider.captured[0])[-1]
    assert first_user.startswith(message)
    assert f"{MARKER} single helper" in first_user
    assert ANSWER in first_user and RUNNING in first_user
    assert "end your turn with one short line" not in first_user
