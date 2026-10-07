"""Foreground completion owns declaration authority.

A todo worker may be held by middleware / a pre_tool_call plugin hook and be
abandoned by the real tool deadline or a real user interrupt. Whatever it does
afterwards (fill its private slot, even complete normally in the SAME
generation with no newer decision and no turn reset) must never grant runtime
authority: only the foreground caller, accepting a normal completed result,
registers it. Real AIAgent / executor / plugin manager classes, no network.
"""
from __future__ import annotations

import json
import socket
import threading
from types import SimpleNamespace

import pytest

from tests.agent.test_delegation_checkpoint import (
    _agent,
    _delegate,
    _direct,
    _write,
    plugin_manager,  # noqa: F401  (fixture)
    run_batch,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise RuntimeError('root-acceptance tests prohibit network access')

    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


@pytest.fixture
def executor_deadline(monkeypatch):
    import agent.tool_executor as executor

    def set_deadline(seconds):
        monkeypatch.setattr(executor, '_resolve_concurrent_tool_timeout', lambda: seconds)

    return set_deadline


class TodoProbe:
    """Observes the real ``todo_for_agent`` seam: per-call finish events."""

    def __init__(self, monkeypatch):
        import tools.todo_tool as todo

        self._real = todo.todo_for_agent
        self.finished = {}
        self.calls = []
        self.hold_reason = None
        self.reached = threading.Event()
        self.release = threading.Event()
        self.hold_after_apply = False
        monkeypatch.setattr(todo, 'todo_for_agent', self._wrapped)

    def finished_event(self, reason):
        return self.finished.setdefault(reason, threading.Event())

    def _wrapped(self, agent, args, owner=None):
        reason = (args.get('delegation') or {}).get('reason')
        self.calls.append((reason, owner))
        try:
            result = self._real(agent, args, owner)
            if self.hold_after_apply and reason == self.hold_reason:
                self.reached.set()
                assert self.release.wait(10)
            return result
        finally:
            self.finished_event(reason).set()


@pytest.fixture
def todo_probe(monkeypatch):
    return TodoProbe(monkeypatch)


def _todo_call(args, call_id='c1'):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name='todo', arguments=json.dumps(args)))


def _plan_direct(reason):
    return ('todo', {'todos': [{'id': 'p1', 'content': 'plan', 'status': 'in_progress'}],
                     'delegation': {'mode': 'direct', 'reason': reason}})


def _state(agent):
    import agent.delegation_checkpoint as dc

    return dc.get_checkpoint(agent).snapshot()


def _hold_middleware(plugin_manager, reason, reached, release):
    def hold(**kw):
        if (kw['args'].get('delegation') or {}).get('reason') == reason and not reached.is_set():
            reached.set()
            assert release.wait(10)
        return kw['next_call'](kw['args'])

    plugin_manager._middleware.setdefault('tool_execution', []).append(hold)


def _write_is_blocked(agent, tmp_path, name='probe.txt'):
    target = tmp_path / name
    (blocked,) = run_batch(agent, [_write(target)])
    assert not target.exists(), 'a non-accepted todo authorized parent work'
    assert blocked['error_type'] in ('delegation_decision_required', 'delegation_dispatch_required')
    return blocked


# ── 1. same sequential frame: later todos must not lend a receipt ──────────

def test_held_todo_cannot_borrow_a_later_todos_receipt_in_the_same_batch(
        tmp_path, plugin_manager, todo_probe, executor_deadline):
    import agent.delegation_checkpoint as dc

    agent = _agent()
    dc.begin_turn(agent)
    reached, release = threading.Event(), threading.Event()
    _hold_middleware(plugin_manager, 'old held direct', reached, release)
    executor_deadline(0.05)

    results = run_batch(agent, [_direct('old held direct'), _delegate('newer decision'), ('todo', {})])

    assert reached.is_set()
    assert 'timed out' in json.dumps(results[0])
    before = _state(agent)
    assert before['state'] == dc.SPAWN_REQUIRED and before['decision']['reason'] == 'newer decision'
    release.set()
    assert todo_probe.finished_event('old held direct').wait(5)

    executor_deadline(10)
    after = _state(agent)
    assert after['decision'] == before['decision'] and after['state'] == dc.SPAWN_REQUIRED
    _write_is_blocked(agent, tmp_path)

    # Immutable per-invocation slots: the late worker wrote ONLY its own slot.
    by_reason = {reason: owner for reason, owner in todo_probe.calls}   # the held worker arrives last
    held, newer, read = by_reason['old held direct'], by_reason['newer decision'], by_reason[None]
    assert len(todo_probe.calls) == 3
    assert held.slot.decision == {'mode': 'direct', 'reason': 'old held direct'}
    assert newer.slot.decision == {'mode': 'delegate', 'reason': 'newer decision'}
    assert read.slot.decision is None, 'a late worker wrote into another invocation\'s slot'
    assert len({id(held.slot), id(newer.slot), id(read.slot)}) == 3


