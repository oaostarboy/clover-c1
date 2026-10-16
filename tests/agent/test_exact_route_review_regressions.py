"""Independent adversarial production-path probes. No live inference/network."""
import json
import time
import pytest
from agent import delegation_router as dr, router_backstop as rb
from tools import async_delegation as ad
from tests.agent.test_requested_route_ownership import routes, running, root, wait_started
from tests.agent.test_router_staged_continuation import (
    isolated, helpers, aux, _dispatch_stage_one, _finish_and_deliver, _completion_turn,
)
from tests.agent.test_router_sol_review import root_agent

pytestmark = pytest.mark.real_delegation_router


def test_registered_handler_exact_and_fail_closed_boundaries(routes, running):
    from tools.registry import registry
    handler = registry.get_entry('delegate_task').handler
    agent = root()
    for args in (
        {'goal': 'Unknown named review', 'route': 'not-configured'},
        {'goal': 'Account-bound review', 'route': 'argon', 'account': 'exact@example.test'},
        {'tasks': [{'goal': 'Reject command injection', 'route': {'api_key': 'injected'}}]},
    ):
        result = json.loads(handler(args, parent_agent=agent, task_id='review-boundaries'))
        assert result.get('error'), result
        assert not ad.list_async_delegations()
    result = json.loads(handler({'goal': 'Registered exact review', 'route': 'argon'},
                                parent_agent=agent, task_id='review-registered'))
    assert result.get('status') == 'dispatched', result
    wait_started(running[0])
    assert running[0][0]['model'] == 'named-argon'
    assert not running[0][0]['fallback']
    with ad._transaction() as conn:
        payload = conn.execute('SELECT task_json FROM async_delegations WHERE delegation_id=?',
                               (result['delegation_id'],)).fetchone()[0]
    for forbidden in ('argon-test-secret', 'default-test-secret', 'http://127.0.0.1:2'):
        assert forbidden not in payload
    print('REGISTERED_BOUNDARIES unknown/account/injected blocked; exact named child accepted; no secrets/endpoints persisted')


def plan(second):
    return dr.parse_verdict({'mode': 'staged', 'tasks': [
        {'goal': 'Build the approved artifact in its admitted project'},
        {'goal': 'Review the exact artifact without another setup launch',
         'depends_on': [0], **second},
    ]})


def test_stop_clear_during_claimed_continuation_preparation(routes, helpers, aux, tmp_path, monkeypatch):
    agent = root_agent([], tmp_path)
    first = _dispatch_stage_one(agent, tmp_path, plan({}))
    _finish_and_deliver(helpers, first)
    original = rb._distinct_handoff
    stopped = []
    def stop_during_preparation(*args, **kwargs):
        if not stopped:
            stopped.append(True)
            agent.interrupt(hard_cancel=True)
            agent.clear_interrupt()
            assert ad.get_continuation_plan(first)['cancelled'] is True
        return original(*args, **kwargs)
    monkeypatch.setattr(rb, '_distinct_handoff', stop_during_preparation)
    _completion_turn(agent, first)
    rows = ad.list_async_delegations()
    print('STOP_PREPARATION', json.dumps({'stop_called': bool(stopped), 'first': first,
          'jobs': [{'id': r['delegation_id'], 'status': r.get('status')} for r in rows]}))
    assert stopped
    assert len(rows) == 1, 'Stopped/revoked claimed continuation spawned a new child after clear_interrupt'


