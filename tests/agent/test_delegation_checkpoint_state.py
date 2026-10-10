"""Delegation checkpoint policy: settings, declarations, lifecycle, dispatch credit.

Observes the runtime authorization state through real agents, real ``todo`` and
real ``delegate_task`` dispatch. Enforcement against actual tool side effects is
covered in ``test_delegation_checkpoint.py``.
"""
from __future__ import annotations

import json
import math
from typing import Any

import pytest

from agent import delegation_checkpoint as dc
from run_agent import AIAgent
from tests.agent.test_delegation_checkpoint import sync_spawn


def _agent(**overrides) -> Any:
    from tests.agent.test_delegation_checkpoint import _opt_in_to_checkpoint
    _opt_in_to_checkpoint()
    kwargs = dict(
        provider='custom', api_mode='chat_completions',
        base_url='http://127.0.0.1:1/v1', api_key='local-test-credential',
        model='chosen-orchestrator', model_pinned=True,
        enabled_toolsets=['todo', 'delegation', 'file'], max_iterations=3,
        quiet_mode=True, tool_progress_mode='off',
        skip_context_files=True, load_soul_identity=False,
        skip_memory=True, skip_background_review=True, platform='cli',
    )
    kwargs.update(overrides)
    return AIAgent(**kwargs)


def _state(agent):
    return dc.get_checkpoint(agent).snapshot()


def _declare(agent, mode='direct', reason='Because.'):
    return json.loads(agent._invoke_tool('todo', {'delegation': {'mode': mode, 'reason': reason}}, 't'))


# ── settings ───────────────────────────────────────────────────────────────

def test_defaults_are_finite_positive_and_disabled():
    settings = dc.normalize_settings(None)
    assert settings.enabled is False
    assert settings.max_work_tools > 0
    assert math.isfinite(settings.max_foreground_seconds) and settings.max_foreground_seconds > 0


@pytest.mark.parametrize('bad', [0, -1, True, False, None, 'x', float('nan'), float('inf'), [], {}])
def test_invalid_budget_values_never_produce_a_zero_or_unbounded_budget(bad):
    settings = dc.normalize_settings({'max_work_tools': bad, 'max_foreground_seconds': bad})
    defaults = dc.CheckpointSettings()
    assert settings.max_work_tools == defaults.max_work_tools
    assert settings.max_foreground_seconds == defaults.max_foreground_seconds


def test_fractional_values_are_valid_seconds_but_not_a_tool_count():
    settings = dc.normalize_settings({'max_work_tools': 1.5, 'max_foreground_seconds': 1.5})
    assert settings.max_work_tools == dc.CheckpointSettings().max_work_tools
    assert settings.max_foreground_seconds == 1.5


def test_valid_budget_values_are_honored():
    settings = dc.normalize_settings({'max_work_tools': 3, 'max_foreground_seconds': 7.5})
    assert (settings.max_work_tools, settings.max_foreground_seconds) == (3, 7.5)


@pytest.mark.parametrize('raw,expected', [
    (False, False), ('false', False), ('OFF', False), ('no', False), ('0', False),
    (True, True), ('true', True), ('on', True), (1, True), (0, False),
    (None, False), ('garbage', False),
])
def test_enabled_flag_only_turns_on_explicitly(raw, expected):
    assert dc.normalize_settings({'enabled': raw}).enabled is expected


# ── declarations ───────────────────────────────────────────────────────────

def test_valid_declaration_sets_the_mode_and_every_event_supersedes_older_receipts():
    agent = _agent()
    assert _state(agent)['state'] == dc.UNDECIDED

    _declare(agent, 'direct')
    first = _state(agent)
    assert first['state'] == dc.DIRECT_AUTHORIZED

    revision_before = agent._todo_store.snapshot()['revision']
    _declare(agent, 'direct')  # byte-identical: the todo revision does not move
    assert agent._todo_store.snapshot()['revision'] == revision_before
    assert _state(agent)['generation'] > first['generation'], 'an unchanged declaration must still supersede older receipts'

    _declare(agent, 'delegate')
    assert _state(agent)['state'] == dc.SPAWN_REQUIRED