def test_each_invocation_owns_a_distinct_private_slot(tmp_path, todo_probe):
    run_batch(_agent(), [_direct('first'), _delegate('second')])

    (first_reason, first_owner), (second_reason, second_owner) = todo_probe.calls
    assert (first_reason, second_reason) == ('first', 'second')
    assert first_owner is not second_owner and first_owner.slot is not second_owner.slot
    assert first_owner.tool_call_id != second_owner.tool_call_id


# ── 2. same generation, no newer decision, no reset: still no authority ────

def test_abandoned_todo_completing_late_in_the_same_generation_grants_nothing_deadline(
        tmp_path, plugin_manager, todo_probe, executor_deadline):
    import agent.delegation_checkpoint as dc

    agent = _agent()
    dc.begin_turn(agent)
    before = _state(agent)
    reached, release = threading.Event(), threading.Event()
    _hold_middleware(plugin_manager, 'held same generation', reached, release)
    executor_deadline(0.05)

    (result,) = run_batch(agent, [_plan_direct('held same generation')])
    assert 'timed out' in json.dumps(result), 'the abandoned call keeps its normal timeout result'
    release.set()
    assert todo_probe.finished_event('held same generation').wait(5)

    executor_deadline(10)
    assert _state(agent) == before, 'no newer decision and no reset: state must be exactly as before'
    assert agent._todo_store.read()[0]['id'] == 'p1', 'metadata may still be mutated by the late todo'
    _write_is_blocked(agent, tmp_path)


def test_abandoned_todo_completing_late_in_the_same_generation_grants_nothing_interrupt(
        tmp_path, plugin_manager, todo_probe):
    import agent.delegation_checkpoint as dc

    agent = _agent()
    dc.begin_turn(agent)
    before = _state(agent)
    reached, release = threading.Event(), threading.Event()

    def slow_hook(**kw):
        if kw.get('tool_name') == 'todo' and not reached.is_set():
            reached.set()
            assert release.wait(10)
        return None

    plugin_manager._hooks.setdefault('pre_tool_call', []).append(slow_hook)
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.setdefault(
        'result', run_batch(agent, [_plan_direct('held by hook')])))
    worker.start()
    assert reached.wait(5)
    agent.interrupt('new user message')
    worker.join(5)
    assert not worker.is_alive(), 'the real interrupt must abandon the held worker'
    assert 'abandoned' in json.dumps(outcome['result'])
    agent.clear_interrupt()
    release.set()
    assert todo_probe.finished_event('held by hook').wait(5)

    assert _state(agent) == before
    _write_is_blocked(agent, tmp_path)


# ── 3. next turn, newer decision, replacement, no owner ────────────────────

def test_late_todo_cannot_authorize_the_next_turn(tmp_path, plugin_manager, todo_probe, executor_deadline):
    import agent.delegation_checkpoint as dc

    agent = _agent()
    dc.begin_turn(agent)
    reached, release = threading.Event(), threading.Event()
    _hold_middleware(plugin_manager, 'old turn', reached, release)
    executor_deadline(0.05)
    run_batch(agent, [_plan_direct('old turn')])
    dc.begin_turn(agent)
    release.set()
    assert todo_probe.finished_event('old turn').wait(5)

    executor_deadline(10)
    assert _state(agent)['state'] == dc.UNDECIDED
    _write_is_blocked(agent, tmp_path)


def test_replacement_checkpoint_with_a_coincident_generation_is_never_authorized(
        tmp_path, plugin_manager, todo_probe, executor_deadline):
    import agent.delegation_checkpoint as dc

    agent = _agent()
    dc.begin_turn(agent)
    reached, release = threading.Event(), threading.Event()
    _hold_middleware(plugin_manager, 'old object', reached, release)
    executor_deadline(0.05)
    run_batch(agent, [_plan_direct('old object')])
    held_generation = _state(agent)['generation']
    replacement = dc.DelegationCheckpoint(dc.load_settings())
    replacement.generation = held_generation
    agent._delegation_checkpoint = replacement
    release.set()
    assert todo_probe.finished_event('old object').wait(5)

    executor_deadline(10)
    assert replacement.snapshot()['state'] == dc.UNDECIDED
    _write_is_blocked(agent, tmp_path)


