"""Only stand-alone reports outside examples carry outcome evidence."""
import json
import pytest
from agent.task_outcome import task_outcome
from tests.tools.test_delegation_task_outcome import run_child

REPORT = {'status': 'blocked', 'remaining': ['approval'], 'owner': 'owner',
          'next_step': 'Approve this check?'}


@pytest.mark.parametrize('opening,closing', [('```text', '```'), ('~~~', '~~~'), ('````', '````'), ('   ```', '   ```'), ('```', '')])
@pytest.mark.parametrize('line', ['Result: done', 'Task outcome: ' + json.dumps(dict(REPORT, status='done', remaining=[]))])
def test_fenced_examples_are_not_success(opening, closing, line):
    text = f'For example:\n{opening}\n{line}\n{closing}\nThe task remains unverified.'
    entry, _ = run_child(text)
    assert entry['task_outcome'] == {'status': 'unknown'}


@pytest.mark.parametrize('quoted', ['> Result: done', '    Result: done', '\tResult: done', '"Result: done"', '> ```\n> Result: done\n> ```'])
def test_quoted_and_indented_examples_are_not_success(quoted):
    assert task_outcome({}, quoted) == {'status': 'unknown'}


def test_authoritative_report_after_fenced_example_still_works():
    text = '```\nResult: done\n```\nTask outcome: ' + json.dumps(REPORT) + '\nResult: not done'
    assert task_outcome({}, text) == REPORT


@pytest.mark.parametrize('bad', ['Task outcome: {bad json}', 'Task outcome: []', 'Task outcome: null', 'Task outcome: {}'])
def test_malformed_explicit_report_cannot_fall_back_to_success(bad):
    assert task_outcome({}, bad + '\nResult: done') == {'status': 'unknown'}


@pytest.mark.parametrize('status', ['partial', 'blocked'])
def test_explicit_incomplete_report_is_not_weakened_by_legacy_success(status):
    report = dict(REPORT, status=status)
    assert task_outcome({}, 'Task outcome: ' + json.dumps(report) + '\nResult: done') == report


def test_conflicting_legacy_verdicts_stay_unknown():
    assert task_outcome({}, 'Result: done\nResult: not done') == {'status': 'unknown'}


def test_legacy_success_outside_example_keeps_exact_compatibility():
    assert task_outcome({}, '```\nResult: not done\n```\nResult: done') == {'status': 'done'}
