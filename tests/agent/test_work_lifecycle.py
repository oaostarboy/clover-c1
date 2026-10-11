"""Completion lifecycle truthfulness (C1.5 P5: the Alfred and Octavia incidents).

* Alfred -- three helpers were dispatched, one reported, two were still running,
  and the root gave a final overall verdict.  A reply over unfinished or failed
  helper work must say what is still running / did not finish, and
  ``work_status`` must never be ``verified_completion`` for it.
* Octavia -- the root kept integrating results in the foreground after a
  dispatch.  Substantial integration on the helper-completion turn goes back to
  a helper; a short check stays direct; the hand-back's own completion is never
  handed back again; a child that was cut off (``max_iterations`` / refusal row
  that still says ``completed``) is a failure, not a success.

Real ``AIAgent.run_conversation`` against a scripted local provider, the real
async-delegation registry and durable ledger in a scratch ``CLOVER_HOME``, a
real ``SessionDB`` and the real ``delegate_task`` handler.  Only a child's own
model run, the provider and the clock are faked.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Dict

import pytest

import tools.async_delegation as ad
from agent import foreground_time_limit as fl
from tests.agent.test_foreground_verification_episode import (  # noqa: F401
    INTERNAL, _clean_async_state, _defaults, _write_config, all_tool_text,
    clock, completion_turn, make_agent, provider, text, tool,
)

STATUS_NOTE = '[Clover runtime] Helper status for this request'
SAFETY_ROW_SUMMARY = (
    '⚠️  The model declined to respond to this request (safety refusal '
    '— not a Clover/gateway failure).\n\nModel\'s explanation: I will research it.'
)


class Children:
    """Controllable helper runs: each task index blocks until released."""

    def __init__(self) -> None:
        self._gates: Dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self.outcomes: Dict[int, Dict[str, Any]] = {}
        self.goals: list = []
        # goal substring -> event: a helper whose goal contains it waits too
        self.holds: Dict[str, threading.Event] = {}

    def gate(self, index: int) -> threading.Event:
        with self._lock:
            return self._gates.setdefault(index, threading.Event())

    def release(self, *indexes: int) -> None:
        for i in indexes:
            self.gate(i).set()

    def release_all(self) -> None:
        with self._lock:
            for g in list(self._gates.values()) + list(self.holds.values()):
                g.set()


@pytest.fixture
def children(monkeypatch):
    import tools.delegate_tool as dt
    from unittest.mock import MagicMock

    ctl = Children()
    child = MagicMock()
    child._delegate_role = 'leaf'
    child._subagent_id = 'sub-1'
    creds = {'model': 'm', 'provider': None, 'base_url': None, 'api_key': None,
             'api_mode': None, 'command': None, 'args': None}

    def run_child(task_index, goal, child=None, parent_agent=None, **kw):
        ctl.goals.append(goal)
        ctl.gate(task_index).wait(20)
        for needle, event in list(ctl.holds.items()):
            if needle in goal:
                event.wait(20)
        entry = {'task_index': task_index, 'status': 'completed',
                 'summary': f'done: {goal}', 'api_calls': 1, 'duration_seconds': 0.1,
                 'model': 'm', 'exit_reason': 'completed'}
        entry.update(ctl.outcomes.get(task_index, {}))
        return entry

    monkeypatch.setattr(dt, '_build_child_agent', lambda **kw: child)
    monkeypatch.setattr(dt, '_run_single_child', run_child)
    monkeypatch.setattr(dt, '_resolve_delegation_credentials', lambda *a, **k: creds)
    yield ctl
    ctl.release_all()
    time.sleep(0.2)


@pytest.fixture
def dispatches(monkeypatch):
    """Every ``delegate_task`` dispatch through the registry, then the real one."""
    from tools.registry import registry

    seen: list = []
    real = registry.dispatch

    def spy(name, args, **kwargs):
        if name == 'delegate_task':
            seen.append(dict(args))
        return real(name, args, **kwargs)

    monkeypatch.setattr(registry, 'dispatch', spy)
    return seen


@pytest.fixture
def config(tmp_path):
    def write(fanout: str = 'joined') -> None:
        _write_config(300)
        from clover_constants import get_clover_home
        import yaml

        path = get_clover_home() / 'config.yaml'
        data = yaml.safe_load(path.read_text())
        data['delegation']['fanout_delivery'] = fanout
        path.write_text(yaml.safe_dump(data))

    return write


def _agent(provider, tmp_path, session_id):
    from clover_constants import get_clover_home
    from clover_state import SessionDB

    db = SessionDB(get_clover_home() / 'state.db')
    return make_agent(provider, session_db=db, session_id=session_id)


def dispatch_batch(agent, provider, goals, task_id='h1', reply='Helpers are on it.') -> str:
    """A human turn in which the root dispatches one background batch."""
    provider.queue[:] = [
        tool('delegate_task', {'tasks': [{'goal': g} for g in goals], 'background': True}, 'dg'),
        text(reply),
    ]
    agent.run_conversation('please look into the house network', conversation_history=[],
                           task_id=task_id)
    found = re.search(r'"delegation_id":\s*"([^"]+)"', all_tool_text(provider))
    assert found, 'delegate_task did not dispatch a background batch'
    provider.captured.clear()
    return found.group(1)


def wait_row(delegation_id: str, timeout: float = 8.0) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = ad.get_durable_delegation(delegation_id)
        if row is not None and row['state'] not in ('running', 'finalizing'):
            return row
        time.sleep(0.02)
    raise AssertionError(f'{delegation_id} never reached a terminal row')


def claim(delegation_id: str) -> None:
    """What the gateway does before it injects a completion turn."""
    assert ad.claim_completion_delivery(delegation_id, 'gw-claim')


def last_user_wire(request: dict) -> str:
    return [m['content'] for m in request['messages'] if m.get('role') == 'user'][-1]


GOALS = ['Scan the LAN for unknown devices', 'Review the router posture',
         'Read the local security logs']


# ── Alfred: one reported, two still running ────────────────────────────────

def test_one_of_three_reported_the_root_cannot_deliver_a_verified_verdict(
        clock, provider, children, config, tmp_path):
    config('each')
    agent = _agent(provider, tmp_path, 'p5-alfred')
    batch = dispatch_batch(agent, provider, GOALS)
    children.release(0)
    child0 = f'{batch}:child:0'
    wait_row(child0)
    claim(child0)

    provider.queue[:] = [text('No spying. No intruder. The house is not on fire.')]
    result = completion_turn(agent, [child0], task_id='c-alfred')

    # transport stays "the turn finished"; the work status is the truth
    assert result['completed'] is True
    assert result['work_status'] == 'partial'
    reply = result['final_response']
    assert reply.startswith('No spying. No intruder.')
    assert 'still running' in reply
    assert 'Review the router posture' in reply and 'Read the local security logs' in reply
    assert 'Scan the LAN' not in reply.split('still running', 1)[1]
    assert re.search(r'\b2 of 3\b', reply)
    # nothing the user can read mentions the plumbing
    for word in ('limit', 'timeout', 'timed out', 'budget', 'backstop'):
        assert word not in reply.lower()
    # the root was told the same facts, as Clover's own note on this turn
    wire = last_user_wire(provider.captured[0])
    assert STATUS_NOTE in wire and 'Review the router posture' in wire
    # the stored user text is clean
    stored = [m for m in result['messages'] if m.get('role') == 'user'][-1]
    assert STATUS_NOTE not in stored['content']


def test_all_three_reported_cleanly_and_read_is_verified_without_a_footer(
        clock, provider, children, config, tmp_path):
    config('joined')
    agent = _agent(provider, tmp_path, 'p5-alfred-clean')
    batch = dispatch_batch(agent, provider, GOALS)
    children.release(0, 1, 2)
    row = wait_row(batch)
    assert row['state'] == 'completed'
    claim(batch)

    provider.queue[:] = [text('Everything checked out.')]
    result = completion_turn(agent, [batch], task_id='c-clean')

    assert result['work_status'] == 'verified_completion'
    assert result['final_response'] == 'Everything checked out.'
    assert STATUS_NOTE not in last_user_wire(provider.captured[0])


# ── a failed or cut-off child is a failure ─────────────────────────────────

@pytest.mark.parametrize('bad', [
    # an ordinary failure
    {'status': 'failed', 'summary': '', 'exit_reason': 'completed'},
    # Octavia: the child's turn ended on a refusal; the row still says
    # "completed" with a summary, paired with max_iterations
    {'status': 'completed', 'summary': SAFETY_ROW_SUMMARY, 'exit_reason': 'max_iterations',
     'truncated': True},
], ids=['failed', 'completed-but-cut-off'])
def test_a_failed_child_is_reported_as_not_done(
        bad, clock, provider, children, config, tmp_path):
    config('joined')
    agent = _agent(provider, tmp_path, 'p5-failed')
    batch = dispatch_batch(agent, provider, GOALS[:2])
    children.outcomes[1] = bad
    children.release(0, 1)
    row = wait_row(batch)
    # the hole being closed: the batch row itself reads as a success
    assert row['state'] == 'completed'
    claim(batch)

    provider.queue[:] = [text('Both helpers finished and everything is verified.')]
    result = completion_turn(agent, [batch], task_id='c-failed')

    assert result['work_status'] == 'incomplete'
    assert result['work_status'] != 'verified_completion'
    reply = result['final_response']
    assert 'did not finish properly' in reply and GOALS[1] in reply
    assert 'Treat that part as not done' in reply
    assert GOALS[0] not in reply.split('did not finish properly', 1)[1]
    assert 'Did not finish properly' in last_user_wire(provider.captured[0])


# ── Octavia: substantial integration goes back to a helper ─────────────────

def _work_script(tmp_path, n):
    return [tool('terminal', {'command': f'touch {tmp_path / f"w{i}.txt"}'}, f't{i}')
            for i in range(n)]


def test_substantial_integration_on_the_completion_turn_goes_to_a_helper(
        clock, provider, children, dispatches, config, tmp_path):
    from agent import work_lifecycle as wl

    config('joined')
    agent = _agent(provider, tmp_path, 'p5-octavia')
    batch = dispatch_batch(agent, provider, GOALS,
                           reply='Three helpers are on it; I will report back.')
    assert dispatches == [], 'the spy sees runtime dispatches, not the root\'s own'
    children.release(0, 1, 2)
    wait_row(batch)
    claim(batch)

    children.gate(0).clear()  # the hand-back's helper (task 0 of its own batch) keeps running
    provider.queue[:] = _work_script(tmp_path, 3) + [text('SHOULD NOT BE REACHED')]
    result = completion_turn(agent, [batch], task_id='c-octavia')

    # two short checks ran in this chat; the third was handed to a helper
    assert (tmp_path / 'w0.txt').exists() and (tmp_path / 'w1.txt').exists()
    assert not (tmp_path / 'w2.txt').exists()
    assert len(dispatches) == 1
    handed = dispatches[0]['tasks']
    assert len(handed) == 1 and handed[0]['goal'] == wl.HANDBACK_GOAL
    assert result['final_response'].startswith('Helpers are on it')
    assert result['turn_exit_reason'] == 'delegation_handoff'
    assert result['completed'] is True
    assert result['work_status'] == 'dispatch_ack'
    assert len(provider.captured) == 3, 'the runtime ended the turn; no fourth model call'
    roles = [m['role'] for m in result['messages']]
    assert all(not (a == b == 'user') for a, b in zip(roles, roles[1:]))


def test_a_short_check_on_the_completion_turn_stays_direct(
        clock, provider, children, dispatches, config, tmp_path):
    config('joined')
    agent = _agent(provider, tmp_path, 'p5-short')
    batch = dispatch_batch(agent, provider, GOALS)
    children.release(0, 1, 2)
    wait_row(batch)
    claim(batch)

    provider.queue[:] = _work_script(tmp_path, 2) + [text('Checked: all three agree.')]
    result = completion_turn(agent, [batch], task_id='c-short')

    assert (tmp_path / 'w0.txt').exists() and (tmp_path / 'w1.txt').exists()
    assert dispatches == [], 'no hand-back for a short check'
    assert result['final_response'] == 'Checked: all three agree.'
    assert result['work_status'] == 'verified_completion'


def test_the_hand_backs_own_completion_is_never_handed_back_again(
        clock, provider, children, dispatches, config, tmp_path):
    config('joined')
    agent = _agent(provider, tmp_path, 'p5-loop')
    batch = dispatch_batch(agent, provider, GOALS)
    children.release(0, 1, 2)
    wait_row(batch)
    claim(batch)
    provider.queue[:] = _work_script(tmp_path, 3) + [text('unused')]
    completion_turn(agent, [batch], task_id='c-first')
    assert len(dispatches) == 1
    integration = fl.own_dispatch_receipts(agent)[-1].delegation_id
    assert integration != batch
    wait_row(integration)
    claim(integration)
    provider.captured.clear()

    # the integration helper reports; the root now does MORE than a short check
    more = [tool('terminal', {'command': f'touch {tmp_path / f"m{i}.txt"}'}, f'm{i}')
            for i in range(4)]
    provider.queue[:] = more + [text('Final report: all verified.')]
    result = completion_turn(agent, [integration], task_id='c-second')

    assert len(dispatches) == 1, 'no acknowledge/dispatch loop'
    assert all((tmp_path / f'm{i}.txt').exists() for i in range(4))
    assert result['final_response'] == 'Final report: all verified.'
    assert result['turn_exit_reason'] != 'delegation_handoff'


def test_another_generations_completion_is_not_ours_to_hand_back(
        clock, provider, children, dispatches, config, tmp_path):
    config('joined')
    agent = _agent(provider, tmp_path, 'p5-gen')
    batch = dispatch_batch(agent, provider, GOALS)
    children.release(0, 1, 2)
    wait_row(batch)
    claim(batch)
    reborn = _agent(provider, tmp_path, 'p5-gen')  # no receipts: not its dispatch
    provider.queue[:] = _work_script(tmp_path, 4) + [text('done')]

    result = completion_turn(reborn, [batch], task_id='c-reborn')

    assert dispatches == []
    assert result.get('work_status') is None


# ── Stop and unrelated chat are untouched ──────────────────────────────────

def test_a_new_unrelated_question_is_answered_plainly_while_helpers_run(
        clock, provider, children, config, tmp_path):
    config('joined')
    agent = _agent(provider, tmp_path, 'p5-chat')
    dispatch_batch(agent, provider, GOALS)

    provider.queue[:] = [text('Paris.')]
    result = agent.run_conversation('what is the capital of France?',
                                    conversation_history=[], task_id='chat')

    assert result['final_response'] == 'Paris.'
    assert result.get('work_status') is None
    assert STATUS_NOTE not in last_user_wire(provider.captured[0])


def test_stop_interrupts_the_completion_turn_and_adds_nothing(
        clock, provider, children, config, tmp_path):
    config('each')
    agent = _agent(provider, tmp_path, 'p5-stop')
    batch = dispatch_batch(agent, provider, GOALS)
    children.release(0)
    child0 = f'{batch}:child:0'
    wait_row(child0)
    claim(child0)

    def stop_on_first_call(count, request):
        if count == 1:
            agent.interrupt('stop')

    provider.before = stop_on_first_call
    provider.queue[:] = [text('No spying. No intruder.')]
    result = completion_turn(agent, [child0], task_id='c-stop')

    assert result['interrupted'] is True
    assert 'still running' not in (result.get('final_response') or '')


def test_helper_control_calls_are_never_refused_on_an_armed_completion_turn(
        clock, provider, children, config, tmp_path):
    from agent import router_backstop as rb
    from agent import work_lifecycle as wl

    config('each')
    agent = _agent(provider, tmp_path, 'p5-control')
    batch = dispatch_batch(agent, provider, GOALS)
    children.release(0)
    child0 = f'{batch}:child:0'
    wait_row(child0)
    claim(child0)
    provider.queue[:] = [text('ok')]
    completion_turn(agent, [child0], task_id='c-armed')

    # re-arm exactly as the completion turn does, then exhaust the short check
    wl.begin_turn(agent, 'helper finished', INTERNAL, [child0])
    state = rb._state(agent)
    assert state is not None, 'the completion turn arms the hand-back'
    state.work_calls = state.settings.backstop_calls

    assert rb.refusal(agent, 'delegate_task', {'action': 'list'}) is None
    assert rb.refusal(agent, 'delegate_task',
                      {'action': 'stop', 'delegation_id': batch}) is None


# ── the ledger shapes the report must read ─────────────────────────────────

def test_helpers_dispatched_separately_in_one_turn_are_one_request(
        clock, provider, children, config, tmp_path):
    """Octavia: separate dispatches for one request; one reports, one runs."""
    config('each')
    children.holds['HomePod firmware'] = threading.Event()
    agent = _agent(provider, tmp_path, 'p5-siblings')
    provider.queue[:] = [
        tool('delegate_task', {'goal': 'Install REA and run a smoke test',
                               'background': True}, 'd1'),
        tool('delegate_task', {'goal': 'Research the HomePod firmware format',
                               'background': True}, 'd2'),
        text('Both helpers are on it.'),
    ]
    agent.run_conversation('set up REA and look into the HomePod', conversation_history=[],
                           task_id='h-sib')
    ids = re.findall(r'"delegation_id":\s*"([^"]+)"', all_tool_text(provider))
    assert len(set(ids)) == 2, ids
    provider.captured.clear()
    first = fl.own_dispatch_receipts(agent)[0].delegation_id
    children.release(0)  # the install finishes; the research is held
    wait_row(first)
    claim(first)

    provider.queue[:] = [text('REA is installed and HomePod research is complete.')]
    result = completion_turn(agent, [first], task_id='c-sib')

    assert result['work_status'] == 'partial'
    assert 'still running' in result['final_response']


def test_a_straggler_slot_with_no_result_yet_is_pending_not_failed(clock, tmp_path):
    from agent.work_lifecycle import BAD, OK, PENDING, slots_for

    now = time.time()
    batch = 'deleg_straggler'
    results = [
        {'task_index': 0, 'status': 'completed', 'summary': 'ok', 'exit_reason': 'completed'},
        {'task_index': 1, 'status': 'incomplete', 'incomplete': True, 'summary': None},
    ]
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            'INSERT INTO async_delegations (delegation_id, origin_session, state, '
            'dispatched_at, completed_at, updated_at, result_json, delivery_state) '
            "VALUES (?, 's', 'unknown', ?, ?, ?, ?, 'pending')",
            (batch, now, now, now, json.dumps({'results': results})),
        )

    slots = slots_for(batch, ['first task', 'second task'])

    assert [s.outcome for s in slots] == [OK, PENDING]
    # the same batch stalled for good is a failure
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET state='stalled' WHERE delegation_id=?", (batch,))
    assert [s.outcome for s in slots_for(batch, ['first task', 'second task'])] == [OK, BAD]
