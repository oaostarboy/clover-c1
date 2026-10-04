"""Mandatory root delegation checkpoint: real AIAgent tool dispatch.

No model responses are mocked. Calls go through ``AIAgent._execute_tool_calls``
(the same router the conversation loop uses) with real tools against a temp
directory, and every assertion is on an observable executor side effect (a file
that is or is not created), never on a hand-built state object.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from run_agent import AIAgent


def _agent(**overrides) -> Any:
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


def _call(name: str, arguments: dict, call_id: str):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(
        name=name, arguments=json.dumps(arguments)))


def run_batch(agent, calls, task_id='checkpoint-test'):
    """Route ``calls`` [(name, args), ...] through the real batch router.

    Returns the tool-result payloads (decoded JSON when possible) in call order.
    """
    tool_calls = [_call(name, args, f'call-{i}') for i, (name, args) in enumerate(calls)]
    wire = [{'id': tc.id, 'type': 'function',
             'function': {'name': tc.function.name, 'arguments': tc.function.arguments}}
            for tc in tool_calls]
    messages = [{'role': 'user', 'content': 'do the job'},
                {'role': 'assistant', 'content': None, 'tool_calls': wire}]
    agent._execute_tool_calls(SimpleNamespace(tool_calls=tool_calls), messages, task_id)
    results = messages[2:]
    assert [m['tool_call_id'] for m in results] == [tc.id for tc in tool_calls], (
        'tool results must stay paired and ordered with the emitted calls')
    decoded = []
    for message in results:
        try:
            decoded.append(json.loads(message['content']))
        except (TypeError, ValueError):
            decoded.append(message['content'])
    return decoded


def _write(path, text='x'):
    return ('write_file', {'path': str(path), 'content': text})


def _direct(reason='Tiny single-file job; no parallel lanes.'):
    return ('todo', {'delegation': {'mode': 'direct', 'reason': reason}})


def test_work_tool_without_declaration_is_blocked_before_its_side_effect(tmp_path):
    agent = _agent()
    target = tmp_path / 'out.txt'

    (result,) = run_batch(agent, [_write(target)])

    assert not target.exists(), 'work ran without a delegation decision'
    assert 'delegation_decision_required' in json.dumps(result)


def test_direct_declaration_then_same_work_executes(tmp_path):
    agent = _agent()
    target = tmp_path / 'out.txt'

    decl, written = run_batch(agent, [_direct(), _write(target, 'hello')])

    assert decl['delegation']['mode'] == 'direct'
    assert target.read_text() == 'hello'
    assert 'delegation_decision_required' not in json.dumps(written)


# ── declaration rules ──────────────────────────────────────────────────────

def test_decision_only_todo_authorizes_a_small_job_without_a_fake_checklist(tmp_path):
    agent = _agent()
    target = tmp_path / 'small.txt'

    decl, written = run_batch(agent, [_direct('One-line edit, nothing to split.'), _write(target)])

    assert decl['todos'] == [], 'a decision must not force a checklist'
    assert target.exists()


@pytest.mark.parametrize('delegation', [
    {'mode': 'direct', 'reason': ''},
    {'mode': 'direct', 'reason': '   \n'},
    {'mode': 'direct'},
    {'mode': 'maybe', 'reason': 'unsure'},
    {'mode': 'direct', 'reason': 'ok', 'extra': 1},
    'direct',
])
def test_malformed_or_blank_declaration_cannot_authorize(tmp_path, delegation):
    agent = _agent()
    target = tmp_path / 'out.txt'

    bad, blocked = run_batch(agent, [('todo', {'delegation': delegation}), _write(target)])

    assert 'error' in bad
    assert not target.exists()
    assert 'delegation_decision_required' in json.dumps(blocked)


def test_work_before_a_declaration_in_the_same_batch_is_blocked_not_replayed(tmp_path):
    agent = _agent()
    target = tmp_path / 'out.txt'

    blocked, decl = run_batch(agent, [_write(target), _direct()])

    assert 'delegation_decision_required' in json.dumps(blocked)
    assert decl['delegation']['mode'] == 'direct'
    assert not target.exists(), 'blocked work must not be replayed by a later declaration'


def test_a_policy_block_is_not_counted_as_a_repeated_tool_failure(tmp_path):
    agent = _agent()
    results = [run_batch(agent, [_write(tmp_path / 'same.txt')])[0] for _ in range(10)]

    # Repeated identical *failures* get a guardrail warning appended (and later a
    # halt). A policy block is not a tool failure, so every result stays a clean
    # structured block and the halt state is untouched.
    assert all(isinstance(r, dict) and r['error_type'] == 'delegation_decision_required' for r in results)
    assert getattr(agent, '_tool_guardrail_halt_decision', None) is None
    assert not (tmp_path / 'same.txt').exists()


# ── delegate must be followed by a real child start ────────────────────────

@pytest.fixture
def child_runs(monkeypatch):
    """Real child construction and dispatch; only the child's model loop is faked."""
    import tools.delegate_tool as dt
    from tools.process_registry import process_registry

    started = []

    def fake_child(task_index, goal, child=None, parent_agent=None, **kw):
        started.append(goal)
        return {'task_index': task_index, 'status': 'completed', 'summary': f'done: {goal}',
                'api_calls': 1, 'duration_seconds': 0.01, 'model': 'm', 'exit_reason': 'completed'}

    monkeypatch.setattr(dt, '_run_single_child', fake_child)
    yield started
    import time
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            process_registry.completion_queue.get_nowait()
        except Exception:
            time.sleep(0.05)
            if process_registry.completion_queue.empty():
                break


