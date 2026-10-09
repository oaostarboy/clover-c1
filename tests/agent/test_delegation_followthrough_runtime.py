"""Bounded continuation contracts through the real executor and async SQLite path."""
import json
import socket
import threading
import time

import pytest
from tests.agent.test_delegation_checkpoint import _agent, _delegate, run_batch
from agent import delegation_checkpoint as dc
from tools import async_delegation as asyncd


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    import run_agent
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline')))
    release = threading.Event()
    started = []
    def conversation(self, user_message=None, **kw):
        started.append(user_message)
        assert release.wait(10)
        return {'final_response': 'test-owned result', 'messages': [], 'completed': True, 'api_calls': 1}
    monkeypatch.setattr(run_agent.AIAgent, 'run_conversation', conversation)
    agent = _agent()
    dc.begin_turn(agent)
    stage = {'goal': 'verify exact saved work', 'context': 'no additional permissions'}
    _, result = run_batch(agent, [_delegate('bounded plan'), ('delegate_task', {'goal': 'build saved work', 'follow_through': [stage]})])
    assert result['status'] == 'dispatched'
    did = result['delegation_id']
    assert asyncd.get_continuation_plan(did)
    def complete(claim=True):
        release.set()
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            row=asyncd.get_durable_delegation(did)
            if row and row['state'] not in ('running','finalizing'):
                break
            time.sleep(.01)
        assert row and row['state'] not in ('running','finalizing')
        if claim: assert asyncd.claim_completion_delivery(did, 'test-owned-claim')
    try:
        yield agent, stage, did, started, complete
    finally:
        release.set()
        deadline=time.monotonic()+10
        while asyncd.active_count() and time.monotonic()<deadline:
            time.sleep(.01)
        assert not asyncd.active_count(), 'test must collect all owned async work'


@pytest.mark.parametrize('extra', [
    {'goal': 'appended authority'}, {'model':'another-model'}, {'toolsets':['terminal']},
    {'follow_through':[{'goal':'third-stage'}]}, {'context':'changed context'}, {'tasks':[{'goal':'another'}]},
])
def test_callback_cannot_change_predeclared_payload(admitted, extra):
    agent,stage,did,started,complete=admitted
    complete()
    dc.begin_turn(agent,'internal_notification')
    before=len(started)
    (result,)=run_batch(agent,[('delegate_task',{**stage,**extra})])
    assert result['error_type']=='delegation_spawn_closed'
    assert len(started)==before
    assert asyncd.get_continuation_plan(did)['consumed']==0


@pytest.mark.parametrize('kind', ['internal_notification','async_delegation_complete',None])
def test_unclaimed_receipt_never_grants_continuation(admitted,kind):
    agent,stage,did,started,complete=admitted
    complete(claim=False)
    dc.begin_turn(agent,kind)
    assert dc.get_checkpoint(agent).window is None
    assert dc.get_checkpoint(agent).expected_followthrough_policy() is None


def test_accepted_old_plan_does_not_close_new_request_budget(admitted):
    agent,stage,did,started,complete=admitted
    checkpoint=dc.get_checkpoint(agent)
    dc.begin_turn(agent)  # an unrelated human question
    checkpoint.declare('direct','answer unrelated question')
    before=checkpoint.snapshot()
    complete()
    dc.begin_turn(agent,'internal_notification')
    (result,)=run_batch(agent,[('delegate_task',stage)])
    assert result['status']=='dispatched'
    after=checkpoint.snapshot()
    assert (after['request_id'],checkpoint.phase,after['used'],after['decision']) == (before['request_id'],before['phase'],before['used'],before['decision'])
    assert asyncd.get_continuation_plan(did)['consumed']==1
    (duplicate,)=run_batch(agent,[('delegate_task',stage)])
    assert duplicate['error_type']=='delegation_spawn_closed'