def test_named_route_drift_during_continuation_preparation(routes, helpers, aux, tmp_path, monkeypatch):
    agent = root_agent([], tmp_path)
    first = _dispatch_stage_one(agent, tmp_path, plan({'route': 'argon'}))
    _finish_and_deliver(helpers, first)
    original = rb._distinct_handoff
    def change_during_preparation(*args, **kwargs):
        routes['routes']['argon']['model'] = 'substituted-after-admission'
        return original(*args, **kwargs)
    monkeypatch.setattr(rb, '_distinct_handoff', change_during_preparation)
    _completion_turn(agent, first)
    with ad._transaction() as conn:
        payloads = [json.loads(r[0]) for r in conn.execute(
            'SELECT task_json FROM async_delegations WHERE delegation_id != ?', (first,))]
    print('DRIFT_PREPARATION', json.dumps([p.get('requested_routes') for p in payloads]))
    assert not payloads, 'Changed admitted named route was resolved afresh and silently substituted'


def test_later_stage_existing_owner_reference_does_not_become_spawn_authority(routes, helpers, aux, tmp_path):
    agent = root_agent([], tmp_path)
    first = _dispatch_stage_one(agent, tmp_path, plan({
        'existing_delegation_id': 'deleg_nonexistent_stale_owner',
        'existing_job_epoch': 'stale-epoch',
    }))
    stored = ad.get_continuation_plan(first)
    print('STORED_OWNER_REFERENCE', json.dumps(stored['tasks'][1]))
    _finish_and_deliver(helpers, first)
    _completion_turn(agent, first)
    rows = ad.list_async_delegations()
    print('LATER_EXISTING_OWNER', json.dumps([r['delegation_id'] for r in rows]))
    assert len(rows) == 1, 'An existing-owner status task lost its owner reference and launched as new work'


def test_owned_continuation_retains_original_workspace_after_foreground_change(routes, helpers, aux, tmp_path):
    from tools import terminal_tool as tt, delegate_tool as dt
    from tools.approval import get_current_session_key
    agent = root_agent([], tmp_path)
    first = _dispatch_stage_one(agent, tmp_path, plan({}))
    original = ad.get_continuation_plan(first)['owner_scope']['workspace']
    different = tmp_path / 'different-foreground-project'
    different.mkdir()
    keys = {'default', 'checkpoint-test', 'sol-review', agent.session_id,
            get_current_session_key(default='') or ''}
    for key in keys - {''}:
        tt.record_session_cwd(key, str(different))
    assert dt._capture_parent_workspace(agent)[1] == str(different)
    _finish_and_deliver(helpers, first)
    _completion_turn(agent, first)
    with ad._records_lock:
        later = [r for did, r in ad._records.items() if did != first]
    assert len(later) == 1
    assert later[0]['owner_scope']['workspace'] == original
    assert tt.get_session_cwd('sol-review') == str(different)
    print('ORIGINAL_WORKSPACE retained for continuation; foreground session cwd record unchanged')


@pytest.mark.parametrize('change', ['stop', 'route', 'payload'])
def test_generated_dispatch_hook_cannot_reset_admission(routes, helpers, aux, tmp_path, monkeypatch, change):
    import agent.tool_executor as executor
    agent = root_agent([], tmp_path)
    first = _dispatch_stage_one(agent, tmp_path, plan({'route': 'argon'}))
    _finish_and_deliver(helpers, first)
    real = executor.dispatch_generated_tool
    def hook(a, name, args, **kw):
        if change == 'stop':
            a.interrupt(hard_cancel=True)
            a.clear_interrupt()
        elif change == 'route':
            routes['routes']['argon']['model'] = 'substituted-by-late-hook'
        else:
            args['tasks'][0].pop('route', None)
        return real(a, name, args, **kw)
    monkeypatch.setattr(executor, 'dispatch_generated_tool', hook)
    _completion_turn(agent, first)
    assert len(ad.list_async_delegations()) == 1