def _delegate(reason='Independent lanes.'):
    return ('todo', {'delegation': {'mode': 'delegate', 'reason': reason}})


def _spawn(goal='audit lane A'):
    return ('delegate_task', {'goal': goal})


def test_delegate_intent_alone_does_not_unlock_work(tmp_path):
    agent = _agent()
    target = tmp_path / 'out.txt'

    _, blocked = run_batch(agent, [_delegate(), _write(target)])

    assert 'delegation_dispatch_required' in json.dumps(blocked)
    assert not target.exists()


@pytest.mark.parametrize('control', [
    {'action': 'list'},
    {'action': 'steer', 'subagent_id': 'nope', 'message': 'hi'},
    {'action': 'stop', 'subagent_id': 'nope'},
    {},
    {'goal': '   '},
])
def test_control_actions_and_failed_dispatch_do_not_unlock(tmp_path, child_runs, control):
    agent = _agent()
    target = tmp_path / 'out.txt'

    run_batch(agent, [_delegate()])
    run_batch(agent, [('delegate_task', control)])
    (blocked,) = run_batch(agent, [_write(target)])

    assert 'delegation_dispatch_required' in json.dumps(blocked)
    assert not target.exists()
    assert child_runs == []


def test_real_async_dispatch_unlocks_work_in_the_same_batch(tmp_path, child_runs):
    agent = _agent()
    target = tmp_path / 'out.txt'

    _, dispatched, written = run_batch(agent, [_delegate(), _spawn(), _write(target, 'after')])

    assert dispatched['status'] == 'dispatched'
    assert target.read_text() == 'after'


def test_inline_child_start_when_async_delivery_is_unsupported_also_unlocks(tmp_path, child_runs):
    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(platform='api_server', chat_id='s', session_key='s', async_delivery=False)
    try:
        agent = _agent()
        target = tmp_path / 'out.txt'

        _, ran, _ = run_batch(agent, [_delegate(), _spawn('inline lane'), _write(target, 'after')])

        assert child_runs == ['inline lane']
        assert target.read_text() == 'after'
    finally:
        clear_session_vars(tokens)


def test_a_child_started_before_the_declaration_cannot_satisfy_it(tmp_path, child_runs):
    agent = _agent()
    target = tmp_path / 'out.txt'

    run_batch(agent, [_spawn('early lane')])
    run_batch(agent, [_delegate()])
    (blocked,) = run_batch(agent, [_write(target)])

    assert 'delegation_dispatch_required' in json.dumps(blocked)
    assert not target.exists()


def test_failed_dispatch_can_recover_by_declaring_direct_with_the_blocker(tmp_path, child_runs):
    agent = _agent()
    target = tmp_path / 'out.txt'

    run_batch(agent, [_delegate(), ('delegate_task', {})])
    _, written = run_batch(agent, [_direct('Spawn failed: no goal accepted.'), _write(target)])

    assert target.exists()


# ── turn entry: real run_conversation against a fake provider endpoint ─────

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


def _tool_calls_response(calls):
    return {
        'id': 'm',
        'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
            'role': 'assistant', 'content': '',
            'tool_calls': [{'id': f'call_{i}', 'type': 'function',
                            'function': {'name': name, 'arguments': json.dumps(args)}}
                           for i, (name, args) in enumerate(calls)]}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 0, 'total_tokens': 10},
    }


def _text_response(text='done'):
    return {
        'id': 'm',
        'choices': [{'index': 0, 'finish_reason': 'stop',
                     'message': {'role': 'assistant', 'content': text}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 0, 'total_tokens': 10},
    }


class _Provider(BaseHTTPRequestHandler):
    scripted: list = []
    requests: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode())
        if not self.path.endswith('/chat/completions'):
            # Metadata probes (e.g. /api/show) must not consume scripted replies.
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        type(self).requests.append(body)
        resp = type(self).scripted.pop(0) if type(self).scripted else _text_response()
        message = resp['choices'][0]['message']
        if body.get('stream') is True:
            delta_calls = [{'index': i, 'id': tc['id'], 'type': 'function', 'function': tc['function']}
                           for i, tc in enumerate(message.get('tool_calls') or [])]
            chunks = [{'id': 'm', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]}]
            if message.get('content'):
                chunks.append({'id': 'm', 'choices': [{'index': 0, 'delta': {'content': message['content']}, 'finish_reason': None}]})
            if delta_calls:
                chunks.append({'id': 'm', 'choices': [{'index': 0, 'delta': {'tool_calls': delta_calls}, 'finish_reason': None}]})
            chunks.append({'id': 'm', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if delta_calls else 'stop'}]})
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(f'data: {json.dumps(chunk)}\n\n'.encode())
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        else:
            payload = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def provider():
    _Provider.scripted = []
    _Provider.requests = []
    server = HTTPServer(('127.0.0.1', 0), _Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f'http://127.0.0.1:{server.server_address[1]}/v1',
            script=lambda *responses: _Provider.scripted.extend(responses),
            requests=_Provider.requests,
        )
    finally:
        server.shutdown()
        server.server_close()


