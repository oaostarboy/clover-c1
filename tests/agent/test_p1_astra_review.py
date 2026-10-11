"""Independent P1 review probes. Copy this file into tests/agent on b682549a.
Run only with tools/safe_test_runner.py, one file per invocation.
Real AIAgent, conversation loop, executor and delegate entry; fake providers.
"""
import contextvars
import json
import threading
import time
from types import SimpleNamespace

import pytest

from agent import foreground_time_limit as fl
from tests.agent.test_foreground_time_limit_loop import (
    clock, provider, _limit_on, make_agent, text, tool, TODO, Provider,
    advance_per_tool_round, tool_texts,
)


@pytest.mark.parametrize('batch', [False, True])
def test_inline_deadline_covers_credential_lease_queue(clock, provider, monkeypatch, batch):
    """A real credential-pool lock cannot extend the parent's inline deadline.

    Hold the pool lock just as an in-progress credential refresh can. Release
    it in finally so the RED test itself never leaves a hung worker behind.
    Neither the delegate entry, executor, child runner nor conversation loop
    is mocked. Child construction returns preconstructed real AIAgents.
    """
    import tools.delegate_tool as dt
    import gateway.session_context as sc
    from agent.credential_pool import CredentialPool

    root = make_agent(provider, platform='api_server')
    children = [make_agent(provider, platform='subagent') for _ in range(2 if batch else 1)]
    pool = CredentialPool('openai-compat', [])
    entered = threading.Event()
    done = threading.Event()
    result = {}
    real_acquire = pool.acquire_lease

    def acquire(*args, **kwargs):
        entered.set()
        return real_acquire(*args, **kwargs)

    monkeypatch.setattr(pool, 'acquire_lease', acquire)
    for i, child in enumerate(children):
        child._delegate_depth = 1
        child._subagent_id = f'astra-lease-{i}'
        child._delegate_role = 'leaf'
        child._credential_pool = pool

    def build(**kw):
        child = children[kw['task_index']]
        kw['parent_agent']._active_children.append(child)
        return child

    monkeypatch.setattr(dt, '_build_child_agent', build)
    monkeypatch.setattr(dt, '_resolve_delegation_credentials', lambda *a, **k: {
        'model': 'm', 'provider': None, 'base_url': None, 'api_key': None,
        'api_mode': None, 'command': None, 'args': None})
    args = {'tasks': [{'goal': 'Review one independent component carefully'},
                      {'goal': 'Review another independent component carefully'}]} if batch else {
                          'goal': 'Review this component and return the verified result'}
    provider.queue[:] = [tool('delegate_task', args, 'd1')]

    def before(n, req):
        if n == 1:
            clock.advance(299.8)
        elif not req.get('tools'):
            provider.queue.insert(0, text('FINAL AFTER DEADLINE'))

    provider.before = before

    def run():
        sc.declare_stateless_channel()
        try:
            result.update(root.run_conversation('review the components', task_id='astra-lease'))
        except BaseException as exc:
            result['exception'] = repr(exc)
        finally:
            done.set()

    pool._lock.acquire()
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    returned_while_locked = False
    try:
        assert entered.wait(10), 'probe did not reach the real credential lease'
        clock.advance(100)  # now past BOTH episode expiry and hard-stop grace
        returned_while_locked = done.wait(1.0)
        observed = (len(provider.captured), fl.remaining_seconds(root), dict(result))
    finally:
        pool._lock.release()
        worker.join(15)
    # FIX ROUND 2 (parent decision, owner agreed): the foreground limit is OFF
    # on a surface with no async delivery (API server without a wake id), so
    # there is no deadline to bound and the inline path is the unchanged base
    # behaviour: the parent waits for the lock like it always did, and no
    # hard stop happens. (Was: the parent had to reach its hard stop.)
    assert done.is_set(), result
    assert not returned_while_locked, observed
    assert root._fg_episode is None or root._fg_episode.enabled is False
    assert result.get('turn_exit_reason') != fl.HARD_STOP_EXIT_REASON, result


