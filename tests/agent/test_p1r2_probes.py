"""Round-2 review probes for C1.4 P1 fix round (ae20de6c). Lives in /tmp only.

Real AIAgent roots/children, real delegate_task entry, real CredentialPool,
real CodexAppServerSession with the scripted FakeClient. Only child
construction (and, for the batch probe, the child runner) is stubbed.
"""
import json
import random
import threading
import time

import pytest

from agent import foreground_time_limit as fl
from tests.agent.test_foreground_time_limit_loop import (  # noqa: F401
    clock, provider, _limit_on, _write_limit, make_agent, text, tool, Provider,
)

_CREDS = {'model': 'm', 'provider': None, 'base_url': None, 'api_key': None,
          'api_mode': None, 'command': None, 'args': None}


def _pool(provider):
    from agent.credential_pool import CredentialPool, PooledCredential

    entry = PooledCredential(id='cred-1', provider='openai-compat', auth_type='api_key',
                             access_token='k', label='cred-1', source='manual',
                             priority=0, base_url=provider.base_url,
                             inference_base_url=provider.base_url)
    return CredentialPool('openai-compat', [entry])


def _stateless():
    import gateway.session_context as sc

    sc.declare_stateless_channel()


@pytest.fixture(autouse=True)
def _unbind_session_context():
    yield
    import gateway.session_context as sc

    for var in sc._VAR_MAP.values():
        var.set(sc._UNSET)
    sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)


# ── 1. batch aggregation abandons a wedged child at the episode deadline ───

def test_batch_abandons_wedged_child_interrupts_it_and_keeps_results_attributed(
        clock, provider, monkeypatch):
    # FIX ROUND 2 (parent decision, owner agreed): the foreground limit is OFF
    # on a no-async surface and the batch-abandonment machinery is removed.
    # The batch is the unchanged base behaviour: it waits for every child and
    # reports each result attributed to its own task index. (Was: the wedged
    # child was abandoned at the episode deadline.)
    import tools.delegate_tool as dt

    _stateless()
    root = make_agent(provider, platform='api_server')
    fl.begin_turn(root)
    assert fl.remaining_seconds(root) is None, 'limit must be off here'
    clock.advance(299.8)

    children = [make_agent(Provider(), platform='subagent') for _ in range(2)]
    for i, c in enumerate(children):
        c._delegate_depth, c._subagent_id, c._delegate_role = 1, f'r2-batch-{i}', 'leaf'

    def build(**kw):
        child = children[kw['task_index']]
        kw['parent_agent']._active_children.append(child)
        return child

    entered, release = threading.Event(), threading.Event()

    def run_child(task_index, goal, child, parent_agent, **kw):
        if task_index == 1:
            entered.set()
            release.wait(15)
            return {'task_index': 1, 'status': 'completed', 'summary': 'LATE ONE',
                    'api_calls': 1, 'duration_seconds': 0.0, 'model': 'm',
                    'exit_reason': 'completed'}
        return {'task_index': 0, 'status': 'completed', 'summary': 'FAST ZERO',
                'api_calls': 1, 'duration_seconds': 0.0, 'model': 'm',
                'exit_reason': 'completed'}

    monkeypatch.setattr(dt, '_build_child_agent', build)
    monkeypatch.setattr(dt, '_run_single_child', run_child)
    monkeypatch.setattr(dt, '_resolve_delegation_credentials', lambda *a, **k: _CREDS)

    out, done = {}, threading.Event()

    def run():
        _stateless()
        try:
            out['raw'] = dt.delegate_task(parent_agent=root, tasks=[
                {'goal': 'Review the first independent component carefully'},
                {'goal': 'Review the second independent component carefully'}])
        except BaseException as exc:  # noqa: BLE001
            out['exception'] = repr(exc)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    assert entered.wait(10)
    try:
        assert not done.wait(1.0), 'base behaviour: the batch waits for the slow child'
    finally:
        release.set()
    assert done.wait(15), out
    assert 'exception' not in out, out
    result = json.loads(out['raw'])
    by_idx = {r['task_index']: r for r in result['results']}
    assert by_idx[0]['summary'] == 'FAST ZERO' and by_idx[1]['summary'] == 'LATE ONE'
    assert all(r['status'] == 'completed' for r in by_idx.values())
    assert not any(getattr(c, '_interrupt_requested', False) for c in children)


# ── 2. detached teardown: lease released exactly once, children list sync ──

