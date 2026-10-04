"""Delegation checkpoint ownership and start boundaries (review fix round 1).

Real AIAgent / executor / delegate_task / fork-factory classes, no network:

* a todo declaration that finishes late (its worker was abandoned by the real
  tool deadline) must not grant authority to a newer turn;
* an inline child counts as started only when its conversation really begins,
  never at pool construction, submission or credential-lease setup;
* the static instruction reaches background forks by cached-prefix inheritance,
  so it must carry its own scope and the fork's permissions must not widen.
"""
from __future__ import annotations

import json
import socket
import threading

import pytest

from tests.agent.test_delegation_checkpoint import (
    _agent,
    _delegate,
    _direct,
    _write,
    run_batch,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise RuntimeError('checkpoint lifecycle tests prohibit network access')

    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


@pytest.fixture
def inline_session():
    """No raw session id bound, async delivery unsupported: the inline fallback."""
    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(platform='batch', chat_id='', session_key='s', async_delivery=False)
    try:
        yield
    finally:
        clear_session_vars(tokens)


@pytest.fixture
def child_conversations(monkeypatch):
    """Only a real child's model conversation is faked; construction, pool,
    credential lease and the runner are the production code."""
    import run_agent

    started = []

    def conversation(self, user_message=None, **kwargs):
        started.append(user_message)
        return {'final_response': 'done', 'messages': [], 'api_calls': 1, 'completed': True,
                'input_tokens': 0, 'output_tokens': 0}

    monkeypatch.setattr(run_agent.AIAgent, 'run_conversation', conversation)
    return started


# ── 1. late declarations cannot authorize a newer turn ─────────────────────

def test_late_todo_completion_cannot_authorize_a_new_turn(tmp_path, monkeypatch):
    import agent.delegation_checkpoint as dc
    import agent.tool_executor as executor
    import tools.todo_tool as todo

    agent = _agent()
    dc.begin_turn(agent)
    monkeypatch.setattr(executor, '_resolve_concurrent_tool_timeout', lambda: 0.05)
    applied, release, recorded = threading.Event(), threading.Event(), threading.Event()
    real_todo, real_for_agent = todo.todo_tool, todo.todo_for_agent

    def delayed_return(*args, **kwargs):
        result = real_todo(*args, **kwargs)
        applied.set()
        assert release.wait(5)
        return result

    def observed_for_agent(*args, **kwargs):
        # The worker no longer registers authority itself (the foreground root
        # does), so the completion signal is the real todo_for_agent returning.
        try:
            return real_for_agent(*args, **kwargs)
        finally:
            recorded.set()

    monkeypatch.setattr(todo, 'todo_tool', delayed_return)
    monkeypatch.setattr(todo, 'todo_for_agent', observed_for_agent)
    errors = []

    def old_turn():
        try:
            run_batch(agent, [_direct('Old turn only')])
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(repr(exc))

    worker = threading.Thread(target=old_turn)
    worker.start()
    assert applied.wait(5)
    worker.join(2)
    assert not worker.is_alive(), 'the real deadline must have abandoned the todo worker'

    dc.begin_turn(agent)                       # a new human turn
    release.set()                              # the old declaration now completes
    assert recorded.wait(5)

    monkeypatch.setattr(executor, '_resolve_concurrent_tool_timeout', lambda: 10)
    assert dc.get_checkpoint(agent).snapshot()['state'] == dc.UNDECIDED
    target = tmp_path / 'new-turn-without-decision.txt'
    (blocked,) = run_batch(agent, [_write(target)])

    assert not target.exists(), 'an old declaration authorized a new work episode'
    assert blocked['error_type'] == 'delegation_decision_required'
    assert not errors


def test_late_declaration_after_budget_renewal_is_ignored(tmp_path):
    """Same ownership rule inside one turn: a stale receipt never overwrites."""
    import agent.delegation_checkpoint as dc

    agent = _agent()
    checkpoint = dc.get_checkpoint(agent)
    stale = dc.claim_declaration(agent)
    checkpoint.declare('direct', 'newer declaration')

    dc.record_declaration(agent, {'mode': 'delegate', 'reason': 'stale'}, stale)

    assert checkpoint.snapshot()['decision'] == {'mode': 'direct', 'reason': 'newer declaration'}


def test_declaration_from_a_replaced_checkpoint_is_not_applied():
    """Identity AND generation must both still match (e.g. state replaced)."""
    import agent.delegation_checkpoint as dc

    agent = _agent()
    stale = dc.claim_declaration(agent)
    replacement = dc.DelegationCheckpoint(dc.load_settings())
    replacement.generation = stale.generation        # same generation number, different owner
    agent._delegation_checkpoint = replacement

    applied = dc.record_declaration(agent, {'mode': 'direct', 'reason': 'stale owner'}, stale)

    assert applied is False
    assert replacement.snapshot()['state'] == dc.UNDECIDED
    assert stale.checkpoint.snapshot()['state'] == dc.UNDECIDED


def test_declaration_in_the_current_turn_still_authorizes(tmp_path):
    agent = _agent()
    target = tmp_path / 'ok.txt'
    run_batch(agent, [_direct(), _write(target)])
    assert target.exists()


# ── 2. inline start boundary ───────────────────────────────────────────────

def test_credential_lease_failure_before_the_child_conversation_does_not_unlock(
        tmp_path, monkeypatch, inline_session, child_conversations):
    import tools.delegate_tool as dt

    original = dt._build_child_preserving_parent_tools

    class BrokenLease:
        def acquire_lease(self):
            raise RuntimeError('offline injected credential lease failure')

    def build(**kwargs):
        child = original(**kwargs)
        child._credential_pool = BrokenLease()
        return child

    monkeypatch.setattr(dt, '_build_child_preserving_parent_tools', build)
    agent = _agent()
    run_batch(agent, [_delegate()])
    try:
        run_batch(agent, [('delegate_task', {'goal': 'lease fails'})])
    except Exception:
        pass
    target = tmp_path / 'should-not-write.txt'

    (blocked,) = run_batch(agent, [_write(target)])

    assert child_conversations == []
    assert not target.exists(), 'parent work unlocked before any child conversation started'
    assert blocked['error_type'] == 'delegation_dispatch_required'


def test_pool_construction_failure_before_any_child_entry_does_not_unlock(
        tmp_path, monkeypatch, inline_session, child_conversations):
    import tools.daemon_pool as pool

    def broken_pool(*args, **kwargs):
        raise RuntimeError('offline injected pool construction failure')

    agent = _agent()
    run_batch(agent, [_delegate()])
    with monkeypatch.context() as patch:
        patch.setattr(pool, 'DaemonThreadPoolExecutor', broken_pool)
        with pytest.raises(RuntimeError, match='pool construction failure'):
            agent._dispatch_delegate_task({
                'tasks': [{'goal': 'offline review lane A'}, {'goal': 'offline review lane B'}],
                'background': False})
    target = tmp_path / 'no-child-started.txt'

    (blocked,) = run_batch(agent, [_write(target)])

    assert child_conversations == []
    assert not target.exists(), 'pool failed before any child ran but parent work was unlocked'
    assert blocked['error_type'] == 'delegation_dispatch_required'


def test_real_inline_child_conversation_start_unlocks(tmp_path, inline_session, child_conversations):
    agent = _agent()
    target = tmp_path / 'ok.txt'

    _, ran, _ = run_batch(agent, [_delegate(), ('delegate_task', {'goal': 'inline lane'}), _write(target, 'after')])

    assert 'results' in ran and ran.get('status') != 'dispatched'
    assert child_conversations == ['inline lane']
    assert target.read_text() == 'after'


def test_real_inline_batch_unlocks_once_the_first_child_starts(tmp_path, inline_session, child_conversations):
    agent = _agent()
    target = tmp_path / 'ok.txt'

    run_batch(agent, [_delegate(), ('delegate_task', {'tasks': [{'goal': 'offline review lane A'}, {'goal': 'offline review lane B'}]}),
                      _write(target, 'after')])

    assert sorted(child_conversations) == ['offline review lane A', 'offline review lane B']
    assert target.read_text() == 'after'


def test_a_child_that_starts_after_a_renewal_cannot_satisfy_the_newer_declaration(
        tmp_path, monkeypatch, inline_session):
    """The ticket is generation-bound: a child entering its conversation after
    the parent re-declared must not unlock the newer declaration."""
    import run_agent
    import agent.delegation_checkpoint as dc

    agent = _agent()
    run_batch(agent, [_delegate('first')])

    def renewing_conversation(self, user_message=None, **kwargs):
        dc.get_checkpoint(agent).declare('delegate', 'second')   # parent renews mid-flight
        return {'final_response': 'done', 'messages': [], 'api_calls': 1, 'completed': True,
                'input_tokens': 0, 'output_tokens': 0}

    # The conversation body runs after the credit boundary, so renew first:
    original_credit = dc.DispatchTicket.credit

    def credit_after_renewal(ticket, kind):
        dc.get_checkpoint(agent).declare('delegate', 'second')
        return original_credit(ticket, kind)

    monkeypatch.setattr(dc.DispatchTicket, 'credit', credit_after_renewal)
    monkeypatch.setattr(run_agent.AIAgent, 'run_conversation', renewing_conversation)
    run_batch(agent, [('delegate_task', {'goal': 'late lane'})])
    target = tmp_path / 'no.txt'

    (blocked,) = run_batch(agent, [_write(target)])

    assert not target.exists()
    assert blocked['error_type'] == 'delegation_dispatch_required'


# ── 3. static instruction scope vs background forks ────────────────────────

def test_inherited_instruction_is_scoped_and_fork_permissions_do_not_widen():
    from agent.background_review import build_cache_parity_fork
    from agent.prompt_builder import TODO_DELEGATION_DECISION_GUIDANCE
    from agent.system_prompt import build_system_prompt_parts
    from clover_cli.plugins import clear_thread_tool_whitelist, set_thread_tool_whitelist

    parent = _agent(enabled_toolsets=['todo', 'delegation', 'file', 'memory', 'skills'], skip_memory=False)
    parent._cached_system_prompt = build_system_prompt_parts(parent)['stable']
    assert TODO_DELEGATION_DECISION_GUIDANCE in parent._cached_system_prompt
    fork, _runtime, routed = build_cache_parity_fork(parent, None, max_iterations=2)
    assert not routed
    assert fork._cached_system_prompt == parent._cached_system_prompt, 'the cached prefix must stay byte-identical'

    inherited = fork._cached_system_prompt
    for excluded in ('background memory/skill review', 'side-question', 'delegated worker', 'scheduled'):
        assert excluded in TODO_DELEGATION_DECISION_GUIDANCE
    assert 'foreground' in TODO_DELEGATION_DECISION_GUIDANCE.lower()
    assert TODO_DELEGATION_DECISION_GUIDANCE in inherited

    set_thread_tool_whitelist(
        {'memory', 'skill_manage', 'skill_view', 'skills_list', 'read_file', 'search_files'},
        deny_msg_fmt='Background review denied non-whitelisted tool: {tool_name}')
    try:
        denied = json.loads(fork._invoke_tool('todo', _direct()[1], 'review', tool_call_id='fork-decision'))
        saved = fork._invoke_tool('memory', {'action': 'add', 'target': 'memory', 'content': 'prefers tabs'},
                                  'review', tool_call_id='fork-memory')
    finally:
        clear_thread_tool_whitelist()
    assert 'denied' in denied['error'].lower(), 'fork permissions must not be widened to admit todo'
    assert json.loads(saved)['success'] is True
    assert getattr(fork, '_delegation_checkpoint', None) is None, 'a fork never owns a checkpoint'


def test_instruction_is_still_taught_to_foreground_roots_only():
    from agent.prompt_builder import TODO_DELEGATION_DECISION_GUIDANCE
    from agent.system_prompt import build_system_prompt_parts

    def stable(agent):
        return build_system_prompt_parts(agent)['stable']

    assert TODO_DELEGATION_DECISION_GUIDANCE in stable(_agent())
    assert TODO_DELEGATION_DECISION_GUIDANCE not in stable(_agent(platform='cron'))
    assert TODO_DELEGATION_DECISION_GUIDANCE not in stable(_agent(enabled_toolsets=['todo', 'file']))
    exempt = _agent()
    exempt._delegation_checkpoint_exempt = 'oneshot'
    assert TODO_DELEGATION_DECISION_GUIDANCE not in stable(exempt)