@pytest.mark.parametrize('boundary',['child_stop','owner_close'])
def test_matching_stop_or_owner_close_revokes_plan(admitted,boundary):
    agent,stage,did,started,complete=admitted
    checkpoint=dc.get_checkpoint(agent)
    owned=checkpoint._owned[did]
    if boundary=='child_stop':
        assert checkpoint.revoke_followthrough(owned.subagent_ids[0])
    else:
        # Invoke the actual owner cleanup entry; all resources are test-owned.
        agent.close()
    assert asyncd.get_continuation_plan(did)['cancelled'] is True
    complete()
    dc.begin_turn(agent,'internal_notification')
    (result,)=run_batch(agent,[('delegate_task',stage)])
    assert result['error_type']=='delegation_spawn_closed'


@pytest.mark.parametrize('hard_cancel',[False,True])
def test_real_interrupt_distinguishes_stop_from_new_question(admitted,hard_cancel):
    agent,stage,did,started,complete=admitted
    agent.interrupt('unrelated question' if not hard_cancel else None,hard_cancel=hard_cancel)
    plan=asyncd.get_continuation_plan(did)
    assert bool(plan.get('cancelled')) is hard_cancel


@pytest.mark.parametrize('field,value',[
    ('state','unknown'), ('state','error'), ('state','interrupted'),
    ('delivery_state','dropped'),
])
def test_uncertain_or_dropped_completion_cannot_launch(admitted,field,value):
    agent,stage,did,started,complete=admitted
    complete()
    # Tamper with an actual test-owned persisted completion, never mint a receipt.
    with asyncd._DB_LOCK, asyncd._transaction() as conn:
        conn.execute(f'UPDATE async_delegations SET {field}=? WHERE delegation_id=?',(value,did))
    dc.begin_turn(agent,'internal_notification')
    before=len(started)
    (result,)=run_batch(agent,[('delegate_task',stage)])
    assert result['error_type']=='delegation_spawn_closed'
    assert len(started)==before


@pytest.mark.parametrize('mutation',['expiry','lineage','receipt','generation','consumed','missing'])
def test_durable_plan_must_keep_exact_runtime_binding(admitted,mutation):
    agent,stage,did,started,complete=admitted
    complete()
    plan=asyncd.get_continuation_plan(did)
    if mutation=='expiry': plan['expires_at']=0
    elif mutation=='lineage': plan['request_id']='foreign-request'
    elif mutation=='receipt': plan['receipt_ids']=['foreign-receipt']
    elif mutation=='generation': plan['generation']=-1
    elif mutation=='consumed': plan['consumed']=1
    with asyncd._DB_LOCK, asyncd._transaction() as conn:
        conn.execute('UPDATE async_delegations SET continuation_json=? WHERE delegation_id=?',(None if mutation=='missing' else json.dumps(plan),did))
    dc.begin_turn(agent,'internal_notification')
    before=len(started)
    (result,)=run_batch(agent,[('delegate_task',stage)])
    assert result['error_type']=='delegation_spawn_closed'
    assert len(started)==before


def test_stop_after_completion_revokes_pending_next_stage(admitted):
    agent,stage,did,started,complete=admitted
    sid=dc.get_checkpoint(agent)._owned[did].subagent_ids[0]
    complete()
    (stopped,)=run_batch(agent,[('delegate_task',{'action':'stop','subagent_id':sid})])
    assert stopped.get('status')=='continuation_cancelled'
    assert asyncd.get_continuation_plan(did)['cancelled'] is True
    dc.begin_turn(agent,'internal_notification')
    (result,)=run_batch(agent,[('delegate_task',stage)])
    assert result['error_type']=='delegation_spawn_closed'


def test_foreign_stop_cannot_revoke_owned_plan(admitted):
    agent,stage,did,started,complete=admitted
    (result,)=run_batch(agent,[('delegate_task',{'action':'stop','subagent_id':'foreign-child'})])
    assert result.get('error')
    assert not asyncd.get_continuation_plan(did).get('cancelled')