def _live_agent(provider_url, **overrides):
    return _agent(base_url=provider_url, max_iterations=10, **overrides)


def _tool_messages(result):
    return [m for m in result['messages'] if m.get('role') == 'tool']


def test_root_turn_blocks_undeclared_work_and_keeps_roles_paired(tmp_path, provider):
    target = tmp_path / 'out.txt'
    provider.script(_tool_calls_response([_write(target)]), _text_response('ok, declaring first'))
    agent = _live_agent(provider.url)

    result = agent.run_conversation('please write the file')

    assert not target.exists()
    (tool_message,) = _tool_messages(result)
    assert 'delegation_decision_required' in tool_message['content']
    roles = [m['role'] for m in result['messages']]
    assert roles == ['user', 'assistant', 'tool', 'assistant']
    assert result['final_response'] == 'ok, declaring first'


def test_decision_made_in_one_iteration_covers_later_iterations_of_the_same_turn(tmp_path, provider):
    target = tmp_path / 'out.txt'
    provider.script(_tool_calls_response([_direct()]), _tool_calls_response([_write(target, 'later')]),
                    _text_response('finished'))
    agent = _live_agent(provider.url)

    agent.run_conversation('write the file')

    assert target.read_text() == 'later'


def test_a_new_human_turn_invalidates_the_previous_decision(tmp_path, provider):
    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'
    provider.script(_tool_calls_response([_direct(), _write(first)]), _text_response('one done'),
                    _tool_calls_response([_write(second)]), _text_response('two blocked'))
    agent = _live_agent(provider.url)

    turn_one = agent.run_conversation('small job')
    agent.run_conversation('now a much bigger job', conversation_history=turn_one['messages'])

    assert first.exists()
    assert not second.exists(), 'a decision for the previous request authorized a new one'


@pytest.mark.parametrize('kind,allowed', [
    ('internal_notification', True),
    ('async_delegation_complete', False),
    (None, False),
])
def test_only_trusted_internal_notification_keeps_a_live_decision(tmp_path, provider, kind, allowed):
    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'
    provider.script(_tool_calls_response([_direct(), _write(first)]), _text_response('one done'),
                    _tool_calls_response([_write(second)]), _text_response('two'))
    agent = _live_agent(provider.url)

    turn_one = agent.run_conversation('small job')
    delivered = agent.run_conversation(
        '[background child finished]', conversation_history=turn_one['messages'],
        persist_user_display_kind=kind)

    assert second.exists() is allowed
    blocked = [m for m in _tool_messages(delivered) if 'delegation_decision_required' in m['content']]
    assert bool(blocked) is (not allowed), 'results must still be delivered either way'


def test_restored_history_cannot_authorize_work_for_a_new_request(tmp_path, provider):
    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'
    provider.script(_tool_calls_response([
        ('todo', {'todos': [{'id': 'a', 'content': 'Edit', 'status': 'in_progress'}],
                  'delegation': {'mode': 'direct', 'reason': 'Tiny edit.'}}),
        _write(first)]), _text_response('one done'))
    original = _live_agent(provider.url)
    turn_one = original.run_conversation('small job')
    assert first.exists()

    provider.script(_tool_calls_response([_write(second)]), _text_response('blocked'))
    restarted = _live_agent(provider.url)
    restarted.run_conversation('a different request', conversation_history=turn_one['messages'])

    assert restarted._todo_store.snapshot()['delegation'] is not None, 'history was hydrated'
    assert not second.exists(), 'restored todo state authorized new work'


def test_steering_does_not_reset_the_decision(tmp_path):
    agent = _agent()
    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'

    run_batch(agent, [_direct(), _write(first)])
    agent.steer('please also keep it short')
    run_batch(agent, [_write(second)])

    assert second.exists()


# ── renewal budget ─────────────────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _set_checkpoint_config(values):
    import yaml
    from clover_constants import get_clover_home

    (get_clover_home() / 'config.yaml').write_text(
        yaml.safe_dump({'delegation': {'checkpoint': values}}))


def _clocked(agent):
    from agent.delegation_checkpoint import get_checkpoint

    clock = _Clock()
    get_checkpoint(agent)._clock = clock
    return clock


def test_sixth_work_call_needs_renewal_and_an_unchanged_declaration_renews(tmp_path):
    agent = _agent()
    run_batch(agent, [_direct()])
    for i in range(5):
        run_batch(agent, [_write(tmp_path / f'w{i}.txt')])
    (blocked,) = run_batch(agent, [_write(tmp_path / 'w5.txt')])

    assert 'delegation_decision_required' in json.dumps(blocked)
    assert 'renew' in json.dumps(blocked)
    assert not (tmp_path / 'w5.txt').exists()
    assert all((tmp_path / f'w{i}.txt').exists() for i in range(5))

    run_batch(agent, [_direct()])  # same text, same todo revision: still a renewal event
    run_batch(agent, [_write(tmp_path / 'w5.txt')])
    assert (tmp_path / 'w5.txt').exists()


