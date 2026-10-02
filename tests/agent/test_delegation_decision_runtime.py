"""Exercise delegation planning through real AIAgent tool dispatch and hydration.

No model responses are mocked: these tests only execute local planning tools.
Real-provider behavior is separately exercised by the live smoke probe.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from agent.system_prompt import build_system_prompt_parts
from run_agent import AIAgent


def _agent() -> Any:
    return AIAgent(
        provider='custom', api_mode='chat_completions',
        base_url='http://127.0.0.1:1/v1', api_key='local-test-credential',
        model='chosen-orchestrator', model_pinned=True,
        enabled_toolsets=['todo', 'delegation'], max_iterations=3,
        quiet_mode=True, tool_progress_mode='off',
        skip_context_files=True, load_soul_identity=False,
        skip_memory=True, skip_background_review=True, platform='cli',
    )


def _plan():
    return [{'id': 'audit', 'content': 'Audit independent modules', 'status': 'in_progress'}]


def _sequential(agent, arguments):
    call_id = 'planning-call'
    wire_call = {'id': call_id, 'type': 'function',
                 'function': {'name': 'todo', 'arguments': json.dumps(arguments)}}
    messages = [{'role': 'user', 'content': 'Perform the audit'},
                {'role': 'assistant', 'content': None, 'tool_calls': [wire_call]}]
    call = SimpleNamespace(id=call_id, function=SimpleNamespace(
        name='todo', arguments=json.dumps(arguments)))
    agent._execute_tool_calls_sequential(SimpleNamespace(tool_calls=[call]), messages, 'planning-test')
    return json.loads(messages[-1]['content']), messages


def test_real_sequential_dispatch_returns_bounded_reminder_without_prompt_mutation():
    agent = _agent()
    before = build_system_prompt_parts(agent)['stable']
    result, _ = _sequential(agent, {'todos': _plan()})
    assert 'reminder' in result
    repeated, _ = _sequential(agent, {'todos': _plan()})
    assert 'reminder' not in repeated
    assert build_system_prompt_parts(agent)['stable'] == before
    assert agent.model == 'chosen-orchestrator'
    assert agent.provider == 'custom'


def test_real_invoke_path_records_direct_decision_and_read_does_not_nag():
    agent = _agent()
    decision = {'mode': 'direct', 'reason': 'The user explicitly prohibited subagents.'}
    result = json.loads(agent._invoke_tool('todo', {'todos': _plan(), 'delegation': decision}, 'planning-test'))
    assert result['delegation'] == decision
    assert 'reminder' not in result
    read = json.loads(agent._invoke_tool('todo', {}, 'planning-test'))
    assert read['delegation'] == decision
    assert 'reminder' not in read
    assert agent.model == 'chosen-orchestrator'


def test_paired_history_restores_decision_and_unpaired_history_cannot_seed_it():
    original = _agent()
    decision = {'mode': 'delegate', 'reason': 'Independent module audits can run separately.'}
    result, history = _sequential(original, {'todos': _plan(), 'delegation': decision})
    restored = _agent()
    restored._hydrate_todo_store(history)
    assert restored._todo_store.snapshot()['delegation'] == decision
    assert restored._todo_store.read() == result['todos']
    unpaired = _agent()
    unpaired._hydrate_todo_store([history[-1]])
    assert not unpaired._todo_store.has_items()
    assert unpaired._todo_store.snapshot()['delegation'] is None


def test_paired_history_does_not_coerce_string_false_to_reminder_true():
    original = _agent()
    _, history = _sequential(original, {'todos': _plan()})
    payload = json.loads(history[-1]['content'])
    payload['delegation_reminded'] = 'false'
    history[-1]['content'] = json.dumps(payload)
    restored = _agent()
    restored._hydrate_todo_store(history)
    assert restored._todo_store.snapshot()['delegation_reminded'] is False


def test_agent_child_identity_does_not_get_root_reminder_outside_context_marker():
    agent = _agent()
    agent._delegate_depth = 1
    agent.platform = 'subagent'
    result = json.loads(agent._invoke_tool('todo', {'todos': _plan()}, 'planning-test'))
    assert 'reminder' not in result
