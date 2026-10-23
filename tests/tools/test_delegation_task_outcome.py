"""Task success is independent of a clean model/lifecycle exit."""
from unittest.mock import MagicMock

import pytest

from tools.delegate_tool import _run_single_child
from agent.delegation_activity import DelegationActivityTracker


def run_child(report, **extra):
    child = MagicMock()
    child._credential_pool = None
    child._delegate_saved_tool_names = []
    child._delegate_task_id = None
    child.run_conversation.return_value = {
        'final_response': report, 'completed': True, 'api_calls': 1, **extra,
    }
    events = []
    child.tool_progress_callback = lambda event, **kw: events.append((event, kw))
    return _run_single_child(0, 'Finish the accepted task', child=child), events


def test_clean_exit_with_explicit_not_done_is_partial_not_success():
    entry, events = run_child('Result: not done\nApproval is still required.\nPlain summary: I need approval.')
    assert entry['status'] == 'completed'
    assert entry['exit_reason'] == 'completed'
    assert entry.get('task_outcome', {}).get('status') == 'partial'
    event = next(kw for event, kw in events if event == 'subagent.complete')
    assert event['task_outcome']['status'] == 'partial'
    tracker = DelegationActivityTracker()
    tracker.observe('subagent.complete', delegation_id='synthetic', task_index=0, **event)
    assert tracker.snapshot('synthetic')[0]['state'] == 'incomplete'


@pytest.mark.parametrize('outcome', ['partial', 'blocked'])
def test_structured_outcome_keeps_remaining_owner_and_next_step(outcome):
    report = {'status': outcome, 'remaining': ['approval and remote verification'],
              'owner': 'human owner', 'next_step': 'Approve the bounded check'}
    entry, events = run_child('Work report.\nPlain summary: I kept the work paused.', task_outcome=report)
    assert entry.get('task_outcome') == report
    event = next(kw for event, kw in events if event == 'subagent.complete')
    assert event['task_outcome'] == report
    tracker = DelegationActivityTracker()
    tracker.observe('subagent.complete', delegation_id='synthetic', task_index=0, **event)
    row = tracker.snapshot('synthetic')[0]
    assert row['state'] == 'incomplete'
    assert outcome in row['reason']


def test_prose_mention_is_not_a_verdict():
    entry, events = run_child('The document contains "Result: not done" as an example.\nPlain summary: I reviewed it.')
    assert entry.get('task_outcome', {}).get('status') == 'unknown'
    event = next(kw for event, kw in events if event == 'subagent.complete')
    tracker = DelegationActivityTracker()
    tracker.observe('subagent.complete', delegation_id='synthetic', task_index=0, **event)
    assert tracker.snapshot('synthetic')[0]['state'] == 'incomplete'


def test_failed_provider_exit_cannot_report_task_success():
    entry, _ = run_child('Result: done', failed=True, error='provider failure')
    assert entry['status'] == 'failed'
    assert entry.get('task_outcome', {}).get('status') != 'done'


def test_child_prompt_requests_explicit_outcome_and_followup_fields():
    from tools.delegate_tool import _build_child_system_prompt
    prompt = _build_child_system_prompt('finish the task')
    for field in ['Task outcome:', 'remaining', 'owner', 'next_step', 'blocked', 'partial']:
        assert field in prompt


@pytest.mark.unified_card
def test_json_report_survives_callback_and_unified_card_without_completing_steps():
    import json
    from agent import ack_checklist as ac
    from agent.foreground_time_limit import DispatchReceipt
    report = {'status': 'blocked', 'remaining': ['human approval'],
              'owner': 'human owner', 'next_step': 'Review the request'}
    entry, events = run_child('Task outcome: ' + json.dumps(report) + '\nResult: not done\nPlain summary: I need approval.')
    assert entry['task_outcome'] == report
    event = next(kw for event, kw in events if event == 'subagent.complete')
    ac.reset_for_tests()
    ac.set_style_for_tests(ac.STYLE_UNIFIED)
    try:
        receipt = DispatchReceipt(delegation_id='card', seq=1, goals=('goal',),
                                  summary='Checking reliability', steps=('check', 'approval'))
        card = ac.card_for([receipt])
        assert card is not None
        ac.on_child_event('subagent.complete', delegation_id='card', **event)
        assert card.finished
        body = card.render()
        assert 'blocked' in body
        assert 'approval' in body
        assert '✓ approval' not in body
    finally:
        ac.reset_for_tests()
        ac.set_style_for_tests(None)


@pytest.mark.parametrize('raw', [
    {'status': 'done', 'remaining': ['not finished'], 'owner': 'owner', 'next_step': 'review'},
    {'status': 'blocked', 'remaining': [], 'owner': 'owner', 'next_step': 'review'},
    {'status': 'partial', 'remaining': [42], 'owner': 'owner', 'next_step': 'review'},
    {'status': 'done'},
])
def test_malformed_or_inconsistent_reports_do_not_claim_task_success(raw):
    entry, _ = run_child('Plain summary: Work ended.', task_outcome=raw)
    assert entry['task_outcome']['status'] == 'unknown'


@pytest.mark.parametrize('batch', [False, True])
def test_completion_prompt_keeps_structured_blocker_and_never_marks_it_success(batch):
    from tools.process_registry import _format_async_delegation, async_delegation_not_done
    report = {'status': 'blocked', 'remaining': ['owner approval'],
              'owner': 'human owner', 'next_step': 'Approve the bounded check?'}
    item = {'status': 'completed', 'summary': 'Saved work.', 'task_outcome': report}
    event = {'is_batch': True, 'results': [dict(task_index=0, **item)]} if batch else item
    assert async_delegation_not_done(event)
    prompt = _format_async_delegation(event)
    assert '✓ TASK' not in prompt
    for field in ['blocked', 'owner approval', 'human owner', 'Approve the bounded check?']:
        assert field in prompt
    assert 'already-authorized' in prompt
