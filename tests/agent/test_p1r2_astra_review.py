"""Round-two probes: real agent/pool/session, scripted provider seams only."""
import threading
import time
from types import SimpleNamespace
import pytest
from agent import foreground_time_limit as fl
from tests.agent.test_foreground_time_limit_loop import clock, provider, _limit_on, make_agent, text, tool

@pytest.mark.parametrize('batch', [False, True])
def test_post_lease_pool_lock_is_bounded(clock, provider, monkeypatch, batch):
    import tools.delegate_tool as dt
    import gateway.session_context as sc
    from agent.credential_pool import CredentialPool
    root = make_agent(provider, platform='api_server')
    children = [make_agent(provider, platform='subagent') for _ in range(2 if batch else 1)]
    pool = CredentialPool('openai-compat', [])
    acquired = threading.Event()
    locked = threading.Event()
    release = threading.Event()
    entered = threading.Event()
    done = threading.Event()
    outcome = {}
    real_acquire, real_current = pool.acquire_lease, pool.current
    def holder():
        assert acquired.wait(10)
        with pool._lock:
            locked.set()
            release.wait(15)
    holder_thread = threading.Thread(target=holder, daemon=True)
    holder_thread.start()
    def acquire():
        lease = real_acquire('review-credential')
        acquired.set()
        assert locked.wait(10)
        return lease
    def current():
        entered.set()
        return real_current()
    monkeypatch.setattr(pool, 'acquire_lease', acquire)
    monkeypatch.setattr(pool, 'current', current)
    for i, child in enumerate(children):
        child._credential_pool = pool
        child._delegate_depth = 1
        child._subagent_id = f'r2-lock-{i}'
        child._delegate_role = 'leaf'
    def build(**kw):
        child = children[kw['task_index']]
        kw['parent_agent']._active_children.append(child)
        return child
    monkeypatch.setattr(dt, '_build_child_agent', build)
    monkeypatch.setattr(dt, '_STALE_RESULT_GRACE_SECONDS', 0.05)
    monkeypatch.setattr(dt, '_resolve_delegation_credentials', lambda *a, **k: {
        'model': 'm', 'provider': None, 'base_url': None, 'api_key': None,
        'api_mode': None, 'command': None, 'args': None})
    args = {'tasks': [{'goal': 'Review this first component carefully'}, {'goal': 'Review this other component carefully'}]} if batch else {'goal': 'Review this component carefully and report'}
    provider.queue[:] = [tool('delegate_task', args, 'r2-d1')]
    def before(n, req):
        if n == 1:
            clock.advance(299.8)
        elif not req.get('tools'):
            provider.queue.insert(0, text('R2 FINAL'))
    provider.before = before
    def run():
        sc.declare_stateless_channel()
        try:
            outcome.update(root.run_conversation('review components', task_id='r2-lock'))
        except BaseException as exc:
            outcome['exception'] = repr(exc)
        finally:
            done.set()
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert entered.wait(10)
        clock.advance(100)
        bounded = done.wait(2.5)
        active_at_return = len(root._active_children)
    finally:
        release.set()
        holder_thread.join(10)
        worker.join(15)
    # FIX ROUND 2 (parent decision, owner agreed): the foreground limit is OFF
    # on a surface with no async delivery, so nothing bounds the inline path:
    # it is the unchanged base behaviour (the parent waits on the pool lock,
    # no abandonment, no hard stop). (Was: bounded + no retained children.)
    assert done.is_set(), outcome
    assert not bounded, 'no limit applies: the inline path waits as it always did'
    assert root._fg_episode is None or root._fg_episode.enabled is False
    assert outcome.get('turn_exit_reason') != fl.HARD_STOP_EXIT_REASON, outcome
    assert not any(c._interrupt_requested for c in children), 'nothing is abandoned'

