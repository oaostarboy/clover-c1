"""Exact route/ownership incident regressions, real registered dispatch + children.

Only provider I/O is scripted. Temp CLOVER_HOME and canonical runner isolate all
state. No calls to real accounts or inference services.
"""
import json
import threading
import time

import pytest

from agent import delegation_router as dr
from agent import router_backstop as rb
from tests.agent.test_delegation_checkpoint import _agent, run_batch
from tests.agent.test_router_backstop import _clean_registry, clock, dispatches  # noqa: F401
from tests.agent.test_router_staged_continuation import helpers, aux  # noqa: F401
from tools import async_delegation as ad
from tools import delegate_tool as dt

pytestmark = pytest.mark.real_delegation_router


@pytest.fixture
def routes(monkeypatch):
    cfg = {'provider': 'custom', 'model': 'default-sol',
           'base_url': 'http://127.0.0.1:1/v1', 'api_key': 'default-test-secret',
           'default_tier': '', 'max_iterations': 3,
           'routes': {'argon': {'provider': 'fixture-argon', 'model': 'named-argon'}}}
    monkeypatch.setattr(dt, '_load_config', lambda: cfg)
    import clover_cli.runtime_provider as rp
    def resolve(**kw):
        assert kw['requested'] == 'fixture-argon'
        return {'provider': 'custom', 'model': 'named-argon',
                'base_url': 'http://127.0.0.1:2/v1', 'api_key': 'argon-test-secret',
                'api_mode': 'chat_completions'}
    monkeypatch.setattr(rp, 'resolve_runtime_provider', resolve)
    monkeypatch.setattr(dt, '_resolve_child_credential_pool', lambda *a, **kw: None)
    return cfg


@pytest.fixture
def running(monkeypatch):
    import run_agent
    started, release = [], threading.Event()
    def conversation(self, user_message=None, **kw):
        started.append({'model': self.model, 'provider': self.provider,
                        'fallback': getattr(self, '_fallback_chain', None),
                        'request': getattr(self, '_delegate_requested_route', None),
                        'user': user_message})
        assert release.wait(12), 'test did not release scripted provider'
        return {'final_response': 'checked artifact', 'messages': [], 'api_calls': 1,
                'completed': True}
    monkeypatch.setattr(run_agent.AIAgent, 'run_conversation', conversation)
    yield started, release
    release.set()
    deadline = time.monotonic() + 8
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(.02)


def wait_started(started, n=1):
    deadline = time.monotonic() + 8
    while len(started) < n and time.monotonic() < deadline:
        time.sleep(.02)
    assert len(started) == n


def root():
    return _agent(opt_in=False, enabled_toolsets=['delegation', 'file', 'terminal', 'todo'])


def test_registered_exact_route_overrides_default_without_secret_payload(routes, running):
    agent = root()
    (result,) = run_batch(agent, [('delegate_task', {'tasks': [
        {'goal': 'Perform the named model audit', 'route': 'argon'}]})])
    assert result['status'] == 'dispatched'
    wait_started(running[0])
    assert running[0][0]['model'] == 'named-argon'
    assert not running[0][0]['fallback']
    row = ad.get_durable_delegation(result['delegation_id'])
    assert row is not None
    with ad._transaction() as conn:
        payload = json.loads(conn.execute('SELECT task_json FROM async_delegations WHERE delegation_id=?',
                                          (result['delegation_id'],)).fetchone()[0])
    assert payload['requested_routes'][0]['route'] == 'argon'
    assert payload['requested_routes'][0]['model'] == 'named-argon'
    assert 'argon-test-secret' not in json.dumps(row)
    assert 'default-test-secret' not in json.dumps(row)


def test_router_preserves_exact_route_account_and_existing_owner():
    verdict = dr.parse_verdict({'mode': 'single', 'tasks': [{
        'goal': 'Inspect the requested adviser startup without another launch',
        'route': 'argon', 'account': 'oaoantngyn@gmail.com',
        'existing_delegation_id': 'deleg_original'}]})
    task = verdict.tasks[0]
    assert getattr(task, 'route', None) == 'argon'
    assert getattr(task, 'account', None) == 'oaoantngyn@gmail.com'
    assert getattr(task, 'existing_delegation_id', None) == 'deleg_original'