def _teardown_probe(provider, monkeypatch, *, limit_on: bool):
    import tools.delegate_tool as dt

    _stateless()
    root = make_agent(provider, platform='api_server')
    if not limit_on:
        _write_limit(0)
    fl.begin_turn(root)
    # FIX ROUND 2: a stateless (no async delivery) surface is never limited,
    # whatever the config says; limit_on only selects the config.
    assert fl.remaining_seconds(root) is None

    child_provider = Provider()
    child_provider.queue[:] = [text('child result')]
    child = make_agent(child_provider, platform='subagent')
    child._delegate_depth, child._subagent_id, child._delegate_role = 1, 'r2-teardown', 'leaf'
    pool = _pool(child_provider)
    child._credential_pool = pool

    releases: list = []
    real_release = pool.release_lease

    def release_lease(cred_id):
        releases.append(cred_id)
        return real_release(cred_id)

    monkeypatch.setattr(pool, 'release_lease', release_lease)

    close_entered, close_release = threading.Event(), threading.Event()
    closed = []
    real_close = child.close

    def slow_close():
        close_entered.set()
        close_release.wait(15)
        closed.append(True)
        return real_close()

    monkeypatch.setattr(child, 'close', slow_close)

    def build(**kw):
        kw['parent_agent']._active_children.append(child)
        return child

    monkeypatch.setattr(dt, '_build_child_agent', build)
    monkeypatch.setattr(dt, '_resolve_delegation_credentials', lambda *a, **k: _CREDS)

    out, done = {}, threading.Event()

    def run():
        _stateless()
        try:
            out['raw'] = dt.delegate_task(parent_agent=root,
                                          goal='Inspect the component and verify the result')
        except BaseException as exc:  # noqa: BLE001
            out['exception'] = repr(exc)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    try:
        assert close_entered.wait(15), out
        returned_while_closing = done.wait(2.0)
        snapshot = (list(releases), dict(pool._active_leases), list(root._active_children))
    finally:
        close_release.set()
    assert done.wait(15), out
    for _ in range(100):  # the detached tail is a daemon thread; let it land
        if closed and len(releases) >= 1:
            break
        time.sleep(0.05)
    child_provider.close()
    assert 'exception' not in out, out
    result = json.loads(out['raw'])
    entry = result['results'][0] if 'results' in result else result
    return returned_while_closing, snapshot, releases, pool, closed, entry, child, root


def _assert_pool_immediately_reusable(pool):
    # the pool is immediately reusable by the next child
    nxt = pool.acquire_lease()
    assert nxt == 'cred-1'
    pool.release_lease(nxt)
    assert pool._active_leases == {}


def test_limit_on_inline_child_teardown_is_detached_and_lease_released_once(provider, clock, monkeypatch):
    # FIX ROUND 2 (parent decision, owner agreed): "limit on" in config but a
    # no-async surface -> the limit is off for the turn and teardown is the
    # unchanged synchronous base behaviour; the lease is still released once.
    returned_while_closing, snapshot, releases, pool, closed, entry, child, root = \
        _teardown_probe(provider, monkeypatch, limit_on=True)
    assert not returned_while_closing, 'no limit applies: teardown stays synchronous'
    rel_at_return, leases_at_return, active_at_return = snapshot
    assert rel_at_return == ['cred-1'] and leases_at_return == {}
    assert child not in active_at_return
    assert closed == [True] and releases == ['cred-1']
    assert pool._active_leases == {}
    assert entry['status'] == 'completed' and entry['summary'] == 'child result', entry
    _assert_pool_immediately_reusable(pool)


def test_limit_off_inline_child_teardown_is_synchronous_as_before(provider, clock, monkeypatch):
    returned_while_closing, snapshot, releases, pool, closed, entry, child, root = \
        _teardown_probe(provider, monkeypatch, limit_on=False)
    assert not returned_while_closing, 'limit off: teardown must stay synchronous'
    rel_at_return, leases_at_return, active_at_return = snapshot
    assert rel_at_return == ['cred-1'] and leases_at_return == {}
    assert child not in active_at_return
    assert closed == [True] and releases == ['cred-1']
    assert entry['status'] == 'completed' and entry['summary'] == 'child result', entry
    _assert_pool_immediately_reusable(pool)


# ── 3. bounded lease acquisition: never leaks, never double-releases ───────

def test_bounded_lease_acquire_stress_releases_every_lease_exactly_once(provider):
    # FIX ROUND 2 (parent decision, owner agreed): the bounded lease helper is
    # gone. Surviving property under stress: the plain inline runner acquires
    # and releases each lease exactly once.
    import tools.delegate_tool as dt
    from types import SimpleNamespace

    assert not hasattr(dt, '_acquire_lease_bounded')
    pool = _pool(provider)
    acquired, released = [], []
    real_acquire, real_release = pool.acquire_lease, pool.release_lease
    pool.acquire_lease = lambda *a, **k: (acquired.append(1), real_acquire(*a, **k))[1]
    pool.release_lease = lambda cid: (released.append(cid), real_release(cid))[1]

    class Child:
        _subagent_id = 'r2-stress'
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

    parent = SimpleNamespace(_touch_activity=lambda *a, **k: None, _current_task_id=None,
                             _active_children=[], _active_children_lock=None)
    for _ in range(300):
        result = dt._run_single_child(task_index=0, goal='stress lease', child=Child(),
                                      parent_agent=parent)
        assert result['status'] == 'completed'
    assert len(acquired) == 300 and len(released) == 300
    assert pool._active_leases == {}, pool._active_leases