def test_real_task_stop_revokes_owner_but_not_independent_job(routes, running):
    from agent.delegation_ownership import existing_owner
    from tests.agent.test_delegation_checkpoint import run_batch
    agent = root()
    agent._current_turn_id = 'first-independent-job'
    (first,) = run_batch(agent, [('delegate_task', {'tasks': [{'goal': 'Original adviser launch setup'}]})])
    agent._current_turn_id = 'second-independent-job'
    (second,) = run_batch(agent, [('delegate_task', {'tasks': [{'goal': 'Separate independent research'}]})])
    wait_started(running[0], 2)
    assert existing_owner(agent, first['delegation_id'], 'checkpoint-test')
    (stopped,) = run_batch(agent, [('delegate_task', {'action': 'stop', 'subagent_id': first['subagent_ids'][0]})])
    assert stopped.get('status') == 'interrupt_requested'
    assert existing_owner(agent, first['delegation_id'], 'checkpoint-test') is None
    assert existing_owner(agent, second['delegation_id'], 'checkpoint-test')


def test_executor_submit_stop_clear_cannot_accept_prepared_child(routes, helpers, aux, tmp_path, monkeypatch):
    agent = root_agent([], tmp_path)
    first = _dispatch_stage_one(agent, tmp_path, plan({'route': 'argon'}))
    _finish_and_deliver(helpers, first)
    original_get = ad._get_executor
    class StoppingExecutor:
        def submit(self, *args, **kw):
            agent.interrupt(hard_cancel=True)
            agent.clear_interrupt()
            return original_get(4).submit(*args, **kw)
    monkeypatch.setattr(ad, '_get_executor', lambda *args: StoppingExecutor())
    _completion_turn(agent, first)
    assert len(ad.list_async_delegations()) == 1
    assert len(helpers.goals) == 1
    assert ad.active_count() == 0


@pytest.mark.parametrize('owner_state', ['active', 'stale_epoch', 'foreign', 'cancelled'])
def test_later_stage_existing_owner_never_grants_replacement(routes, helpers, aux, tmp_path, monkeypatch, owner_state):
    import threading
    import run_agent
    from tests.agent.test_delegation_checkpoint import run_batch
    from agent.delegation_ownership import existing_owner
    original_conversation = run_agent.AIAgent.run_conversation
    original_started, original_release = threading.Event(), threading.Event()
    def conversation(self, user_message=None, **kw):
        if 'EXCLUSIVE-ORIGINAL' in str(user_message):
            original_started.set()
            assert original_release.wait(15)
            return {'final_response': 'original finished', 'messages': [], 'api_calls': 1, 'completed': True}
        return original_conversation(self, user_message=user_message, **kw)
    monkeypatch.setattr(run_agent.AIAgent, 'run_conversation', conversation)
    agent = root_agent([], tmp_path)
    launch_agent = agent
    if owner_state == 'foreign':
        launch_agent = root_agent([], tmp_path)
        launch_agent.session_id = 'foreign-actual-owner'
    try:
        (original,) = run_batch(launch_agent, [('delegate_task', {'tasks': [{'goal': 'EXCLUSIVE-ORIGINAL launch owner'}]})])
        assert original_started.wait(5)
        owner = existing_owner(launch_agent, original['delegation_id'], 'checkpoint-test')
        assert owner
        first = _dispatch_stage_one(agent, tmp_path, plan({
            'existing_delegation_id': original['delegation_id'],
            'existing_job_epoch': 'deliberately-stale-epoch' if owner_state == 'stale_epoch' else owner['job_epoch']}))
        if owner_state == 'cancelled':
            (stopped,) = run_batch(launch_agent, [('delegate_task', {'action': 'stop', 'subagent_id': original['subagent_ids'][0]})])
            assert stopped.get('status') == 'interrupt_requested'
        _finish_and_deliver(helpers, first)
        _completion_turn(agent, first)
        assert len(ad.list_async_delegations()) == 2  # original + build, no replacement
        if owner_state == 'active':
            assert existing_owner(agent, original['delegation_id'], 'checkpoint-test') is not None
            state = rb._state(agent)
            assert state is not None and state.handoff is not None
            assert state.handoff['reused_owner'] is True
            assert state.handoff['delegation_id'] == original['delegation_id']
    finally:
        original_release.set()
        deadline = time.monotonic() + 8
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(.02)
