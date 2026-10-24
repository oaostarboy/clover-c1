"""Diagnostic contract tests, not a native origin success or adapter admission."""
import copy
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def load():
    spec = importlib.util.spec_from_file_location('task_origin', HERE / 'task_origin.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def environment() -> dict:
    # Explicitly modeled API observations: never alleged captured Windows data.
    token = dict(sid='S-1-5-21-1-2-3-1001', session_id=1, elevation_type=3,
                 elevated=False, integrity_sid='S-1-16-8192', interactive_group=True,
                 logon_type=2, logon_session_id=1, logon_sid='S-1-5-21-1-2-3-1001',
                 errors=[])
    return dict(errors=[], current=token, linked=None,
                sessions=[dict(session_id=1, state=0, sid=token['sid'], error=None)])


def test_measured_medium_interactive_context_is_not_task_acceptance():
    result = load().session_diagnostic(environment())
    assert result['session_candidate'] == 'CURRENT_MEDIUM_INTERACTIVE'
    assert result['native_acceptance'] is False
    assert result['public_registration_attempted'] is False
    assert result['review_required'] is True


@pytest.mark.parametrize('mutation', ['no-wts', 'disconnected', 'foreign-wts', 'session-zero',
    'wrong-logon', 'foreign-logon', 'different-logon-session', 'not-interactive',
    'elevated', 'high-integrity', 'errors', 'duplicate-wts', 'missing-native-value'])
def test_no_session_inference_or_fabricated_medium_token(mutation):
    env = environment()
    if mutation == 'no-wts': env['sessions'] = []
    elif mutation == 'disconnected': env['sessions'][0]['state'] = 4
    elif mutation == 'foreign-wts': env['sessions'][0]['sid'] = 'S-1-5-18'
    elif mutation == 'session-zero': env['current']['session_id'] = 0
    elif mutation == 'wrong-logon': env['current']['logon_type'] = 4
    elif mutation == 'foreign-logon': env['current']['logon_sid'] = 'S-1-5-18'
    elif mutation == 'different-logon-session': env['current']['logon_session_id'] = 2
    elif mutation == 'not-interactive': env['current']['interactive_group'] = False
    elif mutation == 'elevated': env['current']['elevated'] = True
    elif mutation == 'high-integrity': env['current']['integrity_sid'] = 'S-1-16-12288'
    elif mutation == 'errors': env['current']['errors'] = ['native-call-error']
    elif mutation == 'duplicate-wts': env['sessions'] *= 2
    elif mutation == 'missing-native-value': del env['current']['logon_type']
    assert load().session_diagnostic(env)['session_candidate'] == 'NONE'


def test_actual_linked_medium_requires_separate_review_not_automatic_reuse():
    env = environment()
    env['linked'] = copy.deepcopy(env['current'])
    env['current'].update(elevation_type=2, elevated=True, integrity_sid='S-1-16-12288')
    result = load().session_diagnostic(env)
    assert result['session_candidate'] == 'EXISTING_LINKED_MEDIUM_REQUIRES_REVIEW'
    assert result['review_required'] is True
    assert result['public_registration_attempted'] is False
    env['linked']['sid'] = 'S-1-5-18'
    assert load().session_diagnostic(env)['session_candidate'] == 'NONE'


def test_new_capture_workflow_is_single_push_only_readonly_and_always_uploads():
    import yaml
    workflow = yaml.safe_load((HERE.parent / 'workflows/windows-task-origin-capture-159.yml').read_text())
    trigger = workflow.get('on', workflow.get(True))
    assert set(trigger) == {'push'}
    assert trigger['push']['branches'] == ['proof/windows-task-origin-capture-159']
    assert workflow['permissions'] == {'contents': 'read'}
    job = workflow['jobs']['capture']
    assert job['runs-on'] == 'windows-2025'
    checkout = job['steps'][0]
    assert checkout['with']['persist-credentials'] is False
    assert job['steps'][-1]['if'] == 'always()'
    text = (HERE / 'task_origin_capture.ps1').read_text()
    assert text.index('$collector.SubscribeChannels(') < text.index('Start-Process')
    assert text.index('native-environment.json') < text.index('$collector.SubscribeChannels(')
    assert "$env:GITHUB_RUN_ATTEMPT -ne '1'" in text
    for forbidden in ('Register-ScheduledTask', 'CreateRestrictedToken', 'CreateProcessWithLogon',
                      'Stop-Process', 'Set-ItemProperty', 'auditpol', 'wevtutil sl', 'gateway install'):
        assert forbidden not in text


def test_native_readonly_source_has_wts_lsa_and_no_token_fabrication():
    text = (HERE / 'task_origin_session.cs').read_text()
    for api in ('WTSEnumerateSessionsW', 'WTSQuerySessionInformationW', 'LsaGetLogonSessionData',
                'GetTokenInformation', 'CloseHandle', 'TokenLinkedToken', 'TokenIntegrityLevel'):
        assert api in text
    for forbidden in ('CreateRestrictedToken', 'LogonUser', 'CreateProcess', 'DuplicateToken', 'AdjustTokenPrivileges'):
        assert forbidden not in text
