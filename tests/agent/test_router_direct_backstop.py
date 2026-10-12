"""The direct-mode safety backstop (``delegation.router.direct_backstop``).

A "direct" verdict used to disarm the runtime backstop, so a mis-sized job ran
until the foreground time limit.  Now the runtime counts a direct turn's work
calls and, at ``work_calls`` calls or ``seconds`` seconds, hands the REST of the
request to exactly one helper through the real ``_dispatch`` / receipt path.

Real ``AIAgent._execute_tool_calls``, real tools, real ``delegate_task`` handler,
child construction, pool and runner; only a child's model conversation is faked.
Clock is fake.
"""
from __future__ import annotations

import json
import threading

import pytest

import tools.async_delegation as ad
from agent import delegation_router as dr
from agent import foreground_time_limit as fl
from agent import router_backstop as rb
from tests.agent.test_delegation_checkpoint import run_batch
from tests.agent.test_router_backstop import (  # noqa: F401  (fixtures)
    _clean_registry,
    _is_handoff,
    _root,
    _wait_started,
    _write,
    children,
    clock,
    dispatches,
)

pytestmark = pytest.mark.real_delegation_router

REQUEST = 'Email Julia the extension, with the instructions I gave you.'
DIRECT = dr.Verdict(mode='direct', tasks=(), reason='one send after a quick look')
SETTINGS = dr.RouterSettings(enabled=True, timeout_seconds=5.0)  # shipped direct defaults


def _arm(agent, *, message=REQUEST, settings=SETTINGS, verdict=DIRECT):
    rb.begin(agent, verdict, message, settings)


def _writes(tmp_path, n, start=0):
    return [_write(tmp_path, f'{i}.txt') for i in range(start, start + n)]


# ── the shipped defaults ───────────────────────────────────────────────────

def test_the_defaults_are_25_calls_180_seconds_and_on(monkeypatch):
    monkeypatch.setenv('CLOVER_IGNORE_USER_CONFIG', '1')
    s = dr.load_settings()
    assert (s.direct_backstop, s.direct_backstop_calls, s.direct_backstop_seconds) == (
        True, 25, 180.0)
    from clover_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG['delegation']['router']['direct_backstop'] == {
        'enabled': True, 'work_calls': 25, 'seconds': 180}


def _config(monkeypatch, router):
    import yaml
    from clover_constants import get_clover_home

    monkeypatch.delenv('CLOVER_IGNORE_USER_CONFIG', raising=False)
    (get_clover_home() / 'config.yaml').write_text(
        yaml.safe_dump({'delegation': {'router': router}}))
    return dr.load_settings()


def test_settings_are_read_from_config(monkeypatch):
    s = _config(monkeypatch, {'direct_backstop': {'enabled': False, 'work_calls': 7,
                                                  'seconds': 45}})
    assert (s.direct_backstop, s.direct_backstop_calls, s.direct_backstop_seconds) == (
        False, 7, 45.0)
    # the other router keys are untouched by it
    assert (s.backstop_calls, s.backstop_seconds) == (2, 60.0)


@pytest.mark.parametrize('off', [0, False])
def test_seconds_zero_or_false_turns_the_time_trigger_off_only(monkeypatch, off):
    s = _config(monkeypatch, {'direct_backstop': {'seconds': off}})
    assert s.direct_backstop is True and s.direct_backstop_calls == 25
    assert s.direct_backstop_seconds is None


@pytest.mark.parametrize('raw', [
    {'direct_backstop': {'enabled': 'sure', 'work_calls': 'many', 'seconds': True}},
    {'direct_backstop': {'work_calls': -4, 'seconds': 'soon'}},
    {'direct_backstop': 'maybe'},
    {'direct_backstop': None},
    {},
])
def test_garbage_falls_back_to_the_defaults(monkeypatch, raw):
    s = _config(monkeypatch, raw)
    assert s.direct_backstop is True and s.direct_backstop_calls == 25
    assert s.direct_backstop_seconds == 180.0


def test_a_bare_false_switches_it_off(monkeypatch):
    assert _config(monkeypatch, {'direct_backstop': False}).direct_backstop is False


# ── thresholds ─────────────────────────────────────────────────────────────

def test_24_work_calls_run_and_so_does_the_25th(tmp_path, children, dispatches):
    agent = _root()
    _arm(agent)

    results = run_batch(agent, _writes(tmp_path, 25))

    assert not any(_is_handoff(r) for r in results)
    assert dispatches == []
    assert all((tmp_path / f'{i}.txt').exists() for i in range(25))


