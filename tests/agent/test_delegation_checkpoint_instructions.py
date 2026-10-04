"""Delegation checkpoint: static instructions and cache identity.

The runtime gate teaches itself to new sessions through the stable prompt and
tool schema; it must never alter either of them mid-conversation.
"""
from __future__ import annotations

import json

from tests.agent.test_delegation_checkpoint import (
    _agent,
    _delegate,
    _direct,
    _set_checkpoint_config,
    _write,
    run_batch,
)


# ── static instructions; cache identity ────────────────────────────────────

def _stable_prompt(agent):
    from agent.system_prompt import build_system_prompt_parts

    return build_system_prompt_parts(agent)['stable']


def test_stable_prompt_teaches_the_checkpoint_only_to_agents_it_gates():
    assert 'Before your first work tool' in _stable_prompt(_agent())
    assert 'Before your first work tool' not in _stable_prompt(_agent(platform='cron'))
    assert 'Before your first work tool' not in _stable_prompt(_agent(enabled_toolsets=['todo', 'file']))
    exempt = _agent()
    exempt._delegation_checkpoint_exempt = 'oneshot'
    assert 'Before your first work tool' not in _stable_prompt(exempt)


def test_rollback_config_removes_the_instruction_too():
    _set_checkpoint_config({'enabled': False})
    assert 'Before your first work tool' not in _stable_prompt(_agent())


def test_gating_never_changes_prompt_bytes_or_tool_schemas(tmp_path):
    agent = _agent()
    prompt_before = _stable_prompt(agent)
    tools_before = json.dumps(agent.tools, sort_keys=True)

    run_batch(agent, [_write(tmp_path / 'a.txt')])                 # blocked
    run_batch(agent, [_direct(), _write(tmp_path / 'b.txt')])      # declared, executed
    for i in range(6):
        run_batch(agent, [_write(tmp_path / f'c{i}.txt')])         # budget -> renewal block

    assert _stable_prompt(agent) == prompt_before
    assert json.dumps(agent.tools, sort_keys=True) == tools_before


def test_model_and_provider_selection_are_untouched_by_gating(tmp_path):
    agent = _agent()
    run_batch(agent, [_write(tmp_path / 'a.txt')])
    run_batch(agent, [_delegate()])

    assert agent.model == 'chosen-orchestrator'
    assert agent.provider == 'custom'