@pytest.mark.parametrize('partial', [False, True])
def test_codex_expiry_interrupts_and_next_message_survives(clock, provider, monkeypatch, partial):
    from tests.agent.transports.test_codex_app_server_session import FakeClient, make_session
    import agent.transports.codex_app_server_session as sm
    monkeypatch.setattr(sm, 'time', SimpleNamespace(monotonic=clock, time=time.time, sleep=time.sleep))
    client = FakeClient()
    session = make_session(client)
    agent = make_agent(provider, platform='cli', api_mode='codex_app_server')
    agent._codex_session = session
    turns = []
    def request(method, params):
        if method == 'thread/start':
            return {'thread': {'id': 'r2-thread'}}
        if method == 'turn/start':
            turns.append(params['input'][0]['text'])
            tid = f'r2-{len(turns)}'
            if len(turns) == 1:
                if partial:
                    client.queue_notification('item/completed', threadId='r2-thread', turnId=tid,
                        item={'id': 'm1', 'type': 'agentMessage', 'text': 'still working'})
            else:
                client.queue_notification('item/completed', threadId='r2-thread', turnId=tid,
                    item={'id': 'm2', 'type': 'agentMessage', 'text': 'SECOND ANSWER'})
                client.queue_notification('turn/completed', threadId='r2-thread', turn={'id': tid, 'status': 'completed'})
            return {'turn': {'id': tid}}
        if method == 'turn/interrupt':
            client.queue_notification('turn/completed', threadId='r2-thread', turn={'id': params['turnId'], 'status': 'interrupted'})
        return {}
    client._request_handler = request
    take = client.take_notification
    def tick(timeout=0):
        note = take(timeout)
        clock.advance(1 if len(turns) > 1 else 151)
        return note
    client.take_notification = tick
    first = agent.run_conversation('FIRST MESSAGE', task_id='r2-codex1')
    interrupts = [p for m,p in client.requests if m == 'turn/interrupt']
    second = agent.run_conversation('SECOND MESSAGE', conversation_history=first['messages'], task_id='r2-codex2')
    assert turns == ['FIRST MESSAGE', 'SECOND MESSAGE']
    assert second['final_response'] == 'SECOND ANSWER'
    assert sum(m.get('content') == 'SECOND MESSAGE' for m in second['messages']) == 1
    assert sum(m.get('content') == 'SECOND ANSWER' for m in second['messages']) == 1
    assert agent._codex_session is session
    assert len(interrupts) == 1, f'live turn not interrupted at deadline: {first}'
    assert first.get('turn_exit_reason') == fl.HARD_STOP_EXIT_REASON


def test_late_real_lease_does_not_release_later_owners_lease():
    # FIX ROUND 2 (parent decision, owner agreed): the bounded/late lease
    # machinery is gone (inline children run only where the limit is off). The
    # surviving property: the inline runner takes a lease with the pool's plain
    # acquire_lease and releases exactly that lease once, never a later owner's.
    import tools.delegate_tool as dt
    from agent.credential_pool import CredentialPool
    assert not hasattr(dt, '_acquire_lease_bounded')
    pool = CredentialPool('openai-compat', [])
    calls = []
    acquire, release = pool.acquire_lease, pool.release_lease
    pool.acquire_lease = lambda *a, **k: acquire('shared')
    def tracked(cid):
        calls.append(cid)
        release(cid)
    pool.release_lease = tracked

    class Child:
        _subagent_id = 'r2-lease-owner'
        _delegate_depth = 1
        _delegate_role = 'leaf'
        model = 'm'
        _credential_pool = pool
        def get_activity_summary(self):
            return {'api_call_count': 1}
        def run_conversation(self, user_message, task_id=None, stream_callback=None,
                             conversation_history=None):
            return {'final_response': 'ok', 'completed': True, 'api_calls': 1}
        def interrupt(self, *a, **k):
            pass
        def close(self):
            pass

    other = acquire('shared')  # a later/other owner holds a lease meanwhile
    parent = SimpleNamespace(_touch_activity=lambda *a, **k: None, _current_task_id=None,
                             _active_children=[], _active_children_lock=None)
    result = dt._run_single_child(task_index=0, goal='lease owner check', child=Child(),
                                  parent_agent=parent)
    assert result['status'] == 'completed', result
    assert calls == ['shared']
    assert pool._active_leases == {'shared': 1}, 'the other owner\'s lease must survive'
    release(other)
    assert pool._active_leases == {}