def test_the_next_admission_after_25_calls_hands_off_once(tmp_path, children, dispatches):
    agent = _root()
    _arm(agent)
    run_batch(agent, _writes(tmp_path, 25))

    (handed,) = run_batch(agent, _writes(tmp_path, 1, start=25))

    assert _is_handoff(handed)
    assert not (tmp_path / '25.txt').exists(), 'the refused call did not run'
    assert len(dispatches) == 1
    _wait_started(children, 1)
    assert len(children) == 1, 'exactly one helper'


def test_calls_after_the_handoff_are_refused_with_it_and_never_dispatch_again(
        tmp_path, children, dispatches):
    agent = _root()
    _arm(agent)

    results = run_batch(agent, _writes(tmp_path, 30))

    assert [_is_handoff(r) for r in results] == [False] * 25 + [True] * 5
    assert len({r['delegation_id'] for r in results[25:]}) == 1
    assert len(dispatches) == 1
    (later,) = run_batch(agent, _writes(tmp_path, 1, start=40))
    assert _is_handoff(later) and len(dispatches) == 1


def test_work_calls_come_from_settings(tmp_path, children, dispatches):
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=3))

    results = run_batch(agent, _writes(tmp_path, 5))

    assert [_is_handoff(r) for r in results] == [False] * 3 + [True] * 2


def test_the_first_call_at_180_seconds_hands_off(tmp_path, children, dispatches, clock):
    agent = _root()
    _arm(agent)
    clock.t += 180

    (handed,) = run_batch(agent, _writes(tmp_path, 1))

    assert _is_handoff(handed) and len(dispatches) == 1
    assert not (tmp_path / '0.txt').exists()


def test_just_under_180_seconds_the_call_runs(tmp_path, children, dispatches, clock):
    agent = _root()
    _arm(agent)
    clock.t += 179

    (ran,) = run_batch(agent, _writes(tmp_path, 1))

    assert not _is_handoff(ran) and (tmp_path / '0.txt').exists()
    assert dispatches == []


def test_the_seconds_path_needs_no_call_count(tmp_path, children, dispatches, clock):
    agent = _root()
    _arm(agent)
    run_batch(agent, _writes(tmp_path, 3))
    clock.t += 200

    (handed,) = run_batch(agent, _writes(tmp_path, 1, start=3))

    assert _is_handoff(handed) and len(dispatches) == 1


def test_seconds_off_leaves_only_the_call_count(tmp_path, children, dispatches, clock):
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_seconds=None))
    clock.t += 3600

    results = run_batch(agent, _writes(tmp_path, 5))

    assert not any(_is_handoff(r) for r in results) and dispatches == []


# ── exemptions: the same as the planned path ───────────────────────────────

def test_control_plane_and_memory_calls_never_count(tmp_path, children, dispatches, clock):
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=2))
    todo = ('todo', {'todos': [{'id': '1', 'content': 'x', 'status': 'pending'}]})

    for _ in range(40):
        assert rb.refusal(agent, 'memory', {'action': 'add', 'target': 'memory',
                                             'content': 'x'}) is None
    results = run_batch(agent, [todo] * 30)
    assert not any(_is_handoff(r) for r in results)
    ran = run_batch(agent, _writes(tmp_path, 2))
    assert not any(_is_handoff(r) for r in ran), 'only two real work calls so far'
    assert dispatches == []
    (handed,) = run_batch(agent, _writes(tmp_path, 1, start=2))
    assert _is_handoff(handed)


def test_memory_past_the_threshold_never_hands_off(children, dispatches, clock):
    agent = _root()
    _arm(agent)
    clock.t += 3600

    assert rb.refusal(agent, 'memory', {'action': 'add', 'target': 'memory',
                                         'content': 'x'}) is None
    assert dispatches == []


# ── switched off, or the user asked for it ─────────────────────────────────

def test_disabled_is_todays_behaviour(tmp_path, children, dispatches, clock):
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop=False))
    clock.t += 3600

    results = run_batch(agent, _writes(tmp_path, 40))

    assert not any(_is_handoff(r) for r in results)
    assert dispatches == [] and rb._state(agent).planned is False
    assert rb._state(agent).direct is False
    assert rb.turn_receipt_floor(agent) is not None, 'the receipt floor is still recorded'


@pytest.mark.parametrize('message', [
    'Do it yourself: email Julia the extension.',
    'do this yourself please, check all the files',
])
def test_an_explicit_self_request_is_never_handed_off(tmp_path, children, dispatches, clock, message):
    agent = _root()
    _arm(agent, message=message)   # the router says direct; the words say self
    clock.t += 3600

    results = run_batch(agent, _writes(tmp_path, 30))

    assert not any(_is_handoff(r) for r in results) and dispatches == []


