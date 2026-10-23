"""Malformed model reports remain unknown through actual completion consumers."""
import json
import pytest
from tests.tools.test_delegation_task_outcome import run_child
from agent.delegation_activity import DelegationActivityTracker


@pytest.mark.parametrize('status', [[], {}, ['done'], None, 1, True])
@pytest.mark.parametrize('wire', ['structured', 'text'])
def test_malformed_status_survives_child_callback_and_ui(status, wire):
    report = {'status': status, 'remaining': [], 'owner': 'owner', 'next_step': 'review'}
    summary = 'Task outcome: ' + json.dumps(report) if wire == 'text' else 'Work ended.'
    entry, events = run_child(summary, **({'task_outcome': report} if wire == 'structured' else {}))
    assert entry['status'] == 'completed'
    assert entry['task_outcome'] == {'status': 'unknown'}
    event = next(kw for event, kw in events if event == 'subagent.complete')
    tracker = DelegationActivityTracker()
    tracker.observe('subagent.complete', delegation_id='bad-report', task_index=0, **event)
    assert tracker.snapshot('bad-report')[0]['state'] == 'incomplete'


@pytest.mark.parametrize('field,value', [
    ('remaining', {}), ('remaining', [None]), ('owner', []), ('owner', None),
    ('next_step', {}), ('next_step', 7), ('remaining', ['x'] * 41),
    ('owner', 'x' * 501), ('next_step', 'x' * 2001),
])
def test_other_malformed_schema_fields_fail_closed(field, value):
    report = {'status': 'partial', 'remaining': ['check'], 'owner': 'owner', 'next_step': 'review'}
    report[field] = value
    entry, _ = run_child('Work ended.', task_outcome=report)
    assert entry['task_outcome'] == {'status': 'unknown'}
