"""End to end: router -> work plan on the turn -> backstop hand-off (C1.5).

A fresh scratch ``CLOVER_HOME`` with no config.yaml (so the shipped defaults
decide), a real ``AIAgent.run_conversation``, the real executor funnel, tools,
``delegate_task`` handler and child pool, and a real ``SessionDB``.  Only two
boundaries are faked: the auxiliary model that sizes the message, and the main
model's replies (a scripted local provider).  One full turn each for
direct / parallel / staged.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest

from tests.agent.test_delegation_router import T, aux, verdict_json  # noqa: F401
from tests.agent.test_foreground_time_limit_loop import (  # noqa: F401
    make_agent,
    provider,
    text,
    tool,
)
from tests.agent.test_router_backstop import (  # noqa: F401
    _clean_registry,
    _wait_started,
    dispatches,
)

pytestmark = pytest.mark.real_delegation_router

MARKER = '[Clover runtime] work plan:'
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _unbind_session_context():
    yield
    import gateway.session_context as sc

    for var in sc._VAR_MAP.values():
        var.set(sc._UNSET)
    sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)


@pytest.fixture
def children(monkeypatch):
    """Helpers only: a delegated child's model conversation is faked, the root
    runs the real ``run_conversation``.  Records each helper's system prompt
    and user message."""
    import time

    import run_agent
    from tools.process_registry import process_registry

    started: list = []
    real = run_agent.AIAgent.run_conversation

    def conversation(self, user_message=None, *args, **kwargs):
        if getattr(self, '_delegate_depth', 0) or getattr(self, '_subagent_id', None):
            started.append({'system': getattr(self, 'ephemeral_system_prompt', '') or '',
                            'user': user_message})
            return {'final_response': f'done: {user_message}', 'messages': [], 'api_calls': 1,
                    'completed': True, 'input_tokens': 0, 'output_tokens': 0}
        return real(self, user_message, *args, **kwargs)

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


def _write(path: Path, body: str, cid: str) -> dict:
    return tool('write_file', {'path': str(path), 'content': body}, cid)


def _wire_users(request: dict) -> list:
    return [m['content'] for m in request['messages'] if m.get('role') == 'user']


def _tool_texts(request: dict) -> list:
    return [m['content'] for m in request['messages']
            if m.get('role') == 'tool' and isinstance(m.get('content'), str)]


def _roles_ok(messages: list) -> None:
    roles = [m['role'] for m in messages]
    assert all(not (a == b == 'user') for a, b in zip(roles, roles[1:])), roles
    wanted = {tc['id'] for m in messages if m['role'] == 'assistant'
              for tc in (m.get('tool_calls') or [])}
    got = {m['tool_call_id'] for m in messages if m['role'] == 'tool'}
    assert wanted == got, 'every tool call needs exactly its own result'


def _router_modes_logged(caplog) -> list:
    spec = importlib.util.spec_from_file_location(
        'delegation_modes_eval_e2e', REPO_ROOT / 'scripts' / 'eval' / 'delegation_modes.py')
    mod = importlib.util.module_from_spec(spec)
    sys.modules['delegation_modes_eval_e2e'] = mod
    spec.loader.exec_module(mod)
    return mod, caplog.text.splitlines()


# ── direct ─────────────────────────────────────────────────────────────────

def test_direct_turn_runs_in_the_chat_and_nothing_is_handed_off(provider, aux, tmp_path,
                                                                 children, dispatches, caplog):
    from clover_state import SessionDB

    caplog.set_level(logging.INFO, logger='agent.delegation_router')
    aux.replies[:] = [verdict_json('direct', reason='five quick files')]
    provider.queue[:] = [
        _write(tmp_path / '1.txt', '1', 'c1'), _write(tmp_path / '2.txt', '2', 'c2'),
        _write(tmp_path / '3.txt', '3', 'c3'), _write(tmp_path / '4.txt', '4', 'c4'),
        text('All four files are written.'),
    ]
    db = SessionDB(tmp_path / 'state.db')
    agent = make_agent(provider, session_db=db, session_id='e2e-direct')

    result = agent.run_conversation('Write four small files for me.', conversation_history=[],
                                    task_id='e2e-d')

    assert result['final_response'] == 'All four files are written.'
    assert all((tmp_path / f'{i}.txt').exists() for i in (1, 2, 3, 4)), 'a direct plan is never cut'
    assert children == [] and dispatches == []
    first_user = _wire_users(provider.captured[0])[-1]
    assert first_user.startswith('Write four small files for me.')
    assert f'{MARKER} direct' in first_user
    assert not any('Handed off' in t for r in provider.captured for t in _tool_texts(r))
    _roles_ok(result['messages'])
    stored = [r for r in db.get_messages('e2e-direct') if r['role'] == 'user']
    assert stored[0]['content'] == 'Write four small files for me.'

    mod, lines = _router_modes_logged(caplog)
    record = mod.parse_router_records(lines, 'e2e-direct')
    assert record and record['mode'] == 'direct' and record['ok'] is True
    assert isinstance(record['latency_ms'], int)


# ── parallel ───────────────────────────────────────────────────────────────

def test_parallel_turn_is_handed_off_after_two_calls_and_the_turn_ends_cleanly(
        provider, aux, tmp_path, children, dispatches, caplog):
    from clover_state import SessionDB

    caplog.set_level(logging.INFO, logger='agent.delegation_router')
    aux.replies[:] = [verdict_json('parallel', [
        T('Research the Notion pricing page and list plans'),
        T('Research the Linear pricing page and list plans'),
        T('Research the Asana pricing page and list plans'),
    ])]
    provider.queue[:] = [
        _write(tmp_path / 'a.txt', 'a', 'c1'), _write(tmp_path / 'b.txt', 'b', 'c2'),
        _write(tmp_path / 'c.txt', 'c', 'c3'),     # refused: the runtime hands off here
        text('Helpers are on it; I will report back.'),
    ]
    db = SessionDB(tmp_path / 'state.db')
    agent = make_agent(provider, session_db=db, session_id='e2e-parallel')

    result = agent.run_conversation(
        'Compare the pricing of Notion, Linear and Asana in one table.',
        conversation_history=[], task_id='e2e-p')

    assert result['final_response'] == 'Helpers are on it; I will report back.'
    assert (tmp_path / 'a.txt').exists() and (tmp_path / 'b.txt').exists()
    assert not (tmp_path / 'c.txt').exists()
    # the plan told the model to dispatch; the runtime did, once, via the handler
    assert f'{MARKER} parallel' in _wire_users(provider.captured[0])[-1]
    assert len(dispatches) == 1
    assert len(dispatches[0]['tasks']) == 3
    _wait_started(children, 3)
    assert len(children) == 3
    # the model saw the hand-off as the refused call's own result, then ended
    last_tools = _tool_texts(provider.captured[-1])
    assert any('Handed off to helpers' in t and 'end your turn' in t for t in last_tools)
    assert len(provider.captured) == 4
    _roles_ok(result['messages'])
    # nothing the user can read mentions the plumbing
    assert 'limit' not in result['final_response'].lower()
    assert [r['role'] for r in db.get_messages('e2e-parallel')][0] == 'user'

    mod, lines = _router_modes_logged(caplog)
    record = mod.parse_router_records(lines, 'e2e-parallel')
    assert record and record['mode'] == 'parallel' and record['tasks'] == 3


# ── staged ─────────────────────────────────────────────────────────────────

def test_staged_turn_dispatches_stage_one_and_names_the_next_stage(
        provider, aux, tmp_path, children, dispatches):
    aux.replies[:] = [verdict_json('staged', [
        T('Write the feature flag module with tests in the workdir'),
        T('Review the whole diff of the feature flag module', [0]),
    ])]
    provider.queue[:] = [
        _write(tmp_path / 'a.txt', 'a', 'c1'), _write(tmp_path / 'b.txt', 'b', 'c2'),
        _write(tmp_path / 'c.txt', 'c', 'c3'),
        text('Building it now; review follows.'),
    ]
    agent = make_agent(provider, session_id='e2e-staged')

    result = agent.run_conversation(
        'Add a feature flag with tests, then review the diff before saying done.',
        conversation_history=[], task_id='e2e-s')

    assert result['final_response'] == 'Building it now; review follows.'
    wire_user = _wire_users(provider.captured[0])[-1]
    assert f'{MARKER} staged' in wire_user
    assert wire_user.index('Write the feature flag') < wire_user.index('Review the whole diff')
    # only stage one is dispatched; the next stage is bound for follow-through
    assert [t['goal'] for t in dispatches[0]['tasks']] == [
        'Write the feature flag module with tests in the workdir']
    assert dispatches[0]['follow_through'][0]['goal'] == (
        'Review the whole diff of the feature flag module')
    _wait_started(children, 1)
    assert len(children) == 1
    handed = json.loads(next(t for t in _tool_texts(provider.captured[-1])
                             if 'Handed off to helpers' in t))
    assert handed['next_stage']['goal'] == 'Review the whole diff of the feature flag module'
    _roles_ok(result['messages'])
    assert not (tmp_path / 'c.txt').exists()