def test_status_correction_reuses_previous_turn_original_setup(routes, running, dispatches, tmp_path):
    agent = root()
    agent._current_turn_id = 'original-request'
    (first,) = run_batch(agent, [('delegate_task', {'tasks': [
        {'goal': 'Prepare and launch the real adviser; this setup operator is SOL'}]})])
    wait_started(running[0])
    agent._current_turn_id = 'correction-status'
    verdict = dr.parse_verdict({'mode': 'single', 'tasks': [{
        'goal': 'Check why the adviser is not running yet; preserve original launch owner',
        'existing_delegation_id': first['delegation_id'],
        'existing_job_epoch': ad._records[first['delegation_id']]['job_epoch']}]})
    rb.begin(agent, verdict, 'It is still SOL, not Argon. What is happening?',
             dr.RouterSettings(enabled=True, backstop_calls=1, backstop_seconds=60))
    results = run_batch(agent, [('read_file', {'path': str(tmp_path / 'status')}),
                                ('read_file', {'path': str(tmp_path / 'status')})])
    assert results[-1]['delegation_id'] == first['delegation_id']
    assert not dispatches, 'a status correction launched a duplicate setup helper'
    assert len(running[0]) == 1


def test_competing_automatic_dispatch_has_one_owner(routes, running, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    agents = [root(), root()]
    agents[1].session_id = agents[0].session_id
    for a in agents:
        a._current_turn_id = 'same-logical-request'
        rb.begin(a, dr.Verdict(mode='single', tasks=(dr.PlanTask('Launch the one owned adviser job'),)),
                 'Launch the adviser', dr.RouterSettings(enabled=True, backstop_calls=1))
    monkeypatch.setattr(rb, '_prepare_handoff', lambda *a: ('', SimpleNamespace(title='', goal='')))
    barrier = threading.Barrier(2)
    def dispatch(a):
        barrier.wait(timeout=5)
        return rb._dispatch(a, rb._state(a), 'checkpoint-test')
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(dispatch, agents))
    wait_started(running[0])
    assert len(running[0]) == 1, 'competing backstops both launched the same job'
    assert sum(r.get('ok') and not r.get('reused_owner') for r in result) == 1


def test_next_stage_route_survives_durable_plan():
    verdict = dr.parse_verdict({'mode': 'staged', 'tasks': [
        {'goal': 'Build the approved HTML artifact in the project'},
        {'goal': 'Review the resulting HTML artifact with the named adviser',
         'route': 'argon', 'account': 'exact@example.test', 'depends_on': [0]}]})
    payload = rb._plan_tasks(verdict)
    assert payload[1]['route'] == 'argon'
    assert payload[1]['account'] == 'exact@example.test'
    # Continuation must reconstruct structured constraints, not just context prose.
    from types import SimpleNamespace
    seen = []
    pseudo = SimpleNamespace(verdict=verdict, user_message='build then review')
    # The persisted task representation is the one consumed by _continue_one.
    restored = dr.PlanTask(**{**payload[1], 'depends_on': tuple(payload[1]['depends_on'])})
    assert rb._task_route_args(restored) == {'route': 'argon', 'account': 'exact@example.test'}


def test_real_staged_next_child_uses_preserved_named_route(routes, helpers, aux, tmp_path):
    from tests.agent.test_router_staged_continuation import (
        _dispatch_stage_one, _finish_and_deliver, _completion_turn, _wait)
    from tests.agent.test_router_sol_review import root_agent
    agent = root_agent([], tmp_path)
    plan = dr.parse_verdict({'mode': 'staged', 'tasks': [
        {'goal': 'Build the approved HTML artifact in the project'},
        {'goal': 'Review the resulting HTML artifact with the named adviser',
         'route': 'argon', 'depends_on': [0]}]})
    first = _dispatch_stage_one(agent, tmp_path, plan)
    _finish_and_deliver(helpers, first)
    _completion_turn(agent, first, 'The build finished; the review is next.')
    _wait(lambda: len(helpers.goals) == 2, 'next-stage reviewer')
    with ad._transaction() as conn:
        payloads = [json.loads(r[0]) for r in conn.execute(
            'SELECT task_json FROM async_delegations WHERE delegation_id != ?', (first,))]
    assert any(p.get('requested_routes') == [{'route': 'argon', 'provider': 'fixture-argon',
                                            'model': 'named-argon'}] for p in payloads)
    assert ad.get_continuation_plan(first)['consumed'] == 1