def test_unrelated_todo_progress_writes_do_not_renew(tmp_path):
    agent = _agent()
    run_batch(agent, [_direct()])
    for i in range(5):
        run_batch(agent, [_write(tmp_path / f'w{i}.txt')])
    run_batch(agent, [('todo', {'todos': [{'id': 'a', 'content': 'x', 'status': 'in_progress'}]})])
    (blocked,) = run_batch(agent, [_write(tmp_path / 'w5.txt')])

    assert 'delegation_decision_required' in json.dumps(blocked)


def test_elapsed_time_renews_at_exactly_the_boundary(tmp_path):
    agent = _agent()
    clock = _clocked(agent)
    run_batch(agent, [_direct()])
    run_batch(agent, [_write(tmp_path / 'first.txt')])

    clock.now += 119.9
    run_batch(agent, [_write(tmp_path / 'just-inside.txt')])
    clock.now += 0.1  # 120.0s since the first work call
    (blocked,) = run_batch(agent, [_write(tmp_path / 'at-boundary.txt')])

    assert (tmp_path / 'just-inside.txt').exists()
    assert not (tmp_path / 'at-boundary.txt').exists()
    assert 'delegation_decision_required' in json.dumps(blocked)


def test_time_does_not_run_before_the_first_work_call(tmp_path):
    agent = _agent()
    clock = _clocked(agent)
    run_batch(agent, [_direct()])

    clock.now += 10_000  # deliberating is not foreground work
    run_batch(agent, [_write(tmp_path / 'late-start.txt')])

    assert (tmp_path / 'late-start.txt').exists()


def test_control_plane_calls_never_spend_the_budget(tmp_path):
    agent = _agent()
    run_batch(agent, [_direct()])
    for _ in range(12):
        run_batch(agent, [('todo', {}), ('skills_list', {})])
    for i in range(5):
        run_batch(agent, [_write(tmp_path / f'w{i}.txt')])

    assert all((tmp_path / f'w{i}.txt').exists() for i in range(5))


def test_executed_failures_count_against_the_budget(tmp_path):
    agent = _agent()
    run_batch(agent, [_direct()])
    for i in range(5):
        run_batch(agent, [('read_file', {'path': str(tmp_path / f'missing-{i}.txt')})])
    (blocked,) = run_batch(agent, [_write(tmp_path / 'after.txt')])

    assert 'delegation_decision_required' in json.dumps(blocked)


def test_child_work_does_not_spend_the_parent_budget(tmp_path, child_runs):
    agent = _agent()
    run_batch(agent, [_delegate(), _spawn('lane one')])
    for i in range(5):
        run_batch(agent, [_write(tmp_path / f'w{i}.txt')])

    assert all((tmp_path / f'w{i}.txt').exists() for i in range(5))


@pytest.mark.parametrize('bad', [0, -3, True, False, 'five', None, float('inf'), float('nan'), 2.5, [], {}])
def test_invalid_budget_settings_fall_back_to_finite_positive_defaults(tmp_path, bad):
    _set_checkpoint_config({'max_work_tools': bad, 'max_foreground_seconds': bad})
    agent = _agent()
    run_batch(agent, [_direct()])
    for i in range(5):
        run_batch(agent, [_write(tmp_path / f'w{i}.txt')])
    (blocked,) = run_batch(agent, [_write(tmp_path / 'w5.txt')])

    assert all((tmp_path / f'w{i}.txt').exists() for i in range(5)), 'a bad value produced a zero budget'
    assert 'delegation_decision_required' in json.dumps(blocked)


def test_configured_budget_is_honored(tmp_path):
    _set_checkpoint_config({'max_work_tools': 2})
    agent = _agent()
    run_batch(agent, [_direct()])
    run_batch(agent, [_write(tmp_path / 'a.txt')])
    run_batch(agent, [_write(tmp_path / 'b.txt')])
    (blocked,) = run_batch(agent, [_write(tmp_path / 'c.txt')])

    assert 'delegation_decision_required' in json.dumps(blocked)
    assert not (tmp_path / 'c.txt').exists()


@pytest.mark.parametrize('off', [False, 'false', 'off', 'no', '0'])
def test_explicit_rollback_restores_ungated_behavior(tmp_path, off):
    _set_checkpoint_config({'enabled': off})
    agent = _agent()

    run_batch(agent, [_write(tmp_path / 'ungated.txt')])

    assert (tmp_path / 'ungated.txt').exists()


def test_default_config_is_enabled_for_eligible_roots(tmp_path):
    agent = _agent()
    (blocked,) = run_batch(agent, [_write(tmp_path / 'gated.txt')])
    assert 'delegation_decision_required' in json.dumps(blocked)
    assert not (tmp_path / 'gated.txt').exists()


# ── eligibility matrix ─────────────────────────────────────────────────────

@pytest.mark.parametrize('overrides', [
    dict(enabled_toolsets=['delegation', 'file']),            # no todo
    dict(enabled_toolsets=['todo', 'file']),                  # no delegate_task
    dict(platform='cron'),                                    # scheduler
])
def test_agents_without_both_tools_or_on_cron_are_never_gated(tmp_path, overrides):
    agent = _agent(**overrides)

    run_batch(agent, [_write(tmp_path / 'free.txt')])

    assert (tmp_path / 'free.txt').exists()