def test_a_todo_without_an_owner_updates_metadata_but_grants_nothing():
    import agent.delegation_checkpoint as dc
    from tools.todo_tool import todo_for_agent

    agent = _agent()
    result = json.loads(todo_for_agent(
        agent, {'todos': [{'id': 'p', 'content': 'x', 'status': 'in_progress'}],
                'delegation': {'mode': 'direct', 'reason': 'ownerless'}}))

    assert result['delegation']['reason'] == 'ownerless'
    assert dc.get_checkpoint(agent).snapshot()['state'] == dc.UNDECIDED


def test_todo_for_agent_never_registers_authority_itself():
    """Even with a perfectly valid owner the worker-side helper only fills a slot."""
    import agent.delegation_checkpoint as dc
    from tools.todo_tool import todo_for_agent

    agent = _agent()
    owner = dc.claim_declaration(agent, 'c-1')
    todo_for_agent(agent, {'delegation': {'mode': 'direct', 'reason': 'slot only'}}, owner)

    assert owner.slot.decision == {'mode': 'direct', 'reason': 'slot only'}
    assert dc.get_checkpoint(agent).snapshot()['state'] == dc.UNDECIDED


def test_non_applied_or_failed_todos_grant_nothing(tmp_path):
    import agent.delegation_checkpoint as dc

    agent = _agent()
    run_batch(agent, [('todo', {}), ('todo', {'delegation': {'mode': 'direct', 'reason': ' '}}),
                      ('todo', {'todos': 'not-a-list', 'delegation': {'mode': 'direct', 'reason': 'x'}})])

    assert dc.get_checkpoint(agent).snapshot()['state'] == dc.UNDECIDED
    _write_is_blocked(agent, tmp_path)


# ── 7. foreground acceptance still works ───────────────────────────────────

def test_accepted_foreground_direct_registers_before_the_next_work_call(tmp_path):
    target = tmp_path / 'after.txt'
    run_batch(_agent(), [_direct('accepted'), _write(target, 'ok')])
    assert target.read_text() == 'ok'


def test_accepted_delegate_still_requires_a_real_child_start(tmp_path):
    import agent.delegation_checkpoint as dc

    agent = _agent()
    run_batch(agent, [_delegate('accepted')])
    assert _state(agent)['state'] == dc.SPAWN_REQUIRED
    _write_is_blocked(agent, tmp_path)


# ── 4. concurrent boundary: real executor classes ──────────────────────────

def _concurrent(agent, calls, task_id='root-acceptance'):
    agent._execute_tool_calls_concurrent(SimpleNamespace(tool_calls=calls), [], task_id)


def test_concurrent_normal_completion_is_accepted_by_the_foreground():
    import agent.delegation_checkpoint as dc

    agent = _agent()
    _concurrent(agent, [_todo_call(_direct('concurrent ok')[1])])

    assert _state(agent)['state'] == dc.DIRECT_AUTHORIZED


def test_concurrent_crash_after_applied_grants_nothing(monkeypatch):
    import agent.delegation_checkpoint as dc
    import tools.todo_tool as todo

    real = todo.todo_for_agent

    def crash_after_apply(agent, args, owner=None):
        real(agent, args, owner)
        raise RuntimeError('crash after the slot was filled')

    monkeypatch.setattr(todo, 'todo_for_agent', crash_after_apply)
    agent = _agent()
    _concurrent(agent, [_todo_call(_direct('crash')[1])])

    assert _state(agent)['state'] == dc.UNDECIDED


def test_concurrent_plugin_denied_todo_grants_nothing(plugin_manager):
    import agent.delegation_checkpoint as dc

    plugin_manager._hooks.setdefault('pre_tool_call', []).append(
        lambda **kw: {'action': 'block', 'message': 'denied by policy'} if kw.get('tool_name') == 'todo' else None)
    agent = _agent()
    _concurrent(agent, [_todo_call(_direct('denied')[1])])

    assert _state(agent)['state'] == dc.UNDECIDED