@pytest.mark.parametrize('bad', [
    {'mode': 'direct', 'reason': ''}, {'mode': 'direct', 'reason': '  '},
    {'mode': 'nope', 'reason': 'x'}, {'mode': 'direct'}, 'direct', 7,
])
def test_invalid_declarations_change_nothing(bad):
    agent = _agent()
    before = _state(agent)

    result = json.loads(agent._invoke_tool('todo', {'delegation': bad}, 't'))

    assert 'error' in result
    assert _state(agent) == before


def test_ordinary_todo_progress_writes_do_not_renew_or_declare():
    agent = _agent()
    before = _state(agent)

    agent._invoke_tool('todo', {'todos': [{'id': 'a', 'content': 'x', 'status': 'in_progress'}]}, 't')
    agent._invoke_tool('todo', {}, 't')

    assert _state(agent) == before


def test_completing_a_plan_does_not_clear_runtime_authorization():
    agent = _agent()
    _declare(agent, 'direct')
    agent._invoke_tool('todo', {'todos': [{'id': 'a', 'content': 'x', 'status': 'completed'}]}, 't')

    assert agent._todo_store.snapshot()['delegation'] is None, 'plan intent is cleared by completion'
    assert _state(agent)['state'] == dc.DIRECT_AUTHORIZED, 'runtime authorization is separate state'


# ── lifecycle: who is gated, when a decision resets ────────────────────────

@pytest.mark.parametrize('overrides,eligible', [
    ({}, True),
    ({'enabled_toolsets': ['todo', 'file']}, False),
    ({'enabled_toolsets': ['delegation', 'file']}, False),
    ({'platform': 'cron'}, False),
])
def test_eligibility_matrix(overrides, eligible):
    assert dc.is_eligible(_agent(**overrides)) is eligible


def test_explicit_markers_and_kanban_worker_are_not_eligible(monkeypatch):
    marked = _agent()
    marked._delegation_checkpoint_exempt = 'oneshot'
    assert dc.is_eligible(marked) is False

    fork = _agent()
    fork._is_background_review_fork = True
    assert dc.is_eligible(fork) is False

    monkeypatch.setenv('CLOVER_KANBAN_TASK', 't_1')
    assert dc.is_eligible(_agent()) is False


def test_real_review_fork_factory_marks_the_fork_and_the_live_root_is_unmarked():
    from agent.background_review import build_cache_parity_fork

    parent = _agent()
    fork, _runtime, _routed = build_cache_parity_fork(parent, None, max_iterations=2)

    assert fork._is_background_review_fork is True
    assert parent._is_background_review_fork is False
    assert dc.is_eligible(fork) is False and dc.is_eligible(parent) is True


def test_one_shot_and_batch_entries_mark_their_agents_noninteractive():
    # The creating code, not message text, owns the marker: an AIAgent never
    # starts out exempt.
    assert _agent()._delegation_checkpoint_exempt is None


def test_begin_turn_resets_unless_a_trusted_internal_notification():
    agent = _agent()
    _declare(agent, 'direct')

    dc.begin_turn(agent, 'internal_notification')
    assert _state(agent)['state'] == dc.DIRECT_AUTHORIZED

    for kind in ('async_delegation_complete', 'something_else', '', None):
        _declare(agent, 'direct')
        dc.begin_turn(agent, kind)
        assert _state(agent)['state'] == dc.UNDECIDED, kind


def test_begin_turn_on_an_unseen_agent_starts_undecided_even_for_internal_delivery():
    agent = _agent()
    dc.begin_turn(agent, 'internal_notification')
    assert _state(agent)['state'] == dc.UNDECIDED