def test_caller_marked_noninteractive_root_is_never_gated(tmp_path):
    agent = _agent()
    agent._delegation_checkpoint_exempt = 'batch_runner'

    run_batch(agent, [_write(tmp_path / 'free.txt')])

    assert (tmp_path / 'free.txt').exists()


def test_dispatcher_spawned_kanban_worker_is_never_gated(tmp_path, monkeypatch):
    monkeypatch.setenv('CLOVER_KANBAN_TASK', 't_123')
    agent = _agent()

    run_batch(agent, [_write(tmp_path / 'free.txt')])

    assert (tmp_path / 'free.txt').exists()


def test_todo_stays_optional_for_ineligible_agents(tmp_path):
    agent = _agent(enabled_toolsets=['todo', 'file'])

    result, _ = run_batch(agent, [
        ('todo', {'todos': [{'id': 'a', 'content': 'Step', 'status': 'in_progress'}]}),
        _write(tmp_path / 'free.txt')])

    assert result['summary']['in_progress'] == 1
    assert (tmp_path / 'free.txt').exists()


def test_orchestrator_child_is_never_gated(tmp_path, monkeypatch):
    """A real orchestrator child has both tools but is a delegated context."""
    import tools.delegate_tool as dt

    _set_deleg_config = __import__('yaml').safe_dump({'delegation': {'max_spawn_depth': 2}})
    from clover_constants import get_clover_home
    (get_clover_home() / 'config.yaml').write_text(_set_deleg_config)
    children = []

    def capture(task_index, goal, child=None, parent_agent=None, **kw):
        children.append(child)
        return {'task_index': task_index, 'status': 'completed', 'summary': 's',
                'api_calls': 1, 'duration_seconds': 0.0, 'model': 'm', 'exit_reason': 'completed'}

    monkeypatch.setattr(dt, '_run_single_child', capture)
    from gateway.session_context import clear_session_vars, set_session_vars
    tokens = set_session_vars(platform='api_server', chat_id='s', session_key='s', async_delivery=False)
    try:
        parent = _agent()
        run_batch(parent, [_direct(), ('delegate_task', {'goal': 'orchestrate', 'role': 'orchestrator'})])
    finally:
        clear_session_vars(tokens)

    (child,) = children
    assert {'todo', 'delegate_task'} <= set(child.valid_tool_names), 'precondition: orchestrator has both tools'
    run_batch(child, [_write(tmp_path / 'child-free.txt')])
    assert (tmp_path / 'child-free.txt').exists()


def test_real_background_review_fork_keeps_memory_and_skill_learning(tmp_path):
    from agent.background_review import build_cache_parity_fork

    parent = _agent(enabled_toolsets=['todo', 'delegation', 'file', 'memory', 'skills'], skip_memory=False)
    fork, _runtime, _routed = build_cache_parity_fork(parent, None, max_iterations=2)
    assert {'todo', 'delegate_task'} <= set(fork.valid_tool_names), 'precondition: the fork looks like a root'

    (memory,) = run_batch(fork, [('memory', {'action': 'add', 'target': 'memory', 'content': 'prefers tabs'})])
    (skill,) = run_batch(fork, [('skill_manage', {
        'action': 'create', 'name': 'review-made-skill',
        'content': '---\nname: review-made-skill\ndescription: Made by review.\n---\n# Skill\nbody\n'})])

    assert memory['success'] is True
    assert skill['success'] is True

    # The same tools on the live parent are work and need a declaration.
    (gated,) = run_batch(parent, [('memory', {'action': 'add', 'target': 'memory', 'content': 'other'})])
    assert 'delegation_decision_required' in json.dumps(gated)


# ── what counts as work ────────────────────────────────────────────────────

@pytest.fixture
def probe_tool():
    """A new (unknown to the policy) tool with an observable side effect."""
    from tools.registry import registry

    calls = []
    registry.register(
        name='checkpoint_probe', toolset='checkpoint-probe',
        schema={'name': 'checkpoint_probe', 'description': 'probe',
                'parameters': {'type': 'object', 'properties': {}, 'required': []}},
        handler=lambda args, **kw: (calls.append(dict(args)) or json.dumps({'ok': True})),
        check_fn=lambda: True,
    )
    try:
        yield calls
    finally:
        registry.deregister('checkpoint_probe')


def test_deferred_unknown_tool_is_work_and_the_bridge_cannot_launder_it(probe_tool):
    from agent.delegation_checkpoint import resolve_work_name

    agent = _agent(enabled_toolsets=['todo', 'delegation', 'checkpoint-probe'])
    # The new tool is deferred behind Tool Search: the model only ever sees the bridge.
    assert 'checkpoint_probe' not in agent.valid_tool_names
    assert {'tool_search', 'tool_describe', 'tool_call'} <= set(agent.valid_tool_names)
    bridged = ('tool_call', {'name': 'checkpoint_probe', 'arguments': {'marker': 1}})
    assert resolve_work_name(*bridged) == 'checkpoint_probe'

    (blocked,) = run_batch(agent, [bridged])
    assert probe_tool == []
    assert 'delegation_decision_required' in json.dumps(blocked)

    run_batch(agent, [_direct(), bridged])
    assert probe_tool == [{'marker': 1}]