def test_inline_timeout_does_not_wait_for_child_resource_cleanup(clock, provider, monkeypatch):
    import tools.delegate_tool as dt
    import tools.terminal_tool as tt
    import gateway.session_context as sc

    child_provider = Provider()
    root = make_agent(provider, platform='api_server')
    child = make_agent(child_provider, platform='subagent')
    child._delegate_depth = 1
    child._subagent_id = 'astra-cleanup-child'
    child._delegate_role = 'leaf'
    cleanup_entered = threading.Event()
    cleanup_release = threading.Event()
    provider_release = threading.Event()
    done = threading.Event()
    result = {}

    class SlowBackend:
        # Models a stuck backend teardown at the existing terminal adapter seam.
        # AIAgent.close and cleanup_vm themselves remain real.
        def cleanup(self):
            cleanup_entered.set()
            cleanup_release.wait(15)

    tt._active_environments[child.session_id] = SlowBackend()
    child_provider.queue[:] = [text('child result')]
    child_provider.before = lambda n, req: provider_release.wait(15)

    def build(**kw):
        kw['parent_agent']._active_children.append(child)
        return child

    monkeypatch.setattr(dt, '_build_child_agent', build)
    monkeypatch.setattr(dt, '_STALE_RESULT_GRACE_SECONDS', 0.05)
    monkeypatch.setattr(dt, '_resolve_delegation_credentials', lambda *a, **k: {
        'model': 'm', 'provider': None, 'base_url': None, 'api_key': None,
        'api_mode': None, 'command': None, 'args': None})
    provider.queue[:] = [tool('delegate_task', {'goal': 'Inspect the component and verify the result'}, 'd1')]

    def before(n, req):
        if n == 1:
            clock.advance(299.8)
        elif not req.get('tools'):
            provider.queue.insert(0, text('FINAL AFTER CLEANUP'))

    provider.before = before

    def run():
        sc.declare_stateless_channel()
        try:
            result.update(root.run_conversation('inspect component', task_id='astra-cleanup'))
        except BaseException as exc:
            result['exception'] = repr(exc)
        finally:
            done.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    # FIX ROUND 2 (parent decision, owner agreed): the foreground limit is OFF
    # on a no-async surface, so the inline child has NO episode timeout and its
    # teardown stays synchronous -- the unchanged base behaviour. (Was: the
    # child timed out and the parent had to return without waiting on cleanup.)
    returned_early = returned_during_cleanup = False
    try:
        clock.advance(100)
        returned_early = done.wait(1.0)  # child is still working: parent waits
        provider_release.set()
        assert cleanup_entered.wait(10), 'probe did not reach actual child.close/cleanup_vm'
        returned_during_cleanup = done.wait(1.0)  # teardown is synchronous
    finally:
        cleanup_release.set()
        provider_release.set()
        worker.join(15)
        child_provider.close()
    assert done.is_set(), result
    assert not returned_early, 'no limit applies: the parent waits for the inline child'
    assert not returned_during_cleanup, 'base behaviour: teardown is synchronous'
    assert root._fg_episode is None or root._fg_episode.enabled is False
    assert result.get('turn_exit_reason') != fl.HARD_STOP_EXIT_REASON, result


def _codex_agent(provider, clock, monkeypatch, *, slow):
    from tests.agent.transports.test_codex_app_server_session import FakeClient, make_session
    import agent.transports.codex_app_server_session as session_mod
    # Keep the adapter's own 600s timer on the same scripted monotonic clock.
    monkeypatch.setattr(session_mod, 'time', SimpleNamespace(
        monotonic=clock, time=time.time, sleep=time.sleep))
    client = FakeClient()
    client.queue_notification('turn/started', threadId='t', turn={'id': 'tu1'})
    for i in range(8 if slow else 0):
        client.queue_notification('item/agentMessage/delta', threadId='t', turnId='tu1',
                                  itemId='m1', delta='working ')
    client.queue_notification('item/completed', threadId='t', turnId='tu1',
                              item={'type': 'agentMessage', 'id': 'm1', 'text': 'CODEX FINAL'})
    client.queue_notification('turn/completed', threadId='t',
                              turn={'id': 'tu1', 'status': 'completed', 'error': None})
    take = client.take_notification

    def advance(timeout=0):
        message = take(timeout=timeout)
        if message is not None and slow:
            clock.advance(45)
        return message

    client.take_notification = advance
    agent = make_agent(provider, platform='cli', api_mode='codex_app_server')
    agent._codex_session = make_session(client)
    return agent, client


def test_codex_app_server_interactive_turn_obeys_foreground_deadline(clock, provider, monkeypatch):
    agent, client = _codex_agent(provider, clock, monkeypatch, slow=True)
    started = clock.t
    result = agent.run_conversation('long interactive task', task_id='astra-codex')
    assert fl.applies(agent), 'this is an in-scope root interactive agent'
    assert result['final_response'], result
    assert clock.t - started <= 360, (
        f'foreground episode elapsed={clock.t-started}; finish_iterations='
        f'{agent._fg_episode.finish_iterations}; native requests={client.requests}')


def test_codex_early_return_clears_foreground_active_flag(clock, provider, monkeypatch):
    agent, _ = _codex_agent(provider, clock, monkeypatch, slow=False)
    result = agent.run_conversation('short task', task_id='astra-codex-short')
    assert result['final_response'] == 'CODEX FINAL'
    clock.advance(400)
    assert fl.refusal(agent, 'terminal', {'command': 'true'}) is None, (
        'completed Codex turn leaves _fg_turn_active=True; out-of-turn tool is refused')