def test_an_override_verdict_is_never_handed_off(tmp_path, children, dispatches, clock):
    agent = _root()
    verdict = dr.Verdict(mode='direct', tasks=(), source='override', reason='you asked')
    _arm(agent, verdict=verdict, message='go ahead')
    clock.t += 3600

    results = run_batch(agent, _writes(tmp_path, 30))

    assert not any(_is_handoff(r) for r in results) and dispatches == []


def test_a_planned_verdict_still_uses_its_own_thresholds(tmp_path, children, dispatches):
    agent = _root()
    plan = dr.Verdict(mode='single', reason='r', tasks=(
        dr.PlanTask(goal='build the report', acceptance='the report exists'),))
    _arm(agent, verdict=plan, settings=dr.RouterSettings(
        enabled=True, backstop_calls=2, backstop_seconds=60.0))

    results = run_batch(agent, _writes(tmp_path, 4))

    assert [_is_handoff(r) for r in results] == [False, False, True, True]
    assert [t['goal'] for t in dispatches[0]['tasks']] == ['build the report']


# ── the root dispatched for itself ─────────────────────────────────────────

def test_a_root_that_dispatched_itself_gets_no_second_dispatch(tmp_path, children, dispatches):
    agent = _root()
    _arm(agent)

    own, *rest = run_batch(agent, [('delegate_task', {'goal': 'prepare the email'})]
                           + _writes(tmp_path, 30))

    assert own['status'] == 'dispatched'
    assert all(_is_handoff(r) for r in rest), 'the root stays out of the work'
    assert {r['delegation_id'] for r in rest} == {own['delegation_id']}
    assert dispatches == [], 'the runtime never dispatched'
    _wait_started(children, 1)
    assert len(children) == 1


def test_a_root_dispatch_exactly_at_the_threshold_is_not_doubled(tmp_path, children, dispatches):
    agent = _root()
    _arm(agent)
    run_batch(agent, _writes(tmp_path, 25))

    own, after = run_batch(agent, [('delegate_task', {'goal': 'finish the email'}),
                                   _write(tmp_path, 'after.txt')])

    assert own['status'] == 'dispatched'
    assert _is_handoff(after) and after['delegation_id'] == own['delegation_id']
    assert dispatches == []
    _wait_started(children, 1)
    assert len(children) == 1


def test_a_listing_is_not_a_dispatch(tmp_path, children, dispatches):
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=2))

    results = run_batch(agent, [('delegate_task', {'action': 'list'})] + _writes(tmp_path, 3))

    assert [_is_handoff(r) for r in results[1:]] == [False, False, True]
    assert len(dispatches) == 1


# ── Stop ───────────────────────────────────────────────────────────────────

def test_a_pending_stop_defers_the_handoff(tmp_path, children, dispatches, clock):
    agent = _root()
    _arm(agent)
    clock.t += 3600
    agent._interrupt_requested = True

    assert rb.refusal(agent, 'write_file', {'path': 'x', 'content': 'x'}) is None
    assert dispatches == []
    assert rb.completion_directive(agent) is None

    agent._interrupt_requested = False
    verdict = rb.refusal(agent, 'write_file', {'path': 'x', 'content': 'x'})
    assert verdict is not None and verdict.block_code == rb.HANDOFF_CODE
    assert len(dispatches) == 1


# ── failure ────────────────────────────────────────────────────────────────

def test_a_failed_dispatch_lets_the_root_carry_on(tmp_path, children, monkeypatch):
    from tools.registry import registry, tool_error

    calls = []
    real = registry.dispatch

    def failing(name, args, **kw):
        if name == 'delegate_task':
            calls.append(args)
            return tool_error('no capacity for helpers right now')
        return real(name, args, **kw)

    monkeypatch.setattr(registry, 'dispatch', failing)
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=3))

    results = run_batch(agent, _writes(tmp_path, 8))

    refused = results[3]
    assert not _is_handoff(refused)
    assert 'handed off' not in json.dumps(refused).lower()
    assert not (tmp_path / '3.txt').exists(), 'the refused call itself did not run'
    assert all((tmp_path / f'{i}.txt').exists() for i in (4, 5, 6, 7)), 'the root carries on'
    assert len(calls) == 1, 'no retry'
    assert children == []
    assert rb.completion_directive(agent) is None, 'no acknowledgment without a receipt'


