"""Reliability follow-through uses only the existing accepted next-stage owner."""
from types import SimpleNamespace

import pytest

from agent import router_backstop as rb, delegation_router as dr
from tests.agent.test_router_staged_continuation import (
    helpers, isolated, aux, root_agent, _staged, _dispatch_stage_one,
    _finish_and_deliver, _all_ids, _new_dispatch_ids, _wait, STAGE_ONE, STAGE_TWO,
    _completion_turn,
)

pytestmark = pytest.mark.real_delegation_router


@pytest.fixture
def reported_helpers(helpers, monkeypatch):
    import run_agent
    original = run_agent.AIAgent.run_conversation
    state = SimpleNamespace(report=None, exhausted=False)
    def conversation(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if getattr(self, '_delegate_depth', 0) or getattr(self, '_subagent_id', None):
            if state.report:
                result['task_outcome'] = dict(state.report)
            if state.exhausted:
                result['completed'] = False
        return result
    monkeypatch.setattr(run_agent.AIAgent, 'run_conversation', conversation)
    return helpers, state


def setup_chain(aux, tmp_path, reported_helpers, report, exhausted=False):
    helpers, state = reported_helpers
    state.report, state.exhausted = report, exhausted
    agent = root_agent([], tmp_path)
    first = _dispatch_stage_one(agent, tmp_path, _staged((STAGE_ONE, ()), (STAGE_TWO, (0,))))
    _finish_and_deliver(helpers, first)
    return agent, first, helpers


def continue_and_note(agent, first):
    _completion_turn(agent, first)
    sent = agent.client.chat.completions.create.call_args.kwargs['messages']
    return '\n'.join(str(message.get('content', '')) for message in sent if isinstance(message, dict))


def test_partial_actionable_continues_only_the_preaccepted_next_stage(aux, tmp_path, reported_helpers):
    report = {'status': 'partial', 'remaining': [STAGE_TWO], 'owner': 'same execution owner',
              'next_step': STAGE_TWO}
    agent, first, helpers = setup_chain(aux, tmp_path, reported_helpers, report)
    before = set(_all_ids())
    note = continue_and_note(agent, first)
    new = _new_dispatch_ids(before)
    assert len(new) == 1, note
    _wait(lambda: len(helpers.goals) == 2, 'accepted next stage')
    assert STAGE_TWO in helpers.goals[1]['user']
    assert rb.continue_staged(agent, [first], dr.RouterSettings()) == ''
    assert len(_all_ids()) == len(before) + 1


def test_authorization_blocker_keeps_pending_work_and_one_specific_question(aux, tmp_path, reported_helpers):
    question = 'Do you approve the read-only Drive check for this project?'
    report = {'status': 'blocked', 'remaining': ['Drive authorization'],
              'owner': 'human owner', 'next_step': question}
    agent, first, helpers = setup_chain(aux, tmp_path, reported_helpers, report)
    before = set(_all_ids())
    note = continue_and_note(agent, first)
    assert _new_dispatch_ids(before) == []
    assert question in note
    assert note.count(question) == 1
    assert 'NOT started' in note
    from tools import async_delegation as ad
    plan = ad.get_continuation_plan(first)
    assert plan is not None
    assert plan['consumed'] == 0 and plan['stages'], 'blocked work must remain durable'


def test_exhausted_retry_escalates_without_another_worker(aux, tmp_path, reported_helpers, monkeypatch):
    # Disable inherited auto-continuation: this lane proves exhausted means exhausted.
    monkeypatch.setattr('tools.delegate_tool._auto_continue_limit', lambda: 0)
    report = {'status': 'partial', 'remaining': [STAGE_TWO],
              'owner': 'parent reviewer', 'next_step': 'Review saved results; the retry budget is exhausted'}
    agent, first, helpers = setup_chain(aux, tmp_path, reported_helpers, report, exhausted=True)
    before = set(_all_ids())
    note = continue_and_note(agent, first)
    assert _new_dispatch_ids(before) == []
    assert 'exhausted' in note
    assert 'NOT started' in note


def test_completed_without_accepted_next_action_is_quiet(aux, tmp_path, reported_helpers):
    helpers, state = reported_helpers
    state.report = {'status': 'done', 'remaining': [], 'owner': 'parent reviewer', 'next_step': 'No action needed'}
    agent = root_agent([], tmp_path)
    first = _dispatch_stage_one(agent, tmp_path, _staged((STAGE_ONE, ()), (STAGE_TWO, (0,))))
    _finish_and_deliver(helpers, first)
    from tools import async_delegation as ad
    ad.revoke_continuation_plan(first)  # explicit no further action/authority
    before = set(_all_ids())
    assert rb.continue_staged(agent, [first], dr.RouterSettings()) == ''
    assert _new_dispatch_ids(before) == []