def test_concurrent_timed_out_todo_that_fills_its_slot_late_grants_nothing(
        tmp_path, monkeypatch, todo_probe, executor_deadline):
    import agent.delegation_checkpoint as dc

    agent = _agent()
    todo_probe.hold_reason, todo_probe.hold_after_apply = 'late slot', True
    executor_deadline(0.05)
    _concurrent(agent, [_todo_call(_direct('late slot')[1])])
    assert todo_probe.reached.is_set(), 'the slot was filled but the worker was still running at the deadline'
    todo_probe.release.set()
    assert todo_probe.finished_event('late slot').wait(5)
    import time
    time.sleep(0.3)                                   # let the abandoned worker set its completion bit

    executor_deadline(10)
    owner = todo_probe.calls[0][1]
    assert owner.slot.decision is not None and owner.slot.completed, 'precondition: late slot data + bit exist'
    assert _state(agent)['state'] == dc.UNDECIDED
    _write_is_blocked(agent, tmp_path)


def test_concurrent_late_result_chosen_for_display_after_grace_cannot_authorize(
        tmp_path, monkeypatch, todo_probe, executor_deadline):
    """The worker finishes AFTER the executor froze eligibility but BEFORE the
    display loop prefers its late result; eligibility must stay frozen."""
    import agent.delegation_checkpoint as dc
    import agent.tool_executor as executor

    agent = _agent()
    todo_probe.hold_reason, todo_probe.hold_after_apply = 'display late', True
    executor_deadline(0.05)
    real_freeze = executor._freeze_accepted_declarations

    def freeze_then_let_the_worker_finish(*args, **kwargs):
        frozen = real_freeze(*args, **kwargs)
        todo_probe.release.set()
        assert todo_probe.finished_event('display late').wait(5)
        import time
        time.sleep(0.3)
        return frozen

    monkeypatch.setattr(executor, '_freeze_accepted_declarations', freeze_then_let_the_worker_finish)
    _concurrent(agent, [_todo_call(_direct('display late')[1])])

    executor_deadline(10)
    assert _state(agent)['state'] == dc.UNDECIDED
    _write_is_blocked(agent, tmp_path)


def test_concurrent_interrupt_abandoned_todo_grants_nothing(tmp_path, monkeypatch, todo_probe):
    import agent.delegation_checkpoint as dc
    import agent.tool_executor as executor

    monkeypatch.setattr(executor, '_resolve_concurrent_tool_timeout', lambda: None)
    agent = _agent()
    todo_probe.hold_reason, todo_probe.hold_after_apply = 'interrupted', True
    runner = threading.Thread(target=lambda: _concurrent(agent, [_todo_call(_direct('interrupted')[1])]))
    runner.start()
    assert todo_probe.reached.wait(5)
    agent.interrupt('stop')
    runner.join(10)
    assert not runner.is_alive(), 'the real interrupt must abandon the batch'
    agent.clear_interrupt()
    todo_probe.release.set()
    assert todo_probe.finished_event('interrupted').wait(5)

    assert _state(agent)['state'] == dc.UNDECIDED
    monkeypatch.setattr(executor, '_resolve_concurrent_tool_timeout', lambda: 10)
    _write_is_blocked(agent, tmp_path)


# ── 5. synchronous legacy entry and executor-owned contexts ────────────────

def test_sync_legacy_fresh_call_registers_on_its_own_accepted_return():
    import agent.delegation_checkpoint as dc

    agent = _agent()
    agent._invoke_tool('todo', {'delegation': {'mode': 'direct', 'reason': 'legacy fresh'}}, 't', 'legacy-1')

    assert _state(agent)['state'] == dc.DIRECT_AUTHORIZED


def test_sync_legacy_explicit_none_stale_and_mismatched_owners_grant_nothing():
    import agent.delegation_checkpoint as dc

    agent = _agent()
    call = {'delegation': {'mode': 'direct', 'reason': 'x'}}
    agent._invoke_tool('todo', call, 't', 'a', declaration_owner=None)
    assert _state(agent)['state'] == dc.UNDECIDED

    stale = dc.claim_declaration(agent, 'b')
    dc.begin_turn(agent)
    agent._invoke_tool('todo', call, 't', 'b', declaration_owner=stale)
    assert _state(agent)['state'] == dc.UNDECIDED

    wrong_call = dc.claim_declaration(agent, 'other-call')
    agent._invoke_tool('todo', call, 't', 'c', declaration_owner=wrong_call)
    assert _state(agent)['state'] == dc.UNDECIDED

    other_agent = _agent()
    foreign = dc.claim_declaration(other_agent, 'd')
    agent._invoke_tool('todo', call, 't', 'd', declaration_owner=foreign)
    assert _state(agent)['state'] == dc.UNDECIDED


