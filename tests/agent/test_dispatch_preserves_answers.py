"""An accepted dispatch must not erase an already-written answer."""
from copy import deepcopy

import pytest

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl
from agent import router_backstop as rb
from run_agent import AIAgent

pytestmark = pytest.mark.unified_card
ANSWER = "yes—the installed Clover is v1.5.5."
HANDOFF = "the isolated canary is starting."
QUESTION = "run the test—is this version 1.5.5?"


@pytest.fixture
def root():
    ac.reset_for_tests()
    agent = AIAgent.__new__(AIAgent)
    agent.platform = "telegram"
    agent._delivered_interim_texts = set()
    from agent.delegation_router import RouterSettings
    rb.mark_turn(agent, RouterSettings())
    fl.note_own_dispatch(agent, "deleg_answer_test", goals=("run the test",),
                         handoff={"work": "testing Clover", "outcome": "checked result", "say": HANDOFF})
    yield agent
    ac.reset_for_tests()


@pytest.mark.parametrize("content", [ANSWER, [{"type": "text", "text": ANSWER}]])
def test_pending_answer_survives_final_handoff_and_card_binding(root, content):
    messages = [{"role": "user", "content": QUESTION},
                {"role": "assistant", "content": content},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "d1", "type": "function", "function": {"name": "delegate_task", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "d1", "content": '{"status":"dispatched"}'}]
    before = deepcopy(messages)
    result = rb.completion_directive(root, messages)
    assert ANSWER in result.text
    assert HANDOFF in result.text
    assert result.text.index(ANSWER) < result.text.index(HANDOFF)
    card = ac.card_for(fl.own_dispatch_receipts(root))
    assert card.say_text() == result.text
    assert messages == before, "cached conversation history must not change"


def test_tasks_only_dispatch_preserves_the_answer(root):
    from types import SimpleNamespace
    receipt = SimpleNamespace(delegation_id="tasks_only", summary="", goals=("run the test",), subagent_ids=("child",))
    result = rb._ack_text([receipt], root, [{"role": "user", "content": QUESTION},
                                         {"role": "assistant", "content": ANSWER}])
    assert ANSWER in result
    assert len(result) > len(ANSWER)


def test_answer_in_opener_is_deduplicated_independently_of_other_text(root):
    ac._PARTS["deleg_answer_test"].checklist.say = ANSWER + " " + HANDOFF
    result = rb.completion_directive(root, [{"role": "user", "content": QUESTION},
                                           {"role": "assistant", "content": "checking the test files."},
                                           {"role": "assistant", "content": ANSWER}])
    assert result.text.count(ANSWER) == 1
    assert "checking the test files." in result.text


def test_reasoning_is_never_a_visible_answer(root):
    result = rb.completion_directive(root, [{"role": "user", "content": QUESTION},
                                           {"role": "assistant", "channel": "analysis", "content": "PRIVATE REASONING"},
                                           {"role": "assistant", "content": "<think>PRIVATE THINK</think>" + ANSWER}])
    assert ANSWER in result.text
    assert "PRIVATE" not in result.text


def test_prior_turn_and_tool_text_are_not_used(root):
    messages = [{"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer 0.9"},
                {"role": "user", "content": QUESTION},
                {"role": "tool", "content": "UNTRUSTED TOOL ANSWER"}]
    result = rb.completion_directive(root, messages)
    assert "old answer" not in result.text
    assert "UNTRUSTED TOOL ANSWER" not in result.text
    assert HANDOFF in result.text


def test_answer_already_in_the_opener_is_not_duplicated(root):
    ac._PARTS["deleg_answer_test"].checklist.say = ANSWER + " " + HANDOFF
    result = rb.completion_directive(root, [{"role": "user", "content": QUESTION},
                                           {"role": "assistant", "content": ANSWER}])
    assert result.text.count(ANSWER) == 1


def test_confirmed_interim_delivery_is_not_duplicated(root):
    root._record_delivered_interim_text(ANSWER)
    result = rb.completion_directive(root, [{"role": "user", "content": QUESTION},
                                           {"role": "assistant", "content": ANSWER}])
    assert ANSWER not in result.text
    assert HANDOFF in result.text