@pytest.mark.parametrize('task, fragment', [
    ({'route': 'missing-model'}, 'not configured'),
    ({'route': 'argon', 'account': 'oaoantngyn@gmail.com'}, 'account binding is unsupported'),
    ({'route': 'argon', 'tier': 'fast'}, 'fallback tier'),
    ({'route': {'api_key': 'not-a-selector'}}, 'Invalid named'),
])
def test_invalid_or_account_bound_route_never_starts_default(routes, running, task, fragment):
    agent = root()
    (result,) = run_batch(agent, [('delegate_task', {'tasks': [
        {'goal': 'Run the explicitly constrained audit', **task}]})])
    assert fragment in result.get('error', '')
    assert not running[0]
    assert not ad.list_async_delegations()


def test_unavailable_named_provider_does_not_leak_or_fallback(routes, running, monkeypatch):
    import clover_cli.runtime_provider as rp
    def unavailable(**kw):
        raise RuntimeError('secret-value at https://user:password@private.example.test')
    monkeypatch.setattr(rp, 'resolve_runtime_provider', unavailable)
    (result,) = run_batch(root(), [('delegate_task', {'tasks': [
        {'goal': 'Run the explicitly constrained audit', 'route': 'argon'}]})])
    assert 'unavailable' in result.get('error', '')
    assert 'secret-value' not in json.dumps(result)
    assert 'password' not in json.dumps(result)
    assert not running[0]


def test_unspecified_parallel_research_stays_on_default(routes, running):
    agent = root()
    (result,) = run_batch(agent, [('delegate_task', {'tasks': [
        {'goal': 'Research the first independent evidence lane'},
        {'goal': 'Research the second independent evidence lane', 'route': 'argon'}]})])
    assert result['status'] == 'dispatched'
    wait_started(running[0], 2)
    assert sorted(c['model'] for c in running[0]) == ['default-sol', 'named-argon']


def test_restart_descriptor_preserves_exact_route_account():
    from gateway.delegation_resume import build_resume_tasks
    record = {'tasks': [{'goal': 'Continue the exact constrained audit',
                         'route': 'argon', 'account': 'exact@example.test'}]}
    tasks = build_resume_tasks(record, [0])
    assert tasks[0]['route'] == 'argon'
    assert tasks[0]['account'] == 'exact@example.test'


def test_completed_child_has_requested_and_actual_provenance(routes, running):
    (result,) = run_batch(root(), [('delegate_task', {'tasks': [
        {'goal': 'Run the named review with authoritative metadata', 'route': 'argon'}]})])
    wait_started(running[0])
    running[1].set()
    deadline = time.monotonic() + 8
    while (ad.get_durable_delegation(result['delegation_id']) or {}).get('state') == 'running' and time.monotonic() < deadline:
        time.sleep(.02)
    row = ad.get_durable_delegation(result['delegation_id'])
    receipt = row['result']['results'][0]['route_provenance']
    assert receipt['requested']['route'] == 'argon'
    assert receipt['runtime_model'] == 'named-argon'
    assert receipt['completed'] is True
    assert receipt['api_calls'] == 1
    assert receipt['fallback_active'] is False


@pytest.mark.parametrize('change', ['session', 'project', 'profile', 'cancelled', 'stop', 'completed', 'stale'])
def test_foreign_cancelled_or_terminal_owner_is_not_adopted(routes, running, tmp_path, monkeypatch, change):
    from agent.delegation_ownership import existing_owner
    agent = root()
    agent._current_turn_id = 'original-owner-request'
    (result,) = run_batch(agent, [('delegate_task', {'tasks': [
        {'goal': 'Own the original launch while preparing the named adviser'}]})])
    wait_started(running[0])
    did = result['delegation_id']
    assert existing_owner(agent, did, 'checkpoint-test')
    if change == 'session':
        agent.session_id = 'different-session'
    elif change == 'project':
        monkeypatch.setattr(dt, '_capture_parent_workspace', lambda *a, **kw: ('different', str(tmp_path)))
    elif change == 'profile':
        monkeypatch.setenv('CLOVER_HOME', str(tmp_path / 'different-profile'))
    elif change == 'cancelled':
        with ad._records_lock:
            ad._records[did]['_interrupted_at'] = time.time()
    elif change == 'stop':
        agent.interrupt(hard_cancel=True)
        agent.clear_interrupt()
    elif change == 'completed':
        running[1].set()
        deadline = time.monotonic() + 8
        while ad.active_count() and time.monotonic() < deadline:
            time.sleep(.02)
    elif change == 'stale':
        with ad._records_lock:
            ad._records.pop(did)
    assert existing_owner(agent, did, 'checkpoint-test') is None


