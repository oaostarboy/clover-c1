"""Declaration ownership is captured by the CALLER before any worker handoff.

A ``todo`` call can be held on a worker thread by execution middleware or a
``pre_tool_call`` plugin hook, abandoned (real tool deadline or a real user
interrupt), and finish after a new human turn began. Its declaration must not
claim the new turn's generation. Real AIAgent / executor / plugin manager
classes, no network.
"""
from __future__ import annotations

import json
import socket
import threading
from types import SimpleNamespace

import pytest

from tests.agent.test_delegation_checkpoint import (
    _agent,
    _direct,
    _write,
    plugin_manager,  # noqa: F401  (fixture)
    run_batch,
)

PLAN = [{'id': 'p1', 'content': 'old plan item', 'status': 'in_progress'}]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise RuntimeError('capture tests prohibit network access')

    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)


@pytest.fixture
def todo_finished(monkeypatch):
    """Event set when the executor's todo implementation has fully returned."""
    import tools.todo_tool as todo

    finished = threading.Event()
    real = todo.todo_for_agent

    def observed(*args, **kwargs):
        try:
            return real(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(todo, 'todo_for_agent', observed)
    return finished


def _old_todo():
    return ('todo', {'todos': PLAN, 'delegation': {'mode': 'direct', 'reason': 'old turn only'}})


def _assert_next_turn_blocked(agent, tmp_path, store_items_expected=True):
    import agent.delegation_checkpoint as dc

    assert dc.get_checkpoint(agent).snapshot()['state'] == dc.UNDECIDED
    target = tmp_path / 'next-turn.txt'
    (blocked,) = run_batch(agent, [_write(target)])
    assert not target.exists(), 'a stale todo authorized the next human turn'
    assert blocked['error_type'] == 'delegation_decision_required'
    if store_items_expected:
        # Previous contract: a stale todo may still update the plan store.
        assert agent._todo_store.read()[0]['id'] == 'p1'


def test_todo_held_in_execution_middleware_then_abandoned_cannot_authorize_next_turn(
        tmp_path, monkeypatch, plugin_manager, todo_finished):
    import agent.delegation_checkpoint as dc
    import agent.tool_executor as executor

    agent = _agent()
    dc.begin_turn(agent)
    reached, release = threading.Event(), threading.Event()

    def slow_middleware(**kw):
        if kw.get('tool_name') == 'todo' and not reached.is_set():
            reached.set()
            assert release.wait(5)
        return kw['next_call'](kw['args'])

    plugin_manager._middleware.setdefault('tool_execution', []).append(slow_middleware)
    monkeypatch.setattr(executor, '_resolve_concurrent_tool_timeout', lambda: 0.05)
    outcome = {}

    def old_turn():
        outcome['result'] = run_batch(agent, [_old_todo()])

    worker = threading.Thread(target=old_turn)
    worker.start()
    assert reached.wait(5)
    worker.join(3)
    assert not worker.is_alive(), 'the real deadline must abandon the held todo worker'
    assert 'timed out' in json.dumps(outcome['result']), 'the old call keeps its normal (timeout) result'

    dc.begin_turn(agent)                       # the next human turn
    release.set()
    assert todo_finished.wait(5)

    monkeypatch.setattr(executor, '_resolve_concurrent_tool_timeout', lambda: 10)
    _assert_next_turn_blocked(agent, tmp_path)


def test_todo_held_in_plugin_pre_tool_hook_then_user_interrupt_cannot_authorize_next_turn(
        tmp_path, plugin_manager, todo_finished):
    """Real agent.interrupt() at the DEFAULT tool deadline."""
    import agent.delegation_checkpoint as dc

    agent = _agent()
    dc.begin_turn(agent)
    reached, release = threading.Event(), threading.Event()

    def slow_hook(**kw):
        if kw.get('tool_name') == 'todo' and not reached.is_set():
            reached.set()
            assert release.wait(10)
        return None

    plugin_manager._hooks.setdefault('pre_tool_call', []).append(slow_hook)
    outcome = {}

    def old_turn():
        outcome['result'] = run_batch(agent, [_old_todo()])

    worker = threading.Thread(target=old_turn)
    worker.start()
    assert reached.wait(5)
    agent.interrupt('new user message')
    worker.join(5)
    assert not worker.is_alive(), 'the interrupt must abandon the held todo worker'
    assert 'abandoned' in json.dumps(outcome['result']) or 'cancelled' in json.dumps(outcome['result']).lower()
    agent.clear_interrupt()

    dc.begin_turn(agent)
    release.set()
    assert todo_finished.wait(5)

    _assert_next_turn_blocked(agent, tmp_path)


def test_concurrent_path_hands_the_worker_a_receipt_captured_before_submission(tmp_path, monkeypatch):
    """The concurrent worker never claims for itself: it is given a receipt
    claimed on the calling thread, at the generation current when it was
    submitted (observed at the real ``_invoke_tool`` seam)."""
    import agent.delegation_checkpoint as dc

    agent = _agent()
    dc.begin_turn(agent)
    generation_at_submit = dc.get_checkpoint(agent).snapshot()['generation']
    seen = []
    real_invoke = agent._invoke_tool

    def spy(*args, **kwargs):
        seen.append(kwargs.get('declaration_owner', dc.RECEIPT_UNSET))
        dc.begin_turn(agent)                     # the turn moves on before the declaration lands
        return real_invoke(*args, **kwargs)

    monkeypatch.setattr(agent, '_invoke_tool', spy)
    name, args = _old_todo()
    call = SimpleNamespace(id='c1', function=SimpleNamespace(name=name, arguments=json.dumps(args)))

    agent._execute_tool_calls_concurrent(SimpleNamespace(tool_calls=[call]), [], 'capture-test')

    (receipt,) = seen
    assert isinstance(receipt, dc.DeclarationOwner), 'the worker must be handed an explicit receipt'
    assert receipt.generation == generation_at_submit
    _assert_next_turn_blocked(agent, tmp_path)


# ── the receipt is mandatory: a caller that skipped it grants nothing ──────

def test_todo_for_agent_without_a_receipt_never_authorizes():
    import agent.delegation_checkpoint as dc
    from tools.todo_tool import todo_for_agent

    agent = _agent()
    result = json.loads(todo_for_agent(agent, {'delegation': {'mode': 'direct', 'reason': 'no receipt'}}))

    assert result['delegation']['mode'] == 'direct'          # the plan metadata is still recorded
    assert dc.get_checkpoint(agent).snapshot()['state'] == dc.UNDECIDED


def test_record_declaration_without_a_receipt_never_authorizes():
    import agent.delegation_checkpoint as dc

    agent = _agent()
    assert dc.record_declaration(agent, {'mode': 'direct', 'reason': 'x'}) is False
    assert dc.get_checkpoint(agent).snapshot()['state'] == dc.UNDECIDED


def test_legacy_invoke_with_an_explicit_missing_receipt_does_not_authorize():
    import agent.delegation_checkpoint as dc

    agent = _agent()
    agent._invoke_tool('todo', {'delegation': {'mode': 'direct', 'reason': 'x'}}, 't',
                       declaration_owner=None)

    assert dc.get_checkpoint(agent).snapshot()['state'] == dc.UNDECIDED


def test_legacy_invoke_captures_at_its_own_synchronous_entry():
    """A direct legacy caller IS the boundary: nothing hands off before entry."""
    import agent.delegation_checkpoint as dc

    agent = _agent()
    agent._invoke_tool('todo', {'delegation': {'mode': 'direct', 'reason': 'legacy'}}, 't')

    assert dc.get_checkpoint(agent).snapshot()['state'] == dc.DIRECT_AUTHORIZED


@pytest.mark.parametrize('mutation', ['new_generation', 'replaced_checkpoint'])
def test_stale_receipt_is_rejected_for_same_object_and_replacement(mutation):
    import agent.delegation_checkpoint as dc
    from tools.todo_tool import todo_for_agent

    agent = _agent()
    receipt = dc.claim_declaration(agent)
    if mutation == 'new_generation':
        dc.begin_turn(agent)
    else:
        replacement = dc.DelegationCheckpoint(dc.load_settings())
        replacement.generation = receipt.generation
        agent._delegation_checkpoint = replacement

    todo_for_agent(agent, {'delegation': {'mode': 'direct', 'reason': 'stale'}}, receipt)

    assert dc.get_checkpoint(agent).snapshot()['state'] == dc.UNDECIDED
