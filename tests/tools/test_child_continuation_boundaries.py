"""Continuation cannot overtake an explicit owner boundary or Stop signal."""
import json
import threading
import pytest
from agent import step_continuation as sc
from tools import delegate_tool as dt
from tests.tools.test_delegate_auto_continue import _Child, _Parent, _leg

REPORT = {'status': 'blocked', 'remaining': ['owner approval'], 'owner': 'human owner',
          'next_step': 'Approve the read-only check?'}


@pytest.mark.parametrize('reason', ['text_response', 'budget_exhausted'])
@pytest.mark.parametrize('wire', ['text', 'structured'])
@pytest.mark.parametrize('schema', [False, True])
def test_blocked_child_never_resumes_or_retries_schema(monkeypatch, reason, wire, schema):
    monkeypatch.setattr(dt, '_auto_continue_limit', lambda: 2)
    first = _leg('The task remains unfinished.', exhausted=reason == 'budget_exhausted')
    first['turn_exit_reason'] = reason
    if wire == 'structured':
        first['task_outcome'] = REPORT
    else:
        first['final_response'] += '\nTask outcome: ' + json.dumps(REPORT)
    child = _Child([first, _leg('should never run', exhausted=False)])
    if schema:
        child._delegate_output_schema = {'type': 'object', 'required': ['answer']}
    budget = child.iteration_budget
    entry = dt._run_single_child(0, 'Do only the approved check', child, _Parent())
    assert entry['task_outcome'] == REPORT
    assert len(child.calls) == 1
    assert child.iteration_budget is budget
    assert entry.get('schema_retries', 0) == 0
    if schema:
        assert entry['schema_valid'] is False


@pytest.mark.parametrize('boundary', ['interrupted', 'failed', 'error', 'live_stop', 'hard_stop'])
@pytest.mark.parametrize('schema', [False, True])
def test_stop_and_error_prevent_all_same_child_retry_paths(monkeypatch, boundary, schema):
    monkeypatch.setattr(dt, '_auto_continue_limit', lambda: 2)
    first = _leg('The task remains unfinished.', exhausted=True)
    child = _Child([first, _leg('must not run', exhausted=False)])
    if boundary == 'live_stop':
        child._interrupt_requested = True
    elif boundary == 'hard_stop':
        child._hard_interrupt_requested = threading.Event()
        child._hard_interrupt_requested.set()
    else:
        first[boundary] = 'stop' if boundary == 'error' else True
    if schema:
        child._delegate_output_schema = {'type': 'object'}
    dt._run_single_child(0, 'approved check', child, _Parent())
    assert len(child.calls) == 1


def test_partial_child_can_continue_once_with_original_safety_context(monkeypatch):
    monkeypatch.setattr(dt, '_auto_continue_limit', lambda: 2)
    report = dict(REPORT, status='partial', owner='worker', next_step='Run the approved checks')
    first = _leg('The task remains unfinished.', exhausted=False)
    first['task_outcome'] = report
    child = _Child([first, lambda h: _leg('Result: done', exhausted=False, prior=h)])
    entry = dt._run_single_child(0, 'approved checks', child, _Parent())
    assert len(child.calls) == 2
    assert entry['auto_continuations'] == 1
    assert entry['task_outcome']['status'] == 'done'


def test_direct_shared_continuation_stops_when_next_leg_reports_blocked():
    first = _leg('partial', exhausted=True)
    calls = []
    child = _Child([])
    def resume(message, history):
        calls.append(message)
        result = _leg('The task remains unfinished.', exhausted=True, prior=history)
        result['task_outcome'] = REPORT
        return result
    out = sc.continue_until_done(child, first, limit=4, run=resume)
    assert len(calls) == 1
    assert out['task_outcome'] == REPORT