def test_policy_drift_cannot_start_next_stage(admitted):
    agent,stage,did,started,complete=admitted
    complete()
    dc.begin_turn(agent,'internal_notification')
    agent.model='test-policy-drift'
    before=len(started)
    (result,)=run_batch(agent,[('delegate_task',stage)])
    assert 'cannot change' in result['error']
    assert len(started)==before
    assert asyncd.get_continuation_plan(did)['consumed']==1


# ── model-natural call shapes of the SAME stored stage ──────────────────────
# The production incident (deleg_e2ad1329): the stored stage was {goal, context}
# and the callback repeated exactly that text, but wrapped it the way every
# ordinary spawn is wrapped: ``tasks=[{goal, context}]`` plus the user-facing
# ``handoff``. The window guard compared raw argument keys and closed it.
_HANDOFF = {'work': 'verifying results', 'outcome': 'independently checked results'}


def _shape(stage, name):
    if name == 'top_level': return dict(stage)
    if name == 'top_level_handoff': return {**stage, 'handoff': _HANDOFF}
    if name == 'tasks': return {'tasks': [dict(stage)]}
    if name == 'tasks_handoff': return {'tasks': [dict(stage)], 'handoff': _HANDOFF}
    if name == 'empty_tasks': return {'tasks': [], **stage}
    if name == 'tasks_title': return {'tasks': [{**stage, 'title': 'Verify results'}], 'handoff': _HANDOFF}
    raise AssertionError(name)


@pytest.mark.parametrize('shape', ['top_level', 'top_level_handoff', 'tasks', 'tasks_handoff', 'empty_tasks', 'tasks_title'])
def test_stored_stage_starts_once_in_every_natural_call_shape(admitted, shape):
    agent, stage, did, started, complete = admitted
    complete()
    dc.begin_turn(agent, 'internal_notification')
    before = len(started)
    (result,) = run_batch(agent, [('delegate_task', _shape(stage, shape))])
    assert result.get('status') == 'dispatched', result
    assert asyncd.get_continuation_plan(did)['consumed'] == 1
    deadline = time.monotonic() + 10
    while len(started) == before and time.monotonic() < deadline:
        time.sleep(.01)
    assert len(started) == before + 1
    assert stage['goal'] in started[-1]
    (duplicate,) = run_batch(agent, [('delegate_task', _shape(stage, shape))])
    assert duplicate['error_type'] == 'delegation_spawn_closed'
    assert asyncd.get_continuation_plan(did)['consumed'] == 1


_G = {'goal': 'verify exact saved work', 'context': 'no additional permissions'}


@pytest.mark.parametrize('bad', [
    {'tasks': [{**_G, 'tier': 'code'}]},
    {'tasks': [{**_G, 'model': 'x'}]},
    {'tasks': [{**_G, 'toolsets': ['terminal']}]},
    {'tasks': [{**_G, 'context': 'changed'}]},
    {'tasks': [dict(_G), {'goal': 'extra'}]},
    {'tasks': [dict(_G)], 'goal': _G['goal']},
    {'tasks': dict(_G)},
    {'tasks': [dict(_G)], 'handoff': 'not-an-object'},
    {'tasks': [dict(_G)], 'follow_through': [{'goal': 'third'}]},
    {'tasks': [dict(_G)], 'max_iterations': 99},
    {**_G, 'handoff': {'work': 'x', 'extra': 'y'}},
])
def test_wrapped_shapes_still_fail_closed_before_construction(admitted, bad):
    agent, stage, did, started, complete = admitted
    complete()
    dc.begin_turn(agent, 'internal_notification')
    before = len(started)
    (result,) = run_batch(agent, [('delegate_task', bad)])
    assert result['error_type'] == 'delegation_spawn_closed'
    assert len(started) == before
    assert asyncd.get_continuation_plan(did)['consumed'] == 0