def test_begin_turn_rereads_settings_for_the_new_turn():
    agent = _agent()
    assert _state(agent) and dc.get_checkpoint(agent).settings.enabled is True
    import yaml
    from clover_constants import get_clover_home
    (get_clover_home() / 'config.yaml').write_text(yaml.safe_dump(
        {'delegation': {'checkpoint': {'enabled': False, 'max_work_tools': 2}}}))

    dc.begin_turn(agent, None)

    settings = dc.get_checkpoint(agent).settings
    assert settings.enabled is False and settings.max_work_tools == 2


# ── dispatch credit: only a real child start moves spawn_required forward ──

@pytest.fixture
def child_runs(monkeypatch):
    import time

    import run_agent
    from tools.process_registry import process_registry

    started = []

    def conversation(self, user_message=None, **kwargs):
        started.append(user_message)
        return {'final_response': 'ok', 'messages': [], 'api_calls': 1, 'completed': True,
                'input_tokens': 0, 'output_tokens': 0}

    monkeypatch.setattr(run_agent.AIAgent, 'run_conversation', conversation)
    yield started
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            process_registry.completion_queue.get_nowait()
        except Exception:
            time.sleep(0.05)
            if process_registry.completion_queue.empty():
                break


@pytest.mark.parametrize('args', [
    {'action': 'list'}, {'action': 'stop', 'subagent_id': 'x'},
    {'action': 'steer', 'subagent_id': 'x', 'message': 'm'}, {}, {'goal': ' '},
])
def test_non_spawns_and_failed_dispatch_leave_spawn_required(child_runs, args):
    agent = _agent()
    _declare(agent, 'delegate')

    agent._invoke_tool('delegate_task', args, 't')

    assert _state(agent)['state'] == dc.SPAWN_REQUIRED
    assert child_runs == []


def test_accepted_async_child_takes_over_the_request(child_runs):
    agent = _agent()
    _declare(agent, 'delegate')

    out = json.loads(agent._invoke_tool('delegate_task', {'goal': 'lane'}, 't'))

    assert out['status'] == 'dispatched'
    assert _state(agent)['phase'] == dc.PHASE_HANDED_OFF


def test_explicit_synchronous_child_start_credits_the_declaration(child_runs):
    agent = _agent()
    _declare(agent, 'delegate')

    out = sync_spawn(agent, goal='inline lane')

    assert 'results' in out and out.get('status') != 'dispatched'
    assert child_runs == ['inline lane'], 'the child must have run before delegate_task returned'
    assert _state(agent)['state'] == dc.DELEGATED_STARTED


def test_unsupported_async_delivery_rejects_and_credits_nothing(child_runs):
    """A root that asked for a handoff the session cannot deliver is told so;
    the work is not run inline and the declaration stays unsatisfied."""
    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(platform='batch', chat_id='', session_key='s', async_delivery=False)
    try:
        agent = _agent()
        _declare(agent, 'delegate')
        out = json.loads(agent._invoke_tool('delegate_task', {'goal': 'inline lane'}, 't'))
    finally:
        clear_session_vars(tokens)

    assert out['status'] == 'rejected' and out['started'] is False
    assert out['reason'] == 'delivery_unsupported'
    assert child_runs == []
    assert _state(agent)['state'] == dc.SPAWN_REQUIRED


def test_stale_ticket_cannot_credit_a_newer_declaration():
    agent = _agent()
    _declare(agent, 'delegate')
    stale = dc.ticket_for(agent)
    _declare(agent, 'delegate')  # redeclared: the earlier ticket is from another generation

    assert stale.accept_handoff(delegation_id='d-stale', goals=['lane']) is False
    assert _state(agent)['phase'] == dc.PHASE_FOREGROUND
    assert dc.ticket_for(agent).accept_handoff(delegation_id='d-live', goals=['lane']) is True
    assert _state(agent)['phase'] == dc.PHASE_HANDED_OFF


def test_a_spawn_without_any_declaration_credits_nothing(child_runs):
    agent = _agent()

    agent._invoke_tool('delegate_task', {'goal': 'eager'}, 't')

    assert _state(agent)['state'] == dc.UNDECIDED