def test_tool_discovery_lookups_are_control_plane_and_free(probe_tool):
    agent = _agent(enabled_toolsets=['todo', 'delegation', 'checkpoint-probe'])
    run_batch(agent, [_direct()])
    for _ in range(8):
        run_batch(agent, [('tool_search', {'query': 'probe'}), ('tool_describe', {'name': 'checkpoint_probe'})])
    for i in range(5):
        run_batch(agent, [('tool_call', {'name': 'checkpoint_probe', 'arguments': {'i': i}})])

    assert [c['i'] for c in probe_tool] == [0, 1, 2, 3, 4]


def test_execute_code_counts_as_one_work_tool_and_is_gated(tmp_path):
    agent = _agent(enabled_toolsets=['todo', 'delegation', 'code_execution'])
    target = tmp_path / 'from-script.txt'
    script = f"open({str(target)!r}, 'w').write('x')\nprint('wrote')"

    (blocked,) = run_batch(agent, [('execute_code', {'code': script})])
    assert not target.exists()
    assert 'delegation_decision_required' in json.dumps(blocked)

    run_batch(agent, [_direct(), ('execute_code', {'code': script})])
    assert target.exists()


# ── common funnel: ordering relative to rewrites, plugins, guardrails ──────

@pytest.fixture
def plugin_manager():
    from clover_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    saved_hooks = {k: list(v) for k, v in manager._hooks.items()}
    saved_mw = {k: list(v) for k, v in manager._middleware.items()}
    try:
        yield manager
    finally:
        manager._hooks.clear()
        manager._hooks.update(saved_hooks)
        manager._middleware.clear()
        manager._middleware.update(saved_mw)


def test_gate_applies_to_the_rewritten_call_not_the_original(tmp_path, plugin_manager):
    original, rewritten = tmp_path / 'original.txt', tmp_path / 'rewritten.txt'
    plugin_manager._middleware.setdefault('tool_request', []).append(
        lambda **kw: {'args': {**kw['args'], 'path': str(rewritten)}} if kw.get('tool_name') == 'write_file' else None)
    agent = _agent()

    run_batch(agent, [_write(original)])
    assert not original.exists() and not rewritten.exists()

    run_batch(agent, [_direct(), _write(original)])
    assert rewritten.exists() and not original.exists()


def test_plugin_block_wins_and_spends_no_budget(tmp_path, plugin_manager):
    blocked_target, ok_target = tmp_path / 'blocked.txt', tmp_path / 'ok.txt'

    def block(**kw):
        if (kw.get('args') or {}).get('path') == str(blocked_target):
            return {'action': 'block', 'message': 'policy says no'}
        return None

    plugin_manager._hooks.setdefault('pre_tool_call', []).append(block)
    agent = _agent()
    run_batch(agent, [_direct()])

    for _ in range(8):
        (result,) = run_batch(agent, [_write(blocked_target)])
        assert 'policy says no' in json.dumps(result)
    for i in range(5):
        run_batch(agent, [_write(tmp_path / f'w{i}.txt')])

    assert not blocked_target.exists()
    assert all((tmp_path / f'w{i}.txt').exists() for i in range(5)), 'plugin blocks consumed the work budget'


def test_guardrail_denial_wins_and_spends_no_budget(tmp_path, monkeypatch):
    from agent.tool_guardrails import ToolGuardrailDecision

    agent = _agent()
    run_batch(agent, [_direct()])
    denied = ToolGuardrailDecision(action='block', code='test_block', message='guardrail says no',
                                   tool_name='write_file', count=9)
    monkeypatch.setattr(agent._tool_guardrails, 'before_call', lambda name, args: denied)
    for _ in range(8):
        (result,) = run_batch(agent, [_write(tmp_path / 'denied.txt')])
        assert 'guardrail says no' in json.dumps(result) or 'test_block' in json.dumps(result)
    monkeypatch.undo()
    agent._tool_guardrail_halt_decision = None

    for i in range(5):
        run_batch(agent, [_write(tmp_path / f'w{i}.txt')])

    assert all((tmp_path / f'w{i}.txt').exists() for i in range(5)), 'guardrail denials consumed the work budget'


# ── legacy / registered invocation ─────────────────────────────────────────

def test_legacy_invoke_with_skip_flag_alone_is_not_authorization(tmp_path):
    agent = _agent()
    target = tmp_path / 'out.txt'

    blocked = agent._invoke_tool(
        'write_file', {'path': str(target), 'content': 'x'}, 'legacy', 'legacy-call',
        pre_tool_block_checked=True, skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True)

    assert not target.exists()
    assert 'delegation_decision_required' in blocked


