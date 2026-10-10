"""Per-message foreground time limit (delegation.foreground_time_limit_seconds).

A root conversational agent gets a time limit per human message. At 80% it
receives a one-time notice (cache-safe channel: the newest tool result); at
100% heavy work tools are refused until the next user message, while the
hand-off / control tools stay open. The clock is a fake one: no real sleeping.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from agent import foreground_time_limit as fl


# ── helpers ────────────────────────────────────────────────────────────────

class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(fl, '_now', c)
    return c


def _write_config(body: dict | None) -> None:
    from clover_constants import get_clover_home

    from clover_cli.config_defaults import DEFAULT_CONFIG

    path = get_clover_home() / 'config.yaml'
    # Current version: an old/missing one would send the file through migration.
    path.write_text(yaml.safe_dump(
        {'_config_version': DEFAULT_CONFIG['_config_version'], **(body or {})}))


def _set_limit(value: Any) -> None:
    _write_config({'delegation': {'foreground_time_limit_seconds': value}})


def _agent(platform: str = 'telegram', **overrides) -> Any:
    from run_agent import AIAgent

    kwargs = dict(
        provider='custom', api_mode='chat_completions',
        base_url='http://127.0.0.1:1/v1', api_key='local-test-credential',
        model='chosen-orchestrator', model_pinned=True,
        enabled_toolsets=['todo', 'delegation', 'file', 'terminal', 'cronjob', 'clarify'],
        max_iterations=6, quiet_mode=True, tool_progress_mode='off',
        skip_context_files=True, load_soul_identity=False,
        skip_memory=True, skip_background_review=True, platform=platform,
    )
    kwargs.update(overrides)
    return AIAgent(**kwargs)


def _call(name: str, arguments: dict, call_id: str):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(
        name=name, arguments=json.dumps(arguments)))


def run_batch(agent, calls, task_id='fg-limit-test'):
    tool_calls = [_call(n, a, f'call-{i}') for i, (n, a) in enumerate(calls)]
    wire = [{'id': tc.id, 'type': 'function',
             'function': {'name': tc.function.name, 'arguments': tc.function.arguments}}
            for tc in tool_calls]
    messages = [{'role': 'user', 'content': 'do the job'},
                {'role': 'assistant', 'content': None, 'tool_calls': wire}]
    agent._execute_tool_calls(SimpleNamespace(tool_calls=tool_calls), messages, task_id)
    decoded = []
    for message in messages[2:]:
        try:
            decoded.append(json.loads(message['content']))
        except (TypeError, ValueError):
            decoded.append(message['content'])
    return decoded


def _tool_messages():
    return [
        {'role': 'user', 'content': 'do the task'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 't1'}]},
        {'role': 'tool', 'tool_call_id': 't1', 'content': 'result one'},
    ]


def _limit_hit(result: Any) -> bool:
    return isinstance(result, dict) and result.get('error_type') == fl.BLOCK_CODE


# ── config: default, off switches, normalization ───────────────────────────

@pytest.mark.parametrize('raw,expected', [
    (300, 300.0), (120.5, 120.5), ('90', 90.0),
    (0, None), (-5, None), (None, None), ('abc', None), ('', None),
    (True, None), (False, None), (float('nan'), None), (float('inf'), None),
    ([], None), ({}, None),
])
def test_normalize_foreground_time_limit(raw, expected):
    assert fl.normalize_limit_seconds(raw) == expected


def test_default_is_300_when_config_is_absent():
    assert fl.load_limit_seconds() == 300.0


def test_default_config_matches_module_default():
    from clover_cli.config_defaults import DEFAULT_CONFIG

    assert (DEFAULT_CONFIG['delegation']['foreground_time_limit_seconds']
            == fl.DEFAULT_LIMIT_SECONDS)


def test_default_is_300_when_delegation_section_has_no_key():
    _write_config({'delegation': {'max_iterations': 10}})
    assert fl.load_limit_seconds() == 300.0


def test_configured_value_is_used():
    _set_limit(90)
    assert fl.load_limit_seconds() == 90.0


@pytest.mark.parametrize('value', [0, None, False, True, 'garbage', -3, []])
def test_zero_null_false_and_garbage_turn_it_off(value):
    _set_limit(value)
    assert fl.load_limit_seconds() is None


def test_off_means_no_clock_no_notice_no_refusal(clock):
    _set_limit(0)
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(10_000)
    assert fl.maybe_inject_notice(agent, _tool_messages()) is False
    assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is None


# ── root conversational agents: notice at 80%, refusal at 100% ──────────────

def test_notice_injected_once_at_eighty_percent(clock):
    agent = _agent()
    fl.begin_turn(agent)
    messages = _tool_messages()

    clock.advance(239)  # just under 80% of 300
    assert fl.maybe_inject_notice(agent, messages) is False
    assert messages == _tool_messages()

    clock.advance(2)  # 241s
    assert fl.maybe_inject_notice(agent, messages) is True
    assert messages[-1]['content'].startswith('result one')
    assert fl.NOTICE_MARKER in messages[-1]['content']
    # cache-safe: no synthetic user message, earlier turns untouched
    assert [m['role'] for m in messages] == ['user', 'assistant', 'tool']

    snapshot = [dict(m) for m in messages]
    clock.advance(30)
    assert fl.maybe_inject_notice(agent, messages) is False
    assert messages == snapshot
    assert messages[-1]['content'].count(fl.NOTICE_MARKER) == 1


def test_notice_retries_until_a_tool_result_exists(clock):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(250)
    messages = [{'role': 'user', 'content': 'task'}]
    assert fl.maybe_inject_notice(agent, messages) is False
    messages += _tool_messages()[1:]
    assert fl.maybe_inject_notice(agent, messages) is True


def test_notice_wording_hands_off_instead_of_stopping(clock):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(245)
    messages = _tool_messages()
    fl.maybe_inject_notice(agent, messages)
    text = messages[-1]['content']
    assert 'delegate_task' in text
    assert 'time limit' in text
    assert '4 minutes' in text and '5' in text
    assert 'produce the final deliverable' not in text.lower()
    assert 'from current state' not in text.lower()
    assert 'from the state you already have' not in text.lower()


def test_notice_handles_multimodal_tool_content(clock):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(250)
    messages = _tool_messages()
    messages[-1]['content'] = [{'type': 'text', 'text': 'block'}]
    assert fl.maybe_inject_notice(agent, messages) is True
    assert messages[-1]['content'][0] == {'type': 'text', 'text': 'block'}
    assert fl.NOTICE_MARKER in messages[-1]['content'][-1]['text']


def test_no_refusal_before_the_limit(clock):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(299)
    assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is None


def test_terminal_is_refused_after_the_limit_and_never_runs(clock, tmp_path):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(301)
    marker = tmp_path / 'ran.txt'

    (result,) = run_batch(agent, [('terminal', {'command': f'touch {marker}'})])

    assert _limit_hit(result), result
    assert not marker.exists(), 'terminal ran after the time limit'
    message = result['error']
    assert 'time limit' in message
    assert 'delegate_task' in message
    assert 'produce the final deliverable' not in message.lower()


def test_file_write_is_refused_after_the_limit(clock, tmp_path):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(301)
    target = tmp_path / 'out.txt'

    (result,) = run_batch(agent, [('write_file', {'path': str(target), 'content': 'x'})])

    assert _limit_hit(result)
    assert not target.exists()


def test_before_the_limit_the_same_write_works(clock, tmp_path):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(100)
    target = tmp_path / 'out.txt'

    (result,) = run_batch(agent, [('write_file', {'path': str(target), 'content': 'x'})])

    assert not _limit_hit(result)
    assert target.read_text() == 'x'


@pytest.mark.parametrize('name,args', [
    ('terminal', {'command': 'ls'}),
    ('execute_code', {'code': 'print(1)'}),
    ('browser_navigate', {'url': 'https://example.com'}),
    ('browser_click', {'ref': '@e1'}),
    ('computer_use', {'action': 'screenshot'}),
    ('web_search', {'query': 'x'}),
    ('web_extract', {'urls': ['https://example.com']}),
    ('write_file', {'path': '/tmp/x', 'content': 'x'}),
    ('patch', {'path': '/tmp/x', 'old_string': 'a', 'new_string': 'b'}),
    ('mcp__github__create_issue', {}),
])
def test_heavy_tools_are_refused_after_the_limit(clock, name, args):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(301)
    verdict = fl.refusal(agent, name, args)
    assert verdict is not None, name
    assert json.loads(verdict.block_result)['error_type'] == fl.BLOCK_CODE


@pytest.mark.parametrize('name,args', [
    ('delegate_task', {'goal': 'x'}),
    ('cronjob', {'action': 'list'}),
    ('todo', {}),
    ('memory', {'action': 'add', 'content': 'x'}),
    ('clarify', {'question': 'x'}),
    ('process', {'action': 'list'}),
    ('process', {'action': 'poll', 'session_id': 'x'}),
    ('process', {'action': 'kill', 'session_id': 'x'}),
    ('read_file', {'path': '/tmp/x'}),
    ('search_files', {'pattern': 'x'}),
    ('skill_view', {'name': 'x'}),
    ('send_message', {'target': 'x', 'message': 'x'}),
    ('terminal', {'command': 'sleep 900', 'background': True}),
])
def test_hand_off_and_control_tools_stay_open_after_the_limit(clock, name, args):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(10_000)
    assert fl.refusal(agent, name, args) is None, name


def test_tool_search_wrapper_is_peeled_to_the_real_tool(clock, monkeypatch):
    from agent import delegation_checkpoint as dc

    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(301)
    monkeypatch.setattr(dc, 'resolve_work_call',
                        lambda name, args: ('terminal', {'command': 'ls'}))
    assert fl.refusal(agent, 'tool_call', {'name': 'terminal'}) is not None
    monkeypatch.setattr(dc, 'resolve_work_call',
                        lambda name, args: ('cronjob', {'action': 'list'}))
    assert fl.refusal(agent, 'tool_call', {'name': 'cronjob'}) is None


def test_hand_off_tools_run_for_real_after_the_limit(clock, monkeypatch):
    """The cronjob hole: after the limit the agent can still schedule a job."""
    import run_agent

    monkeypatch.setattr(
        run_agent.AIAgent, 'run_conversation',
        lambda self, user_message=None, **kw: {
            'final_response': 'done', 'messages': [], 'api_calls': 1,
            'completed': True, 'input_tokens': 0, 'output_tokens': 0})
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(10_000)

    todo, cron, clarify = run_batch(agent, [
        ('todo', {'todos': [{'id': '1', 'content': 'hand off', 'status': 'pending'}]}),
        ('cronjob', {'action': 'list'}),
        ('clarify', {'question': 'which one?'}),
    ])

    assert not any(_limit_hit(r) for r in (todo, cron, clarify))
    assert 'todos' in todo


def test_new_user_message_resets_the_clock_and_the_notice(clock):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(400)
    messages = _tool_messages()
    assert fl.maybe_inject_notice(agent, messages) is True
    assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is not None

    fl.begin_turn(agent)  # next human message

    assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is None
    fresh = _tool_messages()
    assert fl.maybe_inject_notice(agent, fresh) is False
    clock.advance(250)
    assert fl.maybe_inject_notice(agent, fresh) is True


def test_other_platforms_in_scope(clock):
    for platform in ('telegram', 'discord', 'slack', 'api_server', 'cli', 'tui', 'webui'):
        agent = _agent(platform=platform)
        fl.begin_turn(agent)
        clock.advance(301)
        assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is not None, platform
        fl.begin_turn(agent)


# ── out of scope: no limit even with the config set ────────────────────────

def _assert_unlimited(agent, clock):
    fl.begin_turn(agent)
    clock.advance(100_000)
    messages = _tool_messages()
    assert fl.maybe_inject_notice(agent, messages) is False
    assert messages == _tool_messages()
    assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is None
    assert fl.refusal(agent, 'write_file', {'path': '/tmp/x', 'content': 'x'}) is None


def test_subagent_gets_no_limit(clock):
    _set_limit(300)
    agent = _agent(platform='subagent')
    agent._delegate_depth = 1
    _assert_unlimited(agent, clock)


def test_depth_one_agent_on_a_chat_platform_gets_no_limit(clock):
    _set_limit(300)
    agent = _agent(platform='telegram')
    agent._delegate_depth = 1
    _assert_unlimited(agent, clock)


def test_delegate_child_markers_get_no_limit(clock):
    """A child carries depth + subagent id + subagent platform (delegate_tool)."""
    _set_limit(300)
    child = _agent(platform='subagent')
    child._delegate_depth = 1
    child._subagent_id = 'sa-0'
    _assert_unlimited(child, clock)


def test_cron_run_gets_no_limit(clock):
    _set_limit(300)
    _assert_unlimited(_agent(platform='cron'), clock)


def test_oneshot_run_gets_no_limit(clock):
    _set_limit(300)
    agent = _agent(platform='cli')
    agent._delegation_checkpoint_exempt = 'oneshot'  # what clover_cli/oneshot.py sets
    _assert_unlimited(agent, clock)


def test_single_query_cli_run_gets_no_limit(clock):
    _set_limit(300)
    agent = _agent(platform='cli')
    agent._foreground_limit_exempt = 'single_query'
    _assert_unlimited(agent, clock)


def test_batch_and_background_roots_get_no_limit(clock):
    _set_limit(300)
    batch = _agent(platform='cli')
    batch._delegation_checkpoint_exempt = 'batch_runner'
    _assert_unlimited(batch, clock)
    background = _agent(platform='tui')
    background._foreground_limit_exempt = 'background'
    _assert_unlimited(background, clock)


@pytest.mark.parametrize('platform', [None, '', 'acp', 'curator', 'gateway_hygiene', 'batch'])
def test_library_and_internal_platforms_get_no_limit(clock, platform):
    _set_limit(300)
    _assert_unlimited(_agent(platform=platform), clock)


def test_kanban_worker_gets_no_limit(clock, monkeypatch):
    _set_limit(300)
    monkeypatch.setenv('CLOVER_KANBAN_TASK', 't-1')
    _assert_unlimited(_agent(platform='cli'), clock)


def test_agent_without_delegate_task_still_gets_the_limit(clock, tmp_path):
    """Point 6: no helper to hand off to changes the wording, not the limit."""
    _set_limit(300)
    agent = _agent(enabled_toolsets=['file', 'terminal'])
    assert 'delegate_task' not in agent.valid_tool_names
    fl.begin_turn(agent)
    clock.advance(301)
    assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is not None
    messages = _tool_messages()
    assert fl.maybe_inject_notice(agent, messages) is True
    assert 'delegate_task' not in messages[-1]['content']
    assert 'finish now' in messages[-1]['content'].lower()


# ── independence from the checkpoint and the run budget ────────────────────

def test_run_budget_is_untouched_by_the_limit(clock):
    from agent.conversation_loop import RUN_BUDGET_WRAPUP_NOTICE

    agent = _agent(platform='cli', run_budget_seconds=900)
    assert agent.run_budget_seconds == 900.0
    assert fl.NOTICE_MARKER not in RUN_BUDGET_WRAPUP_NOTICE


def test_limit_works_with_the_checkpoint_disabled_and_enabled(clock, tmp_path):
    _write_config({'delegation': {'foreground_time_limit_seconds': 300,
                                  'checkpoint': {'enabled': True}}})
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(301)
    # Checkpoint is on, but the time limit answers first and by itself.
    (result,) = run_batch(agent, [('write_file', {'path': str(tmp_path / 'a'), 'content': 'x'})])
    assert _limit_hit(result)
    assert not (tmp_path / 'a').exists()


def test_checkpoint_on_limit_off_behaves_as_before(clock, tmp_path):
    _write_config({'delegation': {'foreground_time_limit_seconds': 0,
                                  'checkpoint': {'enabled': True}}})
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(100_000)
    (result,) = run_batch(agent, [('write_file', {'path': str(tmp_path / 'a'), 'content': 'x'})])
    assert not _limit_hit(result)
    assert 'delegation_decision_required' in json.dumps(result)


# ── wired into a real conversation (mock provider, fake clock) ─────────────

def test_real_conversation_notice_refusal_and_reset(clock, tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    captured: list[dict] = []
    queue: list[dict] = []

    def text(t):
        return {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': t},
                             'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}

    def tool(name, args, cid):
        return {'choices': [{'index': 0, 'message': {
            'role': 'assistant', 'content': '',
            'tool_calls': [{'id': cid, 'type': 'function',
                            'function': {'name': name, 'arguments': json.dumps(args)}}]},
            'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            req = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            captured.append(req)
            resp = queue.pop(0) if queue else text('DONE')
            msg = resp['choices'][0]['message']
            if req.get('stream') is True:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                chunks = [{'id': 'm', 'choices': [{'index': 0, 'delta': {
                    'role': 'assistant', 'content': msg.get('content') or ''},
                    'finish_reason': None}]}]
                for i, tc in enumerate(msg.get('tool_calls') or []):
                    chunks.append({'id': 'm', 'choices': [{'index': 0, 'delta': {'tool_calls': [{
                        'index': i, 'id': tc['id'], 'type': 'function',
                        'function': tc['function']}]}, 'finish_reason': None}]})
                chunks.append({'id': 'm', 'choices': [{'index': 0, 'delta': {},
                                                       'finish_reason': resp['choices'][0]['finish_reason']}]})
                for c in chunks:
                    self.wfile.write(f'data: {json.dumps(c)}\n\n'.encode())
                self.wfile.write(b'data: [DONE]\n\n')
            else:
                body = json.dumps(resp).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *a, **k):
            pass

    srv = HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        agent = _agent(base_url=f'http://127.0.0.1:{srv.server_address[1]}/v1',
                       provider='openai-compat', api_key='k')
        original = agent._execute_tool_calls

        def advancing(*a, **k):
            out = original(*a, **k)
            clock.advance(100)  # each tool round "takes" 100 fake seconds
            return out

        agent._execute_tool_calls = advancing
        target = tmp_path / 'late.txt'
        todo_args = {'todos': [{'id': '1', 'content': 'x', 'status': 'pending'}]}
        queue[:] = [
            tool('todo', todo_args, 'c1'),                                  # t=100 after
            tool('todo', todo_args, 'c2'),                                  # t=200 after
            tool('todo', todo_args, 'c3'),                                  # t=300 (>80%, =100%) after
            tool('write_file', {'path': str(target), 'content': 'x'}, 'c4'),  # refused
            text('handing this to a helper'),
        ]
        result = agent.run_conversation('big job', conversation_history=[], task_id='fg')
        assert result['final_response'] == 'handing this to a helper'
        assert not target.exists(), 'write ran after the time limit'

        tool_texts = [m['content'] for r in captured for m in r.get('messages', [])
                      if m.get('role') == 'tool' and isinstance(m.get('content'), str)]
        joined = '\n'.join(tool_texts)
        assert fl.NOTICE_MARKER in joined
        # the final request carries exactly one copy of the notice in history
        last_tools = [m['content'] for m in captured[-1].get('messages', []) if m.get('role') == 'tool']
        assert sum(t.count(fl.NOTICE_MARKER) for t in last_tools) == 1
        assert fl.BLOCK_CODE in last_tools[-1]

        # next human message: clock restarts, the same write now works
        queue[:] = [tool('write_file', {'path': str(target), 'content': 'x'}, 'c5'),
                    text('ok')]
        agent.run_conversation('another thing', conversation_history=[], task_id='fg2')
        assert target.read_text() == 'x'
    finally:
        srv.shutdown()


# ═══ v2.2 contract ═════════════════════════════════════════════════════════

def test_clock_is_monotonic_not_wall_time(monkeypatch):
    """No patched ``_now``: the module must follow ``time.monotonic``."""
    import time

    agent = _agent()
    mono = {'t': 100.0}
    monkeypatch.setattr(time, 'monotonic', lambda: mono['t'])
    # A wall clock that jumps (NTP step, DST, suspend) must not matter.
    monkeypatch.setattr(time, 'time', lambda: 9_999_999.0)
    fl.begin_turn(agent)

    monkeypatch.setattr(time, 'time', lambda: 1.0)  # wall clock jumps backwards
    assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is None
    monkeypatch.setattr(time, 'time', lambda: 2e12)  # ... and forwards
    assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is None
    mono['t'] += 301
    assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is not None


def test_remaining_seconds_counts_down_and_clamps(clock):
    agent = _agent()
    assert fl.remaining_seconds(agent) is None  # no turn yet
    fl.begin_turn(agent)
    clock.advance(100)
    assert fl.remaining_seconds(agent) == pytest.approx(200)
    clock.advance(10_000)
    assert fl.remaining_seconds(agent) == 0.0


def test_end_of_turn_stops_the_limit_from_applying_to_stray_tool_calls(clock):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(400)
    assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is not None
    fl.end_turn(agent)
    assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is None


# ── provenance: internal notifications inherit, humans reset ───────────────

def test_internal_notification_inherits_the_remaining_time(clock):
    agent = _agent()
    fl.begin_turn(agent, None)  # human message
    clock.advance(100)
    fl.end_turn(agent)
    fl.begin_turn(agent, 'internal_notification')  # helper finished
    assert fl.remaining_seconds(agent) == pytest.approx(200)
    clock.advance(150)  # 250 s into the ORIGINAL episode: notice due
    assert fl.maybe_inject_notice(agent, _tool_messages()) is True


def test_internal_notification_after_expiry_starts_in_finish_mode(clock):
    agent = _agent()
    fl.begin_turn(agent, None)
    clock.advance(400)
    fl.end_turn(agent)
    fl.begin_turn(agent, 'internal_notification')
    assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is not None
    assert fl.refusal(agent, 'read_file', {'path': '/tmp/x'}) is None
    assert fl.refusal(agent, 'todo', {}) is None


def test_internal_notification_on_a_fresh_agent_does_not_mint_a_budget(clock):
    agent = _agent()  # never ran a human turn: no episode state
    fl.begin_turn(agent, 'internal_notification')
    assert fl.remaining_seconds(agent) == 0.0
    assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is not None
    # ... but the integration path stays open
    for name, args in [('read_file', {'path': '/tmp/x'}), ('search_files', {'pattern': 'x'}),
                       ('skill_view', {'name': 'x'}), ('todo', {}),
                       ('send_message', {'target': 'x', 'message': 'y'})]:
        assert fl.refusal(agent, name, args) is None, name


def test_unknown_display_kind_is_a_human_turn(clock):
    agent = _agent()
    fl.begin_turn(agent, None)
    clock.advance(400)
    fl.begin_turn(agent, 'something_else')
    assert fl.remaining_seconds(agent) == pytest.approx(300)


def test_a_human_message_resets_after_an_internal_one(clock):
    agent = _agent()
    fl.begin_turn(agent, None)
    clock.advance(400)
    fl.begin_turn(agent, 'internal_notification')
    fl.begin_turn(agent, None)  # queued human input consumed as its own turn
    assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is None
    assert fl.remaining_seconds(agent) == pytest.approx(300)


# ── finish mode: new refusals ──────────────────────────────────────────────

def test_process_wait_is_refused_in_finish_mode_but_poll_log_kill_list_work(clock):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(301)
    wait = fl.refusal(agent, 'process', {'action': 'wait', 'session_id': 'p1'})
    assert wait is not None
    assert json.loads(wait.block_result)['blocked_tool'] == 'process'
    for action in ('poll', 'log', 'kill', 'list'):
        assert fl.refusal(agent, 'process', {'action': action, 'session_id': 'p1'}) is None, action


def test_process_wait_is_open_before_the_limit(clock):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(10)
    assert fl.refusal(agent, 'process', {'action': 'wait', 'session_id': 'p1'}) is None


def test_a_surface_without_async_delivery_is_not_limited_at_all(clock):
    """Fix round 2: no deliverable helper -> the limit is OFF for the turn."""
    from gateway.session_context import declare_stateless_channel

    declare_stateless_channel()
    try:
        agent = _agent(platform='api_server')
        fl.begin_turn(agent)
        assert fl.remaining_seconds(agent) is None
        clock.advance(10_000)
        assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is None
        assert fl.refusal(agent, 'delegate_task', {'goal': 'x'}) is None
        assert fl.refusal(agent, 'process', {'action': 'wait', 'session_id': 'p'}) is None
        assert fl.maybe_inject_notice(agent, _tool_messages()) is False
        assert fl.hard_stop_due(agent, _after_tool_round()) is False
        assert fl.forces_background(agent) is False
        assert fl.codex_turn_budget(agent) is None
    finally:
        import gateway.session_context as sc

        sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)


def test_the_off_decision_is_inherited_by_internal_notifications(clock):
    import gateway.session_context as sc

    sc.declare_stateless_channel()
    try:
        agent = _agent(platform='api_server')
        fl.begin_turn(agent, None)  # human message on the undeliverable surface
        fl.end_turn(agent)
        # even if the context later looked deliverable, the episode stays off
        sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)
        clock.advance(10_000)
        fl.begin_turn(agent, 'internal_notification')
        assert fl.remaining_seconds(agent) is None
        assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is None
    finally:
        sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)


def test_fresh_instance_internal_turn_on_an_undeliverable_surface_is_off(clock):
    import gateway.session_context as sc

    sc.declare_stateless_channel()
    try:
        agent = _agent(platform='api_server')
        fl.begin_turn(agent, 'internal_notification')
        assert fl.remaining_seconds(agent) is None
        assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is None
    finally:
        sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)


def test_an_api_server_session_with_a_wake_id_is_limited_and_forced_background(clock, monkeypatch):
    import gateway.session_context as sc

    monkeypatch.setenv('CLOVER_SESSION_ID', 'raw-sid-3')
    sc.set_session_vars(platform='api_server', chat_id='raw-sid-3',
                        session_key='raw-sid-3', session_id='raw-sid-3',
                        async_delivery=False)
    try:
        agent = _agent(platform='api_server')
        fl.begin_turn(agent)
        assert fl.remaining_seconds(agent) is not None
        assert fl.forces_background(agent) is True
        clock.advance(301)
        assert fl.refusal(agent, 'terminal', {'command': 'ls'}) is not None
    finally:
        for var in sc._VAR_MAP.values():
            var.set(sc._UNSET)
        sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)


def test_delegate_task_is_forced_background_not_refused_where_delivery_exists(clock):
    agent = _agent(platform='telegram')
    fl.begin_turn(agent)
    clock.advance(301)
    assert fl.forces_background(agent) is True
    assert fl.refusal(agent, 'delegate_task', {'goal': 'x'}) is None


def test_forces_background_is_off_without_a_limited_turn(clock):
    agent = _agent(platform='telegram')
    assert fl.forces_background(agent) is False  # no turn started
    _set_limit(0)
    fl.begin_turn(agent)
    assert fl.forces_background(agent) is False


# ── hard stop decision ─────────────────────────────────────────────────────

def _after_tool_round():
    return [{'role': 'user', 'content': 'job'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 't1'}]},
            {'role': 'tool', 'tool_call_id': 't1', 'content': 'r'}]


def test_hard_stop_not_due_before_the_limit_or_inside_the_window(clock):
    agent = _agent()
    fl.begin_turn(agent)
    assert fl.hard_stop_due(agent, _after_tool_round()) is False
    clock.advance(301)
    assert fl.hard_stop_due(agent, _after_tool_round()) is False  # window just opened
    clock.advance(58)  # 359 s: one second of the 60 s grace left
    assert fl.hard_stop_due(agent, _after_tool_round()) is False


def test_hard_stop_due_after_the_grace_period(clock):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(361)
    assert fl.hard_stop_due(agent, _after_tool_round()) is True


def test_hard_stop_due_after_three_model_iterations_in_finish_mode(clock):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(301)
    for _ in range(3):
        assert fl.hard_stop_due(agent, _after_tool_round()) is False
        fl.note_model_call(agent)
    assert fl.hard_stop_due(agent, _after_tool_round()) is True


def test_model_calls_before_the_limit_do_not_count_toward_the_window(clock):
    agent = _agent()
    fl.begin_turn(agent)
    for _ in range(10):
        fl.note_model_call(agent)
    clock.advance(301)
    assert fl.hard_stop_due(agent, _after_tool_round()) is False


@pytest.mark.parametrize('tail', [
    {'role': 'user', 'content': 'restored ask after compaction'},
    {'role': 'system', 'content': 'x'},
])
def test_hard_stop_only_fires_when_the_last_message_is_tool_or_assistant(clock, tail):
    """handle_max_iterations appends a user message: two users in a row is a
    provider 400, so the stop is deferred to the next boundary."""
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(1000)
    messages = _after_tool_round() + [tail]
    assert fl.hard_stop_due(agent, messages) is False
    assert fl.hard_stop_due(agent, _after_tool_round()) is True
    assert fl.hard_stop_due(
        agent, _after_tool_round()[:2] + [{'role': 'assistant', 'content': 'x'}]) is True


def test_hard_stop_is_inert_for_unlimited_turns(clock):
    _set_limit(0)
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(100_000)
    assert fl.hard_stop_due(agent, _after_tool_round()) is False


def test_hard_stop_prefers_a_pending_answer_over_another_model_call(clock):
    """A composed answer withheld by a verification gate is the best result;
    the finalizer already prefers it to a fallible extra call."""
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(1000)
    calls = []
    agent._handle_max_iterations = lambda *a, **k: calls.append((a, k)) or 'MODEL SUMMARY'

    text, reason = fl.run_hard_stop(
        agent, _after_tool_round(), 7,
        pending_answer='the composed answer', pending_previewed=True)

    assert text == 'the composed answer'
    assert reason == fl.HARD_STOP_EXIT_REASON
    assert not calls, 'no model call when an answer is already composed'
    assert agent._response_was_previewed is True


def test_hard_stop_without_a_pending_answer_calls_handle_max_iterations_once(clock):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(1000)
    calls = []
    agent._handle_max_iterations = lambda messages, n, **k: (
        calls.append((messages, n, k)) or 'MODEL SUMMARY')
    messages = _after_tool_round()

    text, reason = fl.run_hard_stop(agent, messages, 7)

    assert text == 'MODEL SUMMARY'
    assert reason == fl.HARD_STOP_EXIT_REASON
    assert len(calls) == 1 and calls[0][0] is messages and calls[0][1] == 7
    assert reason not in {'unknown', 'budget_exhausted'}  # finalizer must not re-run


# ── turn_notices: the unsent-tool-result channel (shared with run budget) ───

def _fresh_agent_state():
    return SimpleNamespace()


def test_attach_requires_the_unsent_tool_tail_the_loop_recorded():
    from agent import turn_notices as tn

    agent = _fresh_agent_state()
    messages = _tool_messages()
    tn.note_fresh_tool_results(agent, messages, start_index=2)
    original = messages[-1]

    assert tn.attach_to_fresh_tool_result(agent, messages, 'NOTE') is True
    assert messages[-1] is not original, 'must copy before modifying'
    assert original['content'] == 'result one', 'original dict was mutated'
    assert messages[-1]['content'].endswith('NOTE')
    assert messages[-1]['tool_call_id'] == 't1'


def test_a_tool_result_the_model_already_saw_is_never_modified():
    from agent import turn_notices as tn

    agent = _fresh_agent_state()
    messages = _tool_messages()
    tn.note_fresh_tool_results(agent, messages, start_index=2)
    tn.consume_fresh_tool_tail(agent)  # the request that carried it was sent

    assert tn.attach_to_fresh_tool_result(agent, messages, 'NOTE') is False
    assert messages == _tool_messages()


def test_empty_response_nudge_tail_is_not_a_fresh_tool_result():
    from agent import turn_notices as tn

    agent = _fresh_agent_state()
    messages = _tool_messages()
    tn.note_fresh_tool_results(agent, messages, start_index=2)
    tn.consume_fresh_tool_tail(agent)
    messages += [{'role': 'assistant', 'content': '(empty)'},
                 {'role': 'user', 'content': 'please continue'}]

    assert tn.attach_to_fresh_tool_result(agent, messages, 'NOTE') is False


def test_post_compaction_list_is_not_a_fresh_tool_result():
    from agent import turn_notices as tn

    agent = _fresh_agent_state()
    messages = _tool_messages()
    tn.note_fresh_tool_results(agent, messages, start_index=2)
    compacted = [{'role': 'user', 'content': 'summary'},
                 {'role': 'tool', 'tool_call_id': 't1', 'content': 'result one'}]

    assert tn.attach_to_fresh_tool_result(agent, compacted, 'NOTE') is False
    assert compacted[-1]['content'] == 'result one'


def test_without_loop_tracking_only_the_final_message_may_carry_the_notice():
    """Legacy/stub callers: never reach back past a non-tool tail."""
    from agent import turn_notices as tn

    agent = _fresh_agent_state()
    behind = _tool_messages() + [{'role': 'assistant', 'content': 'x'}]
    assert tn.attach_to_fresh_tool_result(agent, behind, 'NOTE') is False
    assert behind == _tool_messages() + [{'role': 'assistant', 'content': 'x'}]
    assert tn.attach_to_fresh_tool_result(agent, _tool_messages(), 'NOTE') is True


def test_notice_in_a_toolless_first_iteration_leaves_history_alone(clock):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(250)
    history_tool = {'role': 'tool', 'tool_call_id': 'old', 'content': 'OLD'}
    messages = [{'role': 'user', 'content': 'earlier'},
                {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'old'}]},
                history_tool,
                {'role': 'assistant', 'content': 'answer'},
                {'role': 'user', 'content': 'new question'}]
    assert fl.maybe_inject_notice(agent, messages) is False
    assert history_tool['content'] == 'OLD'
    assert fl.NOTICE_MARKER not in json.dumps(messages)


def test_notice_latches_only_on_a_successful_attach(clock):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(250)
    assert fl.maybe_inject_notice(agent, [{'role': 'user', 'content': 'x'}]) is False
    assert fl.maybe_inject_notice(agent, _tool_messages()) is True
    assert fl.maybe_inject_notice(agent, _tool_messages()) is False  # latched


# ── run-budget notice shares the fix ───────────────────────────────────────

def test_run_budget_wrapup_never_modifies_a_tool_result_already_sent():
    import time as _time

    from agent import turn_notices as tn
    from agent.conversation_loop import (
        RUN_BUDGET_WRAPUP_NOTICE,
        _maybe_inject_run_budget_wrapup,
    )

    agent = SimpleNamespace(
        run_budget_seconds=900, _run_budget_started_at=_time.time() - 800,
        _run_budget_wrapup_injected=False)
    messages = _tool_messages()
    tn.note_fresh_tool_results(agent, messages, start_index=2)
    tn.consume_fresh_tool_tail(agent)  # already sent to the provider

    assert _maybe_inject_run_budget_wrapup(agent, messages) is False
    assert agent._run_budget_wrapup_injected is False
    assert messages == _tool_messages()

    tn.note_fresh_tool_results(agent, messages, start_index=2)  # a fresh round
    original = messages[-1]
    assert _maybe_inject_run_budget_wrapup(agent, messages) is True
    assert original['content'] == 'result one', 'run-budget notice mutated in place'
    assert RUN_BUDGET_WRAPUP_NOTICE in messages[-1]['content']


# ── eligibility: execution mode, not flags ─────────────────────────────────

def test_notice_wording_names_background_true(clock):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(245)
    messages = _tool_messages()
    fl.maybe_inject_notice(agent, messages)
    assert 'background=true' in messages[-1]['content']


def test_refusal_wording_promises_a_fresh_window_for_a_follow_up(clock):
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(301)
    message = json.loads(fl.refusal(agent, 'terminal', {'command': 'ls'}).block_result)['error']
    assert 'follow-up message gets a fresh' in message
    assert 'resets' not in message.lower()


def test_repeated_process_wait_is_refused_every_time_through_the_executor(clock, monkeypatch):
    """A model that keeps polling with ``wait`` must never block past the limit."""
    from tools.process_registry import process_registry

    def _never(*_a, **_k):
        raise AssertionError('process wait ran after the time limit')

    monkeypatch.setattr(process_registry, 'wait', _never)
    agent = _agent()
    fl.begin_turn(agent)
    clock.advance(301)

    results = run_batch(agent, [
        ('process', {'action': 'wait', 'session_id': 'p1', 'timeout': 600}),
        ('process', {'action': 'wait', 'session_id': 'p1', 'timeout': 600}),
        ('process', {'action': 'wait', 'session_id': 'p2'}),
    ])

    assert all(_limit_hit(r) and r['blocked_tool'] == 'process' for r in results), results


def test_delegate_task_stays_open_on_a_session_that_can_be_woken(clock, monkeypatch):
    """API server with a raw session id: a detached helper can be delivered."""
    import gateway.session_context as sc

    monkeypatch.setenv('CLOVER_SESSION_ID', 'raw-sid-9')
    sc.set_session_vars(platform='api_server', chat_id='raw-sid-9',
                        session_key='raw-sid-9', session_id='raw-sid-9',
                        async_delivery=False)
    try:
        agent = _agent(platform='api_server')
        fl.begin_turn(agent)
        clock.advance(301)
        assert fl.refusal(agent, 'delegate_task', {'goal': 'x'}) is None
    finally:
        for var in sc._VAR_MAP.values():
            var.set(sc._UNSET)
        sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)


def test_codex_turn_budget_is_the_remaining_time_then_the_finish_window(clock):
    agent = _agent()
    assert fl.codex_turn_budget(agent) is None  # no turn: unbounded as before
    fl.begin_turn(agent, None)
    clock.advance(100)
    assert fl.codex_turn_budget(agent) == pytest.approx(200)
    clock.advance(250)  # spent; a later internal turn gets the finish window
    fl.end_turn(agent)
    fl.begin_turn(agent, 'internal_notification')
    assert fl.codex_turn_budget(agent) == pytest.approx(fl.HARD_STOP_GRACE_SECONDS)
    clock.advance(30)
    assert fl.codex_turn_budget(agent) == pytest.approx(fl.HARD_STOP_GRACE_SECONDS - 30)


def test_codex_turn_budget_is_off_when_the_limit_is_off(clock):
    _set_limit(0)
    agent = _agent()
    fl.begin_turn(agent)
    assert fl.codex_turn_budget(agent) is None


def test_codex_budget_never_drops_below_the_finish_window_for_an_inherited_turn(clock):
    """An inherited internal turn with a sliver of time left is not cut at that
    sliver: the normal loop grants the finish window at expiry, so does this."""
    agent = _agent()
    fl.begin_turn(agent, None)
    clock.advance(299.7)  # 0.3 s left
    fl.end_turn(agent)
    fl.begin_turn(agent, 'internal_notification')
    assert fl.codex_turn_budget(agent) == pytest.approx(fl.HARD_STOP_GRACE_SECONDS)
    # a fresh human turn with plenty of time left keeps its remainder
    fl.begin_turn(agent, None)
    clock.advance(100)
    assert fl.codex_turn_budget(agent) == pytest.approx(200)