# ── 4. Codex app-server: deadline hit keeps the session usable ─────────────

def test_codex_deadline_hit_then_next_human_turn_is_clean(clock, provider, monkeypatch):
    from tests.agent.test_p1_astra_review import _codex_agent

    agent, client = _codex_agent(provider, clock, monkeypatch, slow=True)
    session = agent._codex_session
    original_take = client.take_notification.__wrapped__ if hasattr(
        client.take_notification, '__wrapped__') else None
    r1 = agent.run_conversation('long interactive task', task_id='r2-codex-1')

    assert r1['completed'] is True and r1.get('partial') is False, r1
    assert r1.get('turn_exit_reason') == 'foreground_time_limit_hard_stop', r1
    assert r1['final_response'], r1
    assert agent._codex_session is session, 'session was retired on a deadline hit'
    assert fl._active(agent) is None, 'turn flag still active after the Codex early return'
    roles1 = [m['role'] for m in r1['messages'] if m['role'] != 'system']
    assert roles1[-1] == 'assistant' and roles1[-2] == 'user', roles1
    assert sum(1 for m in r1['messages'] if m['role'] == 'assistant') == 1, roles1
    assert client.requests[-1][0] == 'turn/interrupt', client.requests[-3:]

    # The interrupted turn's leftovers (CODEX FINAL item + turn/completed for
    # turn-fake-001) are still queued. Second human turn gets a new turn id.
    def handler(method, params):
        if method == 'turn/start':
            return {'turn': {'id': 'turn-2'}}
        if method == 'turn/interrupt':
            return {}
        return {}

    client._request_handler = handler
    client.take_notification = type(client).take_notification.__get__(client)
    client.queue_notification('turn/started', threadId='t', turn={'id': 'turn-2'})
    client.queue_notification('item/completed', threadId='t', turnId='turn-2',
                              item={'type': 'agentMessage', 'id': 'm2', 'text': 'SECOND'})
    client.queue_notification('turn/completed', threadId='t',
                              turn={'id': 'turn-2', 'status': 'completed', 'error': None})
    n_before = len(r1['messages'])
    r2 = agent.run_conversation('next human message', task_id='r2-codex-2',
                                conversation_history=list(r1['messages']))

    assert r2['final_response'] == 'SECOND', r2
    assert r2['completed'] is True and not r2.get('turn_exit_reason'), r2
    assert agent._codex_session is session
    new_rows = r2['messages'][n_before:]
    assert [m['role'] for m in new_rows] == ['user', 'assistant'], new_rows
    assert new_rows[0]['content'] == 'next human message'
    assert new_rows[1]['content'] == 'SECOND'
    assert all(m.get('content') != 'CODEX FINAL' for m in r2['messages']), 'stale row leaked'
    roles = [m['role'] for m in r2['messages'] if m['role'] != 'system']
    assert all(a != b for a, b in zip(roles, roles[1:])), roles


def test_codex_turn_that_starts_spent_gets_the_finish_window(clock, provider, monkeypatch):
    from tests.agent.test_p1_astra_review import _codex_agent

    agent, client = _codex_agent(provider, clock, monkeypatch, slow=True)
    session = agent._codex_session
    agent.run_conversation('long interactive task', task_id='r2-codex-spent-1')
    assert agent._fg_episode is not None and agent._fg_episode.expired()

    seen = {}
    real_run_turn = session.run_turn

    def spy(*a, **kw):
        seen.update(kw)
        return real_run_turn(*a, **kw)

    monkeypatch.setattr(session, 'run_turn', spy)
    client._request_handler = lambda m, p: {'turn': {'id': 'turn-3'}} if m == 'turn/start' else {}
    client.take_notification = type(client).take_notification.__get__(client)
    client.queue_notification('turn/started', threadId='t', turn={'id': 'turn-3'})
    client.queue_notification('item/completed', threadId='t', turnId='turn-3',
                              item={'type': 'agentMessage', 'id': 'm3', 'text': 'NOTICE ANSWER'})
    client.queue_notification('turn/completed', threadId='t',
                              turn={'id': 'turn-3', 'status': 'completed', 'error': None})
    r = agent.run_conversation('[helper finished]', task_id='r2-codex-spent-2',
                               persist_user_display_kind='internal_notification')
    assert r['final_response'] == 'NOTICE ANSWER', r
    assert seen.get('foreground_deadline') is True, seen
    assert seen.get('turn_timeout') == pytest.approx(fl.HARD_STOP_GRACE_SECONDS, abs=1.0), seen