def test_legacy_invoke_after_a_declaration_runs_once_and_charges_once(tmp_path):
    agent = _agent()
    run_batch(agent, [_direct()])

    for i in range(5):
        agent._invoke_tool('write_file', {'path': str(tmp_path / f'l{i}.txt'), 'content': 'x'},
                           'legacy', f'legacy-{i}', skip_tool_execution_middleware=True)
    sixth = agent._invoke_tool('write_file', {'path': str(tmp_path / 'l5.txt'), 'content': 'x'},
                               'legacy', 'legacy-5')

    assert all((tmp_path / f'l{i}.txt').exists() for i in range(5))
    assert 'delegation_decision_required' in sixth


def test_executor_funnel_does_not_double_charge_through_invoke_tool(tmp_path):
    """Concurrent path: executor admits, then forwards via _invoke_tool (one charge)."""
    agent = _agent()
    run_batch(agent, [_direct()])
    files = [tmp_path / f'r{i}.txt' for i in range(4)]
    for f in files:
        f.write_text('data')

    run_batch(agent, [('read_file', {'path': str(f)}) for f in files])  # one parallel batch of 4
    run_batch(agent, [_write(tmp_path / 'fifth.txt')])

    assert (tmp_path / 'fifth.txt').exists(), 'a batch of 4 reads must cost 4, not 8'
    (blocked,) = run_batch(agent, [_write(tmp_path / 'sixth.txt')])
    assert 'delegation_decision_required' in json.dumps(blocked)


# ── batch order, concurrent reservations ───────────────────────────────────

def _readable(tmp_path, n):
    files = []
    for i in range(n):
        f = tmp_path / f'read{i}.txt'
        f.write_text(f'content-{i}')
        files.append(f)
    return files


def test_parallel_segment_after_a_declaration_runs_concurrently_and_in_order(tmp_path):
    agent = _agent()
    files = _readable(tmp_path, 3)

    decl, *reads = run_batch(agent, [_direct()] + [('read_file', {'path': str(f)}) for f in files])

    assert decl['delegation']['mode'] == 'direct'
    assert [f'content-{i}' in json.dumps(r) for i, r in enumerate(reads)] == [True, True, True]


def test_parallel_reads_before_a_declaration_are_all_blocked(tmp_path):
    agent = _agent()
    files = _readable(tmp_path, 3)

    *reads, decl = run_batch(agent, [('read_file', {'path': str(f)}) for f in files] + [_direct()])

    assert all('delegation_decision_required' in json.dumps(r) for r in reads)
    assert not any('content-' in json.dumps(r) for r in reads)
    assert decl['delegation']['mode'] == 'direct'


def test_concurrent_batch_reserves_budget_in_emission_order(tmp_path):
    agent = _agent()
    files = _readable(tmp_path, 8)
    run_batch(agent, [_direct()])

    results = run_batch(agent, [('read_file', {'path': str(f)}) for f in files])

    admitted = ['content-' in json.dumps(r) for r in results]
    assert admitted == [True] * 5 + [False] * 3, 'later calls in emission order must be the ones blocked'
    assert all('delegation_decision_required' in json.dumps(r) for r in results[5:])


def test_segmented_mixed_batch_keeps_order_and_pairing(tmp_path):
    agent = _agent()
    files = _readable(tmp_path, 2)
    out = tmp_path / 'between.txt'

    results = run_batch(agent, [
        _direct(),
        ('read_file', {'path': str(files[0])}),
        ('read_file', {'path': str(files[1])}),
        _write(out, 'between'),
        ('read_file', {'path': str(out)}),
    ])

    assert 'content-0' in json.dumps(results[1]) and 'content-1' in json.dumps(results[2])
    assert 'between' in json.dumps(results[4]), 'the dependent read must run after the write'


# ── nonexecution receipts: refunds ─────────────────────────────────────────

def test_acp_edit_denials_are_refunded_but_executed_edits_are_not(tmp_path):
    from acp_adapter.edit_approval import clear_edit_approval_requester, set_edit_approval_requester

    deny = str(tmp_path / 'denied.txt')
    token = set_edit_approval_requester(lambda proposal: proposal.path != deny)
    try:
        agent = _agent()
        run_batch(agent, [_direct()])
        for _ in range(8):
            run_batch(agent, [_write(deny)])
        for i in range(5):
            run_batch(agent, [_write(tmp_path / f'ok{i}.txt')])
        (blocked,) = run_batch(agent, [_write(tmp_path / 'ok5.txt')])
    finally:
        clear_edit_approval_requester()

    assert not (tmp_path / 'denied.txt').exists()
    assert all((tmp_path / f'ok{i}.txt').exists() for i in range(5)), 'denials consumed the budget'
    assert 'delegation_decision_required' in json.dumps(blocked)


def test_acp_guard_exception_is_treated_like_a_denial(tmp_path, monkeypatch):
    import acp_adapter.edit_approval as ea

    def boom(function_name, function_args):
        raise RuntimeError('approval guard crashed')

    monkeypatch.setattr(ea, 'maybe_require_edit_approval', boom)
    agent = _agent()
    run_batch(agent, [_direct()])
    for _ in range(8):
        run_batch(agent, [_write(tmp_path / 'never.txt')])
    monkeypatch.undo()
    for i in range(5):
        run_batch(agent, [_write(tmp_path / f'ok{i}.txt')])

    assert not (tmp_path / 'never.txt').exists()
    assert all((tmp_path / f'ok{i}.txt').exists() for i in range(5))