def test_a_dispatch_that_raises_is_not_propagated(tmp_path, children, monkeypatch):
    from tools.registry import registry

    real = registry.dispatch

    def exploding(name, args, **kw):
        if name == 'delegate_task':
            raise RuntimeError('pool exploded')
        return real(name, args, **kw)

    monkeypatch.setattr(registry, 'dispatch', exploding)
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=1))

    results = run_batch(agent, _writes(tmp_path, 3))

    assert (tmp_path / '0.txt').exists() and (tmp_path / '2.txt').exists()
    assert not any(_is_handoff(r) for r in results)


# ── concurrent admission ───────────────────────────────────────────────────

def test_a_concurrent_batch_across_the_threshold_dispatches_once(tmp_path, children, dispatches):
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=5))

    results = run_batch(agent, _writes(tmp_path, 12))

    assert [_is_handoff(r) for r in results] == [False] * 5 + [True] * 7
    assert len(dispatches) == 1
    assert len({r['delegation_id'] for r in results[5:]}) == 1


def test_threads_admitting_at_the_threshold_dispatch_once(children, dispatches):
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=3))
    args = [{'path': f'p{i}', 'content': 'x'} for i in range(12)]
    verdicts: list = [None] * len(args)

    def admit(i):
        verdicts[i] = rb.refusal(agent, 'write_file', args[i])

    threads = [threading.Thread(target=admit, args=(i,)) for i in range(len(args))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)

    assert sum(v is None for v in verdicts) == 3
    assert all(v.block_code == rb.HANDOFF_CODE for v in verdicts if v is not None)
    assert len(dispatches) == 1


def test_calls_in_flight_at_the_handoff_are_listed_not_stopped(tmp_path, children, dispatches):
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=2))
    first, second = {'command': 'sleep 1; echo first'}, {'command': 'sleep 1; echo second'}
    assert rb.refusal(agent, 'terminal', first) is None
    assert rb.refusal(agent, 'terminal', second) is None

    verdict = rb.refusal(agent, 'terminal', {'command': 'echo third'})

    assert verdict is not None and verdict.block_code == rb.HANDOFF_CODE
    assert agent._interrupt_requested is False
    _wait_started(children, 1)
    blob = children[0]['system'] + str(children[0]['user'])
    assert 'Already started by the parent' in blob
    assert 'sleep 1; echo first' in blob and 'sleep 1; echo second' in blob


# ── what the helper is given ───────────────────────────────────────────────

def test_the_helper_gets_the_request_and_what_the_root_found(tmp_path, children, dispatches):
    notes = tmp_path / 'notes.txt'
    notes.write_text('FINDING-4471: the extension lives in the autohdr folder')
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=2))

    run_batch(agent, [('read_file', {'path': str(notes)})])
    run_batch(agent, [('read_file', {'path': str(notes)})])
    (handed,) = run_batch(agent, [('read_file', {'path': str(notes)})])

    assert _is_handoff(handed)
    (call,) = dispatches
    assert len(call['tasks']) == 1, 'a single-task plan'
    _wait_started(children, 1)
    blob = children[0]['system'] + str(children[0]['user'])
    assert REQUEST in blob, 'the user\'s message, verbatim'
    assert 'FINDING-4471' in blob, 'the findings travel'
    assert 'do not repeat the work' in blob
    assert 'Plan: direct' not in blob


def test_control_plane_results_are_not_findings(tmp_path, children, dispatches):
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=1))
    notes = tmp_path / 'n.txt'
    notes.write_text('REAL-FINDING-9')

    run_batch(agent, [('todo', {'todos': [{'id': 'a', 'content': 'TODO-NOISE', 'status': 'pending'}]}),
                      ('read_file', {'path': str(notes)})])
    run_batch(agent, [('read_file', {'path': str(notes)})])
    _wait_started(children, 1)

    blob = children[0]['system'] + str(children[0]['user'])
    assert 'REAL-FINDING-9' in blob and 'TODO-NOISE' not in blob


def test_a_call_in_the_same_batch_as_the_trip_is_listed_as_started(
        tmp_path, children, dispatches):
    notes = tmp_path / 'n.txt'
    notes.write_text('FINDING-IN-BATCH')
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=1))

    run_batch(agent, [('read_file', {'path': str(notes)}), _write(tmp_path, 'x.txt')])
    _wait_started(children, 1)

    blob = children[0]['system'] + str(children[0]['user'])
    assert 'Already started by the parent' in blob and 'read_file' in blob


