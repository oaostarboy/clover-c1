"""Saved runtime restrictions survive real async completion, without a provider."""
import threading
import time

import pytest

from agent import delegation_checkpoint as dc
from tests.agent.test_delegation_checkpoint import _agent, _delegate, run_batch
from tools import async_delegation as asyncd


@pytest.mark.parametrize('drift', [
    'unchanged', 'disabled_tools', 'loaded_tools', 'approval_config',
    'session_yolo', 'process_yolo', 'session_allowlist', 'permanent_allowlist',
    'child_delegation',
])
def test_completed_plan_cannot_relax_original_runtime_restrictions(tmp_path, monkeypatch, drift):
    import run_agent
    from tools import approval, delegate_tool

    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    config = {'mode': 'manual'}
    monkeypatch.setattr(approval, '_get_approval_config', lambda: dict(config))
    monkeypatch.setattr(approval, '_session_approved', {})
    monkeypatch.setattr(approval, '_permanent_approved', set())
    monkeypatch.setattr(approval, '_session_yolo', set())
    monkeypatch.setattr(approval, '_YOLO_MODE_FROZEN', False)
    monkeypatch.setattr(delegate_tool, '_get_orchestrator_enabled', lambda: False)
    released = threading.Event()
    started = threading.Event()
    children = []

    def offline_conversation(self, **kwargs):
        children.append(set(self.valid_tool_names))
        started.set()
        assert released.wait(10)
        return {'final_response': 'test-owned completion', 'messages': [],
                'completed': True, 'api_calls': 1}

    monkeypatch.setattr(run_agent.AIAgent, 'run_conversation', offline_conversation)
    agent = _agent(disabled_toolsets=['file'])
    try:
        dc.begin_turn(agent)
        stage = {'goal': 'verify within the admitted capability surface'}
        _, result = run_batch(agent, [_delegate('bounded capability'),
            ('delegate_task', {'goal': 'inspect without file access', 'follow_through': [stage]})])
        assert result['status'] == 'dispatched'
        assert started.wait(10)
        assert 'write_file' not in children[0]
        did = result['delegation_id']
        assert asyncd.get_continuation_plan(did)
        released.set()
        deadline = time.monotonic() + 10
        row = None
        while time.monotonic() < deadline:
            row = asyncd.get_durable_delegation(did)
            if row and row['state'] not in ('running', 'finalizing'):
                break
            time.sleep(.01)
        assert row is not None and row['state'] == 'completed'
        assert asyncd.claim_completion_delivery(did, 'capability-policy-test')
        key = approval.get_current_session_key(default='') or agent.session_id
        if drift == 'disabled_tools':
            agent.disabled_toolsets = []
        elif drift == 'loaded_tools':
            agent.valid_tool_names = set(agent.valid_tool_names) | {'write_file'}
        elif drift == 'approval_config':
            config['mode'] = 'off'
        elif drift == 'session_yolo':
            approval._session_yolo.add(key)
        elif drift == 'process_yolo':
            monkeypatch.setattr(approval, '_YOLO_MODE_FROZEN', True)
        elif drift == 'session_allowlist':
            approval._session_approved[key] = {'dangerous-new-pattern'}
        elif drift == 'permanent_allowlist':
            approval._permanent_approved.add('dangerous-new-pattern')
        elif drift == 'child_delegation':
            monkeypatch.setattr(delegate_tool, '_get_orchestrator_enabled', lambda: True)
        dc.begin_turn(agent, 'internal_notification')
        (next_result,) = run_batch(agent, [('delegate_task', stage)])
        if drift == 'unchanged':
            assert next_result['status'] == 'dispatched'
        else:
            assert next_result.get('error'), next_result
            assert len(children) == 1, 'changed restrictions must reject before child construction'
    finally:
        released.set()
        deadline = time.monotonic() + 10
        while asyncd.active_count() and time.monotonic() < deadline:
            time.sleep(.01)
        assert not asyncd.active_count()
        agent.close()