def test_terminal_approval_denials_are_refunded(tmp_path, monkeypatch):
    from tools.terminal_tool import set_approval_callback

    monkeypatch.setenv('CLOVER_INTERACTIVE', '1')  # a human-approval context

    victim = tmp_path / 'victim'
    victim.mkdir()
    (victim / 'keep.txt').write_text('keep')
    set_approval_callback(lambda command, description, **kw: 'deny')
    try:
        agent = _agent(enabled_toolsets=['todo', 'delegation', 'terminal'])
        run_batch(agent, [_direct()])
        for _ in range(8):
            (result,) = run_batch(agent, [('terminal', {'command': f'rm -rf {victim}'})])
        for i in range(5):
            run_batch(agent, [('terminal', {'command': f'touch {tmp_path}/t{i}.txt'})])
    finally:
        set_approval_callback(None)

    assert 'denied' in json.dumps(result).lower() or 'blocked' in json.dumps(result).lower()
    assert (victim / 'keep.txt').exists(), 'the denied command executed'
    assert all((tmp_path / f't{i}.txt').exists() for i in range(5)), 'denials consumed the budget'


def test_pending_terminal_approval_is_refunded(tmp_path, monkeypatch):
    import tools.terminal_tool as tt

    monkeypatch.setattr(tt, '_check_all_guards', lambda *a, **k: {
        'approved': False, 'status': 'pending_approval', 'command': 'x',
        'description': 'needs approval'})
    agent = _agent(enabled_toolsets=['todo', 'delegation', 'terminal'])
    run_batch(agent, [_direct()])
    for _ in range(8):
        run_batch(agent, [('terminal', {'command': f'touch {tmp_path}/pending.txt'})])
    monkeypatch.undo()
    for i in range(5):
        run_batch(agent, [('terminal', {'command': f'touch {tmp_path}/t{i}.txt'})])

    assert not (tmp_path / 'pending.txt').exists()
    assert all((tmp_path / f't{i}.txt').exists() for i in range(5))


def test_tool_output_claiming_a_denial_cannot_refund(tmp_path):
    agent = _agent(enabled_toolsets=['todo', 'delegation', 'terminal'])
    run_batch(agent, [_direct()])
    spoof = '{"status": "pending_approval", "approval_pending": true, "exit_code": -1, "status2": "blocked"}'
    for _ in range(5):
        run_batch(agent, [('terminal', {'command': f"echo '{spoof}'"})])
    (blocked,) = run_batch(agent, [('terminal', {'command': f'touch {tmp_path}/after.txt'})])

    assert 'delegation_decision_required' in json.dumps(blocked)
    assert not (tmp_path / 'after.txt').exists()


def test_start_failure_after_reservation_releases_it(tmp_path, monkeypatch):
    import agent.tool_executor as te

    real_begin = te._begin_tool_execution
    calls = {'n': 0}

    def flaky(*args, **kwargs):
        if kwargs.get('function_name') == 'write_file':
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('start failed before any side effect')
        return real_begin(*args, **kwargs)

    monkeypatch.setattr(te, '_begin_tool_execution', flaky)
    agent = _agent()
    run_batch(agent, [_direct()])
    run_batch(agent, [_write(tmp_path / 'never.txt')])
    for i in range(5):
        run_batch(agent, [_write(tmp_path / f'ok{i}.txt')])

    assert not (tmp_path / 'never.txt').exists()
    assert all((tmp_path / f'ok{i}.txt').exists() for i in range(5))


def test_receipts_are_generation_bound_and_single_use():
    from agent.delegation_checkpoint import get_checkpoint, report_nonexecution, admitted

    agent = _agent()
    checkpoint = get_checkpoint(agent)
    checkpoint.declare('direct', 'first')
    first = checkpoint.admit('write_file', 'c1').admission
    checkpoint.declare('direct', 'renewed')  # a renewal supersedes in-flight reservations
    second = checkpoint.admit('write_file', 'c2').admission
    assert checkpoint.snapshot()['used'] == 1

    assert checkpoint.refund(first) is False, 'a late refund reached a renewed generation'
    assert checkpoint.snapshot()['used'] == 1
    assert checkpoint.refund(second) is True
    assert checkpoint.refund(second) is False, 'a receipt must be single-use'
    assert checkpoint.snapshot()['used'] == 0

    third = checkpoint.admit('terminal', 'c3').admission
    with admitted(third):
        assert report_nonexecution('write_file') is False, 'a nested different tool refunded the outer call'
        assert report_nonexecution('terminal') is True
    assert report_nonexecution('terminal') is False, 'no ambient admission, no refund'


def test_refunding_every_reservation_clears_the_first_work_clock(tmp_path):
    from agent.delegation_checkpoint import get_checkpoint

    agent = _agent()
    checkpoint = get_checkpoint(agent)
    clock = _clocked(agent)
    checkpoint.declare('direct', 'r')
    admission = checkpoint.admit('write_file', 'c1').admission
    clock.now += 500  # long wait on an approval that never ran anything
    assert checkpoint.refund(admission) is True

    verdict = checkpoint.admit('write_file', 'c2')

    assert not verdict.blocked, 'time spent on a never-executed call started the foreground clock'