def test_executor_owned_pipeline_never_lets_a_nested_legacy_call_reacquire_authority(
        plugin_manager):
    """An execution-middleware plugin that re-enters agent._invoke_tool('todo')
    with the legacy (unset) signature runs inside the executor context."""
    import agent.delegation_checkpoint as dc

    agent = _agent()
    nested = []

    def reentrant(**kw):
        if kw.get('tool_name') == 'write_file' and not nested:
            nested.append(agent._invoke_tool(
                'todo', {'delegation': {'mode': 'direct', 'reason': 'nested legacy'}}, 't', 'nested-1'))
        return kw['next_call'](kw['args'])

    plugin_manager._middleware.setdefault('tool_execution', []).append(reentrant)
    run_batch(agent, [_write('/nonexistent-dir-for-test/x.txt')])

    assert nested, 'precondition: the nested call ran inside the executor pipeline'
    assert _state(agent)['state'] == dc.UNDECIDED


def test_executor_context_is_reset_so_the_next_legacy_call_is_not_poisoned():
    import agent.delegation_checkpoint as dc

    agent = _agent()
    run_batch(agent, [('todo', {})])                     # an executor-owned call came and went
    agent._invoke_tool('todo', {'delegation': {'mode': 'direct', 'reason': 'after executor'}}, 't', 'next')

    assert _state(agent)['state'] == dc.DIRECT_AUTHORIZED


# ── contract edges ─────────────────────────────────────────────────────────

def test_an_executor_style_owner_handed_to_invoke_tool_fills_the_slot_but_never_registers():
    """Registration belongs to whoever allocated the owner (the foreground root)."""
    import agent.delegation_checkpoint as dc

    agent = _agent()
    owner = dc.claim_declaration(agent, 'exec-1')
    agent._invoke_tool('todo', {'delegation': {'mode': 'direct', 'reason': 'payload only'}}, 't', 'exec-1',
                       declaration_owner=owner)

    assert owner.slot.decision == {'mode': 'direct', 'reason': 'payload only'}
    assert _state(agent)['state'] == dc.UNDECIDED
    assert dc.register_accepted_declaration(agent, owner, 'exec-1') is True    # the root's step
    assert _state(agent)['state'] == dc.DIRECT_AUTHORIZED
    assert dc.register_accepted_declaration(agent, owner, 'exec-1') is False   # at most once


@pytest.mark.parametrize('marker_name', ['_ToolTimeoutResult', '_ToolCancelledResult'])
def test_sequential_acceptance_excludes_synthesized_timeout_and_cancel_markers(marker_name):
    """The markers report dispatched=True, blocked=False even though the worker
    was abandoned; a slot the worker already filled must still not register."""
    import agent.delegation_checkpoint as dc
    import agent.tool_executor as executor

    agent = _agent()
    owner = dc.claim_declaration(agent, 'seq-1')
    owner.slot.decision = {'mode': 'direct', 'reason': 'filled before the abandon'}
    owner.slot.completed = True
    marker = getattr(executor, marker_name)('abandoned')
    managed = executor._ManagedToolResult(result=marker, args={}, middleware_trace=[], blocked=False, dispatched=True)

    assert executor._accept_sequential_declaration(agent, owner, 'seq-1', managed) is False
    assert _state(agent)['state'] == dc.UNDECIDED

    normal = executor._ManagedToolResult(result='{"ok": 1}', args={}, middleware_trace=[], blocked=False, dispatched=True)
    assert executor._accept_sequential_declaration(agent, owner, 'seq-1', normal) is True
    assert _state(agent)['state'] == dc.DIRECT_AUTHORIZED


def test_blocked_or_undispatched_sequential_results_are_not_accepted():
    import agent.delegation_checkpoint as dc
    import agent.tool_executor as executor

    agent = _agent()
    for blocked, dispatched in ((True, True), (False, False)):
        owner = dc.claim_declaration(agent, 'seq-2')
        owner.slot.decision = {'mode': 'direct', 'reason': 'x'}
        owner.slot.completed = True
        managed = executor._ManagedToolResult(result='r', args={}, middleware_trace=[], blocked=blocked,
                                              dispatched=dispatched)
        assert executor._accept_sequential_declaration(agent, owner, 'seq-2', managed) is False
    assert _state(agent)['state'] == dc.UNDECIDED


def test_result_pairing_and_visible_todo_output_are_unchanged(tmp_path):
    agent = _agent()
    results = run_batch(agent, [_plan_direct('visible'), ('todo', {}), _write(tmp_path / 'p.txt')])

    assert results[0]['delegation'] == {'mode': 'direct', 'reason': 'visible'}
    assert set(results[0]) >= {'todos', 'revision', 'summary'}
    assert results[1]['todos'][0]['id'] == 'p1' and 'delegation_decision_required' not in json.dumps(results)
