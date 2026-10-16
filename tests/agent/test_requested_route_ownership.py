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
        'existing_delegation_id': first['delegation_id']}]})
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