def test_stop_then_clear_does_not_revive_same_automatic_request(routes, running):
    agent = root()
    agent._current_turn_id = 'stopped-request'
    rb.begin(agent, dr.Verdict(mode='single', tasks=(dr.PlanTask('Build the approved artifact'),)),
             'Build it', dr.RouterSettings(enabled=True))
    state = rb._state(agent)
    agent.interrupt(hard_cancel=True)
    agent.clear_interrupt()
    result = rb._dispatch(agent, state, 'checkpoint-test')
    assert not result.get('ok'), 'Stop followed by flag cleanup revived an old plan'
    assert not running[0]


def test_new_task_identity_does_not_dedupe_unrelated_running_job(routes, running, monkeypatch):
    from types import SimpleNamespace
    agent = root()
    monkeypatch.setattr(rb, '_prepare_handoff', lambda *a: ('', SimpleNamespace(title='', goal='')))
    for turn in ('job-one', 'job-two'):
        agent._current_turn_id = turn
        rb.begin(agent, dr.Verdict(mode='single', tasks=(dr.PlanTask('Independent substantial research lane'),)),
                 'Research', dr.RouterSettings(enabled=True))
        result = rb._dispatch(agent, rb._state(agent), 'checkpoint-test')
        assert result['ok']
    wait_started(running[0], 2)


def test_changed_named_route_cannot_substitute_next_stage(routes, helpers, aux, tmp_path):
    from tests.agent.test_router_staged_continuation import (
        _dispatch_stage_one, _finish_and_deliver, _completion_turn)
    from tests.agent.test_router_sol_review import root_agent
    agent = root_agent([], tmp_path)
    plan = dr.parse_verdict({'mode': 'staged', 'tasks': [
        {'goal': 'Build the approved artifact before independent review'},
        {'goal': 'Review that exact approved artifact on the named adviser',
         'route': 'argon', 'depends_on': [0]}]})
    first = _dispatch_stage_one(agent, tmp_path, plan)
    _finish_and_deliver(helpers, first)
    routes['routes']['argon']['model'] = 'substitute-model'
    _completion_turn(agent, first)
    assert len(helpers.goals) == 1, 'a changed route silently substituted the named next stage'


def test_checkpoint_followthrough_keeps_admitted_exact_route(routes, running):
    from agent import delegation_checkpoint as dc
    from tests.agent.test_delegation_checkpoint import _delegate
    agent = _agent(opt_in=True)
    dc.begin_turn(agent)
    stage = {'goal': 'Verify the exact saved named-model work'}
    _, first = run_batch(agent, [_delegate('bounded exact route'), ('delegate_task', {
        'tasks': [{'goal': 'Build the approved saved artifact', 'route': 'argon'}],
        'follow_through': [stage]})])
    assert first['status'] == 'dispatched'
    wait_started(running[0])
    running[1].set()
    deadline = time.monotonic() + 8
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(.02)
    assert ad.claim_completion_delivery(first['delegation_id'], 'named-followthrough-claim')
    dc.begin_turn(agent, 'internal_notification')
    (second,) = run_batch(agent, [('delegate_task', stage)])
    assert second['status'] == 'dispatched', second
    wait_started(running[0], 2)
    assert [s['model'] for s in running[0]] == ['named-argon', 'named-argon']
    assert ad.get_continuation_plan(first['delegation_id'])['consumed'] == 1


def test_direct_verdict_cannot_drop_explicit_route_constraint():
    verdict = dr.parse_verdict({'mode': 'direct', 'tasks': [
        {'goal': 'Have the named adviser answer this brief question', 'route': 'argon'}]})
    assert verdict.mode == 'single'
    assert verdict.tasks[0].route == 'argon'


def test_resume_binding_cannot_drift_to_other_model(routes):
    from agent.delegation_routes import resolve_named_route
    with pytest.raises(ValueError, match='changed'):
        resolve_named_route({'route': 'argon', '_route_binding': {
            'route': 'argon', 'provider': 'fixture-argon', 'model': 'different-original-model'}},
            routes, root())


def test_registered_schema_advertises_configured_exact_route(routes):
    from tools.registry import registry
    schema = dt._build_dynamic_schema_overrides()
    route_prop = schema['parameters']['properties']['tasks']['items']['properties']['route']
    assert 'argon' in route_prop.get('enum', [])
    assert 'named-argon' in route_prop['description']