# ── the acknowledgment ─────────────────────────────────────────────────────

def test_the_ack_is_the_normal_handoff_summary_and_says_nothing_about_limits(
        tmp_path, children, dispatches):
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=1))

    run_batch(agent, _writes(tmp_path, 2))
    directive = rb.completion_directive(agent)

    assert directive is not None and directive.reason == rb.EXIT_REASON
    text = directive.text
    assert '\U0001f504 ' in text
    assert 'emailing Julia the extension' in text, 'the card names the job, not a placeholder'
    assert 'taking over the rest of your request' not in text
    assert REQUEST not in text, 'the task text is never pasted into the ack'
    refusal = rb._verdict_for(rb._state(agent), 'write_file').block_message
    lowered = (text + ' ' + refusal).lower()
    for word in ('timeout', 'time limit', 'timed out', 'backstop', 'budget', 'limit'):
        assert word not in lowered
    # the helper may already have finished: any status except 'nothing dispatched'
    assert rb.work_status(agent) is not None


def test_the_dispatch_carries_the_handoff_summary(tmp_path, children, dispatches):
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=1))

    run_batch(agent, _writes(tmp_path, 2))

    sent = dict(dispatches[0]['handoff'])
    assert sent.pop('say'), 'the root opens in its own voice before the card'
    assert sent == rb._runtime_handoff(rb._state(agent).verdict, rb._state(agent).user_message)
    (receipt,) = rb.turn_receipts(agent)
    assert receipt.summary and 'emailing Julia the extension' in receipt.summary


# ── scope: the router's, unchanged ─────────────────────────────────────────

def test_a_new_turn_starts_clean(tmp_path, children, dispatches):
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=1))
    run_batch(agent, _writes(tmp_path, 2))
    assert len(dispatches) == 1

    rb.clear(agent)
    rb.begin(agent, DIRECT, 'next question', SETTINGS)
    results = run_batch(agent, _writes(tmp_path, 3, start=10))

    assert not any(_is_handoff(r) for r in results), 'a fresh count for the new message'
    assert len(dispatches) == 1


def test_a_helper_of_an_earlier_turn_does_not_count_for_this_one(tmp_path, children, dispatches):
    agent = _root()
    _arm(agent, settings=dr.RouterSettings(enabled=True, direct_backstop_calls=1))
    run_batch(agent, _writes(tmp_path, 2))
    assert len(dispatches) == 1

    _arm(agent, message='another message')   # the next human message
    results = run_batch(agent, _writes(tmp_path, 1, start=10))

    assert not any(_is_handoff(r) for r in results)


def test_the_router_scope_gates_still_apply(monkeypatch):
    """``plan_for_turn`` only arms inside the router's scope; out of scope the
    direct backstop is never armed (no detached delivery -> nothing to hand to)."""
    agent = _root()
    monkeypatch.setattr(fl, '_detached_delivery_available', lambda: False)
    note = dr.plan_for_turn(agent, REQUEST, [])
    assert note == ''
    state = rb._state(agent)
    assert state is None or (state.direct is False and state.planned is False)


# ── the model-facing note ──────────────────────────────────────────────────

def test_the_direct_note_no_longer_claims_no_helpers_are_needed():
    note = dr.work_plan_note(dr.Verdict(mode='direct', tasks=(), reason='one lookup'))

    assert note == f'{dr.RUNTIME_NOTE_MARKER} work plan: direct (one lookup). Handle it here.'
    lowered = note.lower()
    for phrase in ('no helpers', 'helpers are needed', 'yourself', 'limit', 'backstop', '25',
                   '180'):
        assert phrase not in lowered
    assert len(note) < 80


def test_the_direct_note_is_byte_stable_and_wrapped_as_a_runtime_note():
    verdict = dr.Verdict(mode='direct', tasks=(), reason='one lookup')

    assert dr.work_plan_note(verdict) == dr.work_plan_note(verdict)
    assert dr.work_plan_note(verdict).startswith(dr.RUNTIME_NOTE_MARKER)
    assert dr.work_plan_note(dr.Verdict(mode='direct', tasks=())) == (
        f'{dr.RUNTIME_NOTE_MARKER} work plan: direct. Handle it here.')


def test_arming_never_changes_the_note(tmp_path, children, dispatches):
    verdict = dr.Verdict(mode='direct', tasks=(), reason='one lookup')
    before = dr.work_plan_note(verdict)
    agent = _root()
    _arm(agent, verdict=verdict)
    run_batch(agent, _writes(tmp_path, 30))
    assert dr.work_plan_note(verdict) == before