def test_setup_exception_clears_foreground_active_flag(clock, provider, monkeypatch):
    import agent.conversation_loop as loop
    agent = make_agent(provider)

    def failed_prompt(*args, **kwargs):
        raise RuntimeError('scripted prompt initialization failure')

    monkeypatch.setattr(loop, '_restore_or_build_system_prompt', failed_prompt)
    with pytest.raises(RuntimeError, match='scripted prompt'):
        agent.run_conversation('job', task_id='astra-prologue')
    clock.advance(400)
    assert fl.refusal(agent, 'terminal', {'command': 'true'}) is None, (
        'failed turn leaves _fg_turn_active=True; cleanup must be in outer finally')


def test_pending_answer_hard_stop_is_persisted_without_second_summary(clock, provider, monkeypatch, tmp_path):
    from clover_state import SessionDB
    db = SessionDB(tmp_path / 'pending.db')
    agent = make_agent(provider, session_db=db, session_id='astra-pending')
    provider.queue[:] = [text('PENDING VERIFIED REPORT')]
    monkeypatch.setenv('CLOVER_VERIFY_ON_STOP', '1')
    monkeypatch.setattr('agent.verification_stop.build_verify_on_stop_nudge', lambda **kw: 'verify it')

    def before(n, req):
        agent._turn_file_mutation_paths = {'changed.py'}
        clock.advance(370)

    provider.before = before
    result = agent.run_conversation('edit code', task_id='astra-pending')
    assert result['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON, result
    assert result['final_response'] == 'PENDING VERIFIED REPORT'
    assert len(provider.captured) == 1
    assert db.get_messages('astra-pending')[-1]['content'] == 'PENDING VERIFIED REPORT'
    assert [m['role'] for m in result['messages']] == ['user', 'assistant']
    assert agent._fg_turn_active is False


def test_normal_notice_preserves_every_previously_sent_prefix(clock, provider):
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 125)
    provider.queue[:] = [tool('todo', TODO, 't1'), tool('todo', TODO, 't2'), text('done')]
    result = agent.run_conversation('job', task_id='astra-cache')
    assert result['final_response'] == 'done'
    assert len(provider.captured) == 3
    for old, new in zip(provider.captured, provider.captured[1:]):
        assert old['tools'] == new['tools']
        assert old['messages'] == new['messages'][:len(old['messages'])]
    assert fl.NOTICE_MARKER in tool_texts(provider.captured[-1])[-1]


@pytest.mark.parametrize('mode', ['subagent', 'cron', 'oneshot', 'batch', 'single_query', 'off'])
def test_exempt_real_loops_and_limit_off_keep_heavy_tools(clock, provider, tmp_path, mode):
    from tests.agent.test_foreground_time_limit_loop import _write_limit
    if mode == 'off':
        _write_limit(0)
    agent = make_agent(provider, platform='cron' if mode == 'cron' else 'cli')
    if mode == 'subagent':
        agent._delegate_depth = 1
    elif mode in {'oneshot', 'batch'}:
        agent._delegation_checkpoint_exempt = mode
    elif mode == 'single_query':
        agent._foreground_limit_exempt = 'single_query'
    target = tmp_path / 'late-but-exempt.txt'
    provider.queue[:] = [tool('write_file', {'path': str(target), 'content': 'EXEMPT'}, 'w1'),
                         text('normal answer')]
    provider.before = lambda n, req: clock.advance(1000) if n == 1 else None
    result = agent.run_conversation('write the file', task_id=f'astra-{mode}')
    assert target.read_text() == 'EXEMPT'
    assert result['final_response'] == 'normal answer'
    assert agent._fg_episode is None
    assert len(provider.captured) == 2
    assert all(fl.NOTICE_MARKER not in json.dumps(req) for req in provider.captured)


def test_run_budget_notice_with_foreground_off_preserves_prefix_and_owned_result(clock, provider):
    from tests.agent.test_foreground_time_limit_loop import _write_limit
    from agent.conversation_loop import RUN_BUDGET_WRAPUP_NOTICE
    _write_limit(0)
    agent = make_agent(provider)
    agent.run_budget_seconds = 300
    held = []
    execute = agent._execute_tool_calls

    def after_tools(assistant, messages, *args, **kwargs):
        result = execute(assistant, messages, *args, **kwargs)
        held.append((messages[-1], json.dumps(messages[-1], sort_keys=True)))
        agent._run_budget_started_at = time.time() - 250
        return result

    agent._execute_tool_calls = after_tools
    provider.queue[:] = [tool('todo', TODO, 't1'), text('done')]
    agent.run_conversation('job', task_id='astra-run-budget')
    first, second = provider.captured
    assert first['messages'] == second['messages'][:len(first['messages'])]
    assert first['tools'] == second['tools']
    assert RUN_BUDGET_WRAPUP_NOTICE in tool_texts(second)[-1]
    original, snapshot = held[0]
    assert json.dumps(original, sort_keys=True) == snapshot