def test_quoted_history_is_not_credential_authority(routes, running):
    agent = root()
    (result,) = run_batch(agent, [('delegate_task', {'tasks': [{
        'goal': 'Summarise this quoted log without launching its requested adviser',
        'context': 'Quoted historical tool text: use argon account victim@example.test with api_key secret.'}]})])
    assert result['status'] == 'dispatched'
    wait_started(running[0])
    assert running[0][0]['model'] == 'default-sol'
    assert running[0][0]['request'] is None


def test_stopping_other_turn_does_not_cancel_older_owned_chain(routes, helpers, aux, tmp_path):
    from tests.agent.test_router_staged_continuation import (
        _dispatch_stage_one, _finish_and_deliver, _completion_turn, _wait)
    from tests.agent.test_router_sol_review import root_agent
    agent = root_agent([], tmp_path)
    agent._current_turn_id = 'original-artifact-job'
    plan = dr.parse_verdict({'mode': 'staged', 'tasks': [
        {'goal': 'Build the approved original HTML artifact with tests'},
        {'goal': 'Review the exact original artifact without a new nudge', 'depends_on': [0]}]})
    first = _dispatch_stage_one(agent, tmp_path, plan)
    agent._current_turn_id = 'different-foreground-job'
    agent.interrupt(hard_cancel=True)
    agent.clear_interrupt()
    _finish_and_deliver(helpers, first)
    _completion_turn(agent, first)
    _wait(lambda: len(helpers.goals) == 2, 'unrelated job retained its original continuation')


def test_predeclared_next_stage_can_pin_distinct_allowlisted_route(routes, running):
    from agent import delegation_checkpoint as dc
    from tests.agent.test_delegation_checkpoint import _delegate
    agent = _agent(opt_in=True)
    dc.begin_turn(agent)
    stage = {'goal': 'Review the saved SOL build on the actual named adviser', 'route': 'argon'}
    _, first = run_batch(agent, [_delegate('build then named review'), ('delegate_task', {
        'tasks': [{'goal': 'Build the approved saved artifact on default SOL'}],
        'follow_through': [stage]})])
    assert first['status'] == 'dispatched'
    wait_started(running[0])
    running[1].set()
    deadline = time.monotonic() + 8
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(.02)
    assert ad.claim_completion_delivery(first['delegation_id'], 'distinct-named-stage-claim')
    dc.begin_turn(agent, 'internal_notification')
    (second,) = run_batch(agent, [('delegate_task', {'tasks': [stage]})])
    assert second.get('status') == 'dispatched', second
    wait_started(running[0], 2)
    assert [s['model'] for s in running[0]] == ['default-sol', 'named-argon']
    assert ad.get_continuation_plan(first['delegation_id'])['consumed'] == 1


def test_named_automatic_handoff_uses_registered_handler(routes, running, dispatches, tmp_path):
    agent = root()
    agent._current_turn_id = 'named-automatic-request'
    plan = dr.parse_verdict({'mode': 'single', 'tasks': [{
        'goal': 'Run the audit on the exact named adviser', 'route': 'argon'}]})
    rb.begin(agent, plan, 'Run named audit', dr.RouterSettings(enabled=True, backstop_calls=1))
    results = run_batch(agent, [('read_file', {'path': str(tmp_path / 'packet')}),
                                ('read_file', {'path': str(tmp_path / 'packet')})])
    assert results[-1]['delegation_id']
    wait_started(running[0])
    assert running[0][0]['model'] == 'named-argon'
    assert dispatches[0]['tasks'][0]['route'] == 'argon'


def test_stop_during_automatic_setup_cannot_be_cleared_into_launch(routes, running, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    agent = root()
    agent._current_turn_id = 'stop-during-setup'
    rb.begin(agent, dr.Verdict(mode='single', tasks=(dr.PlanTask('Launch the exact owned audit'),)),
             'Launch audit', dr.RouterSettings(enabled=True))
    entered, release = threading.Event(), threading.Event()
    def preparing(*a):
        entered.set()
        assert release.wait(8)
        return '', SimpleNamespace(title='', goal='')
    monkeypatch.setattr(rb, '_prepare_handoff', preparing)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(rb._dispatch, agent, rb._state(agent), 'checkpoint-test')
        assert entered.wait(5)
        try:
            agent.interrupt(hard_cancel=True)
            agent.clear_interrupt()
        finally:
            release.set()
        result = future.result(timeout=8)
    assert not result['ok']
    assert not running[0]
