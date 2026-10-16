"""Actual root conversation -> correction planner -> same live owner -> answer.

Offline scripted provider I/O only; production loop/executor/router/DB/child
lifecycle. This is not a native Telegram or live-model transport claim.
"""
import json

import pytest

from agent import delegation_router as dr
from agent import router_backstop as rb
from tests.agent.test_requested_route_ownership import routes, _clean_registry  # noqa: F401
from tests.agent.test_router_staged_continuation import helpers, aux, _wait  # noqa: F401
from tests.agent.test_router_sol_review import root_agent
from tests.agent.test_handoff_completion_seam import _tools, _text, _call
from tools import async_delegation as ad

pytestmark = pytest.mark.real_delegation_router


def test_real_two_turn_status_keeps_original_launch_and_separate_answer(routes, helpers, aux, tmp_path, monkeypatch):
    settings = dr.RouterSettings(enabled=True, backstop_calls=1, backstop_seconds=60)
    monkeypatch.setattr(dr, 'load_settings', lambda: settings)
    original_goal = 'Prepare and launch the real named adviser; this setup operator is SOL'
    agent = root_agent([_tools(_call('delegate_task', {'tasks': [{'goal': original_goal}]}, 'setup'))], tmp_path)
    aux.replies = [json.dumps({'mode': 'single', 'tasks': [{'goal': original_goal}]})]
    first = agent.run_conversation('Prepare the adviser audit and preserve the exact requested route.')
    _wait(lambda: len(helpers.goals) == 1, 'original setup operator')
    records = ad.list_async_delegations()
    assert len(records) == 1
    did = records[0]['delegation_id']
    epoch = ad._records[did]['job_epoch']
    original_turn = ad._records[did]['request_turn_id']
    aux.replies = [json.dumps({'mode': 'single', 'tasks': [{
        'goal': 'Inspect existing launch status and explain the user correction without a second setup job',
        'existing_delegation_id': did, 'existing_job_epoch': epoch}]})]
    expected = 'No second setup worker started. A title does not select the model. The requested adviser is not verified as started.'
    agent.client.chat.completions.create.side_effect = [
        _tools(_call('read_file', {'path': str(tmp_path / 'status.json')}, 'status-1'),
               _call('read_file', {'path': str(tmp_path / 'status-2.json')}, 'status-2')),
        _text(expected)]
    second = agent.run_conversation(
        'It is still SOL, not Argon. Check the original launch status. Also answer: does a title select the model?',
        conversation_history=first.get('messages'))
    assert second['final_response'] == expected
    assert len(helpers.goals) == 1
    assert (ad.get_durable_delegation(did) or {}).get('state') == 'running'
    assert ad._records[did]['request_turn_id'] == original_turn
    assert rb._state(agent).handoff['reused_owner'] is True
    assert agent._current_turn_id != original_turn
    planner_inputs = '\n'.join(str(c['messages']) for c in aux.router_calls)
    assert did in planner_inputs and epoch in planner_inputs
    assert 'default-test-secret' not in planner_inputs


def test_mixed_top_account_constraint_cannot_be_silently_dropped(routes, helpers):
    from tests.agent.test_delegation_checkpoint import _agent, run_batch
    agent = _agent(opt_in=False)
    (result,) = run_batch(agent, [('delegate_task', {
        'tasks': [{'goal': 'Run the exact-account constrained audit'}],
        'account': 'exact@example.test'})])
    assert result.get('status') != 'dispatched', result
    assert 'account' in result.get('error', '').lower()


def test_callback_mixed_carrier_cannot_hide_extra_account_requirement():
    from agent.delegation_followthrough import matches_stage
    stage = {'goal': 'verify saved artifact'}
    assert not matches_stage({'tasks': [stage], 'account': 'exact@example.test'}, stage)
