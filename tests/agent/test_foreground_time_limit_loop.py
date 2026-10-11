"""Foreground time limit wired into the real conversation loop (C1.4 P1).

Real ``AIAgent.run_conversation``, a real ``SessionDB`` in the scratch
``CLOVER_HOME``, a local mock provider over HTTP and a fake monotonic clock
(no sleeping).  The tool executor, the loop's notice channel and the hard-stop
finalizer are the production ones.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Optional

import pytest
import yaml

from agent import foreground_time_limit as fl


class FakeClock:
    def __init__(self) -> None:
        self.t = 5000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(fl, '_now', c)
    return c


def _write_limit(seconds: Any = 300) -> None:
    from clover_constants import get_clover_home

    from clover_cli.config_defaults import DEFAULT_CONFIG

    (get_clover_home() / 'config.yaml').write_text(yaml.safe_dump({
        '_config_version': DEFAULT_CONFIG['_config_version'],
        'delegation': {'foreground_time_limit_seconds': seconds},
    }))


@pytest.fixture(autouse=True)
def _limit_on():
    _write_limit(300)


def text(t: str) -> dict:
    return {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': t},
                         'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}


def tool(name: str, args: dict, cid: str) -> dict:
    return {'choices': [{'index': 0, 'message': {
        'role': 'assistant', 'content': '',
        'tool_calls': [{'id': cid, 'type': 'function',
                        'function': {'name': name, 'arguments': json.dumps(args)}}]},
        'finish_reason': 'tool_calls'}],
        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}


class Provider:
    """Scripted OpenAI-compatible endpoint. ``before`` runs before each answer."""

    def __init__(self) -> None:
        self.captured: list[dict] = []
        self.queue: list[dict] = []
        self.before: Optional[Callable[[int, dict], None]] = None
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                req = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                if 'messages' not in req:  # a metadata probe, not a model call
                    body = b'{}'
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                provider.captured.append(req)
                if provider.before is not None:
                    provider.before(len(provider.captured), req)
                resp = provider.queue.pop(0) if provider.queue else text('DONE')
                msg = resp['choices'][0]['message']
                if req.get('stream') is True:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    chunks = [{'id': 'm', 'choices': [{'index': 0, 'delta': {
                        'role': 'assistant', 'content': msg.get('content') or ''},
                        'finish_reason': None}]}]
                    for i, tc in enumerate(msg.get('tool_calls') or []):
                        chunks.append({'id': 'm', 'choices': [{'index': 0, 'delta': {
                            'tool_calls': [{'index': i, 'id': tc['id'], 'type': 'function',
                                            'function': tc['function']}]},
                            'finish_reason': None}]})
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

        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f'http://127.0.0.1:{self.server.server_address[1]}/v1'

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def provider():
    p = Provider()
    yield p
    p.close()


def make_agent(provider: Provider, platform: str = 'telegram', **kw):
    from run_agent import AIAgent

    kwargs = dict(
        provider='openai-compat', api_mode='chat_completions',
        base_url=provider.base_url, api_key='k',
        model='chosen-orchestrator', model_pinned=True,
        enabled_toolsets=['todo', 'delegation', 'file', 'terminal', 'cronjob', 'clarify'],
        max_iterations=40, quiet_mode=True, tool_progress_mode='off',
        skip_context_files=True, load_soul_identity=False,
        skip_memory=True, skip_background_review=True, platform=platform,
    )
    kwargs.update(kw)
    return AIAgent(**kwargs)


def advance_per_tool_round(agent, clock: FakeClock, seconds: float = 130,
                           after_limit: Optional[float] = None, limit: float = 300.0):
    """Each tool round "takes" ``seconds`` fake seconds; once the episode is
    past ``limit`` it takes ``after_limit`` instead (default: the same)."""
    original = agent._execute_tool_calls
    start = clock.t

    def advancing(*a, **k):
        out = original(*a, **k)
        past = after_limit is not None and clock.t - start >= limit
        clock.advance(after_limit if past else seconds)
        return out

    agent._execute_tool_calls = advancing


TODO = {'todos': [{'id': '1', 'content': 'x', 'status': 'pending'}]}


def tool_texts(request: dict) -> list[str]:
    return [m['content'] for m in request.get('messages', [])
            if m.get('role') == 'tool' and isinstance(m.get('content'), str)]


# ── hard stop ──────────────────────────────────────────────────────────────

def test_hard_stop_after_three_more_iterations_makes_one_toolless_final_call(clock, provider):
    agent = make_agent(provider)
    # 100 s rounds up to the limit, then 5 s rounds: the 60 s grace never fires
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    provider.queue[:] = (
        [tool('todo', TODO, f'c{i}') for i in range(1, 20)]  # model never stops
    )

    def before(n, req):
        if not req.get('tools'):  # the final toolless call gets this answer
            provider.queue.insert(0, text('HANDING OFF: summary of what is done'))

    provider.before = before

    result = agent.run_conversation('big job', conversation_history=[], task_id='hs')

    assert result['final_response'] == 'HANDING OFF: summary of what is done'
    assert result['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON
    assert result['completed'] is True and not result['failed']
    with_tools = [r for r in provider.captured if r.get('tools')]
    toolless = [r for r in provider.captured if not r.get('tools')]
    assert len(toolless) == 1, 'exactly one final summary call, no second summary'
    # limit reached after the 3rd 100 s round; then 3 more tool iterations
    assert len(with_tools) == 3 + 3
    assert provider.captured[-1] is toolless[0]
    # role-safe: the summary request is the only synthetic user row, at the end
    roles = [m['role'] for m in result['messages']]
    assert roles[-1] == 'assistant'
    assert all(not (a == b == 'user') for a, b in zip(roles, roles[1:]))
    assert result['messages'][-1]['content'] == 'HANDING OFF: summary of what is done'


def test_hard_stop_by_grace_period(clock, provider):
    agent = make_agent(provider)
    original = agent._execute_tool_calls
    rounds = {'n': 0}

    def burning(*a, **k):
        out = original(*a, **k)
        rounds['n'] += 1
        clock.advance(301 if rounds['n'] == 1 else 61)  # past limit, then past grace
        return out

    agent._execute_tool_calls = burning
    provider.queue[:] = [tool('todo', TODO, 'c1'), tool('todo', TODO, 'c2'),
                         tool('todo', TODO, 'c3'), text('SUMMARY')]

    result = agent.run_conversation('job', conversation_history=[], task_id='hs2')

    assert result['final_response'] == 'SUMMARY'
    assert result['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON
    assert len([r for r in provider.captured if r.get('tools')]) == 2  # c1, c2 rounds


def test_tool_admitted_before_the_limit_runs_then_repeated_allowed_calls_hit_the_hard_stop(
        clock, provider, tmp_path):
    marker = tmp_path / 'admitted.txt'
    agent = make_agent(provider)
    original = agent._execute_tool_calls
    rounds = {'n': 0}

    def slow(*a, **k):
        rounds['n'] += 1
        if rounds['n'] == 1:
            clock.advance(298)  # a long first round ...
        out = original(*a, **k)
        # ... leaves the clock at 4:59 when round 2's terminal is admitted; it
        # finishes past 5:00 and every later (allowed) round takes 1 s.
        clock.advance(2 if rounds['n'] == 2 else 1)
        return out

    agent._execute_tool_calls = slow
    provider.queue[:] = (
        [tool('todo', TODO, 'c1'),
         tool('terminal', {'command': f'touch {marker}'}, 'c2')]
        + [tool('todo', TODO, f'r{i}') for i in range(8)]  # allowed, forever
    )
    provider.before = lambda n, req: (
        None if req.get('tools') else provider.queue.insert(0, text('FINAL')))

    result = agent.run_conversation('job', conversation_history=[], task_id='hs3')

    assert marker.exists(), 'the call admitted at 4:59 must run to completion'
    assert result['final_response'] == 'FINAL'
    assert result['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON
    # three more model iterations after the limit, then the toolless call
    assert len([r for r in provider.captured if r.get('tools')]) == 2 + 3
    assert len([r for r in provider.captured if not r.get('tools')]) == 1


def test_a_slow_admitted_tool_means_no_more_iterations_just_the_hard_stop(
        clock, provider, tmp_path):
    """The stop lands at the next iteration boundary: a tool that ran past the
    grace period earns no further tool iterations."""
    marker = tmp_path / 'slow.txt'
    agent = make_agent(provider)
    original = agent._execute_tool_calls

    def slow(*a, **k):
        clock.advance(299)  # admitted at 4:59 ...
        out = original(*a, **k)
        clock.advance(120)  # ... and runs two minutes
        return out

    agent._execute_tool_calls = slow
    provider.queue[:] = [tool('terminal', {'command': f'touch {marker}'}, 'c1')] + [
        tool('todo', TODO, f'r{i}') for i in range(5)]
    provider.before = lambda n, req: (
        None if req.get('tools') else provider.queue.insert(0, text('WRAPPED UP')))

    result = agent.run_conversation('job', conversation_history=[], task_id='hs4')

    assert marker.exists()
    assert result['final_response'] == 'WRAPPED UP'
    assert len([r for r in provider.captured if r.get('tools')]) == 1
    assert len([r for r in provider.captured if not r.get('tools')]) == 1


def test_limit_applies_without_delegate_task_and_the_notice_says_finish_now(clock, provider, tmp_path):
    agent = make_agent(provider, enabled_toolsets=['todo', 'file', 'terminal'])
    assert 'delegate_task' not in agent.valid_tool_names
    advance_per_tool_round(agent, clock, 100)
    target = tmp_path / 'late.txt'
    provider.queue[:] = [
        tool('todo', TODO, 'c1'), tool('todo', TODO, 'c2'), tool('todo', TODO, 'c3'),
        tool('write_file', {'path': str(target), 'content': 'x'}, 'c4'),
        text('finishing now'),
    ]

    result = agent.run_conversation('job', conversation_history=[], task_id='nd')

    assert result['final_response'] == 'finishing now'
    assert not target.exists(), 'write ran after the limit'
    notices = [t for r in provider.captured for t in tool_texts(r) if fl.NOTICE_MARKER in t]
    assert notices, 'no notice was delivered'
    assert all('delegate_task' not in n.split(fl.NOTICE_MARKER, 1)[1] for n in notices)
    assert any('finish now' in n.lower() for n in notices)


def test_hard_stop_persists_the_transcript_and_keeps_queued_steer(clock, provider, tmp_path):
    from clover_state import SessionDB

    db = SessionDB(tmp_path / 'state.db')
    agent = make_agent(provider, session_db=db, session_id='fg-hard-stop')
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    provider.queue[:] = [tool('todo', TODO, f'c{i}') for i in range(1, 12)]

    def before(n, req):
        if not req.get('tools'):
            # human input arrives while the final call is in flight
            agent.steer('and also check the second table')
            provider.queue.insert(0, text('SUMMARY'))

    provider.before = before
    result = agent.run_conversation('job', conversation_history=[], task_id='ps')

    assert result['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON
    stored = db.get_messages('fg-hard-stop')
    assert stored[-1]['role'] == 'assistant' and stored[-1]['content'] == 'SUMMARY'
    assert any(m['role'] == 'tool' for m in stored), 'tool rows were not persisted'
    # queued human input is handed back to the caller, not dropped
    assert 'second table' in (result.get('pending_steer') or '')


# ── notice: cache-safe and never touches earlier turns ──────────────────────

def test_notice_lands_on_this_iterations_unsent_tool_result_only(clock, provider):
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 130)
    provider.queue[:] = [tool('todo', TODO, 'c1'), tool('todo', TODO, 'c2'), text('ok')]

    agent.run_conversation('job', conversation_history=[], task_id='nt')

    assert not any(fl.NOTICE_MARKER in t for t in tool_texts(provider.captured[1])), \
        'notice appeared before 80%'
    third = tool_texts(provider.captured[2])  # after round 2: t=260 > 240
    assert sum(t.count(fl.NOTICE_MARKER) for t in third) == 1
    assert fl.NOTICE_MARKER in third[-1], 'notice must ride the newest tool result'


def test_slow_toolless_first_iteration_never_mutates_the_previous_turn(clock, provider):
    """The v1 defect: a tool-less iteration past 80% reached back into history."""
    history = [
        {'role': 'user', 'content': 'earlier question'},
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'old1', 'type': 'function',
             'function': {'name': 'todo', 'arguments': json.dumps(TODO)}}]},
        {'role': 'tool', 'tool_call_id': 'old1', 'content': 'OLD RESULT'},
        {'role': 'assistant', 'content': 'earlier answer'},
    ]
    old_tool = history[2]
    snapshot = json.dumps(history, sort_keys=True)
    agent = make_agent(provider)

    def before(n, req):
        if n == 1:
            clock.advance(250)  # the first model call is slow ...

    provider.before = before
    provider.queue[:] = [text(''), text('answer')]  # ... and returns nothing: a retry

    agent.run_conversation('new question', conversation_history=history, task_id='st')

    assert json.dumps(history, sort_keys=True) == snapshot, 'caller-owned history mutated'
    assert old_tool['content'] == 'OLD RESULT'
    for request in provider.captured:
        olds = [t for t in tool_texts(request) if t.startswith('OLD RESULT')]
        assert all(fl.NOTICE_MARKER not in t for t in olds)


def test_notice_does_not_mutate_the_caller_owned_tool_dict(clock, provider):
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 250)
    provider.queue[:] = [tool('todo', TODO, 'c1'), text('ok')]
    seen: list[dict] = []
    original = agent._execute_tool_calls

    def spying(assistant_message, messages, *a, **k):
        out = original(assistant_message, messages, *a, **k)
        seen.append(messages[-1])  # the executor's own tool message object
        seen.append(dict(messages[-1]))
        return out

    agent._execute_tool_calls = spying
    agent.run_conversation('job', conversation_history=[], task_id='cp')

    original_dict, content_at_creation = seen[0], seen[1]['content']
    assert original_dict['content'] == content_at_creation, \
        'notice mutated the executor-created dict in place'
    assert any(fl.NOTICE_MARKER in t for t in tool_texts(provider.captured[1]))


# ── provenance and episode inheritance ─────────────────────────────────────

def test_internal_notification_turn_inherits_the_episode(clock, provider, tmp_path):
    agent = make_agent(provider)
    target = tmp_path / 'late.txt'
    provider.queue[:] = [text('first answer')]
    agent.run_conversation('big job', conversation_history=[], task_id='e1')
    started = agent._fg_episode.started_at
    clock.advance(400)  # the helper finishes long after the human's 5 minutes

    provider.captured.clear()
    provider.queue[:] = [
        tool('write_file', {'path': str(target), 'content': 'x'}, 'w1'),
        text('integrated'),
    ]
    agent.run_conversation(
        'helper finished', conversation_history=[], task_id='e2',
        persist_user_display_kind='internal_notification')

    assert agent._fg_episode.started_at == started, 'internal turn minted a new clock'
    assert not target.exists(), 'finish mode must be inherited, write_file refused'
    assert any(fl.BLOCK_CODE in t for t in tool_texts(provider.captured[-1]))


def test_internal_notification_turn_with_time_left_does_not_reset_the_clock(clock, provider):
    agent = make_agent(provider)
    provider.queue[:] = [text('first answer')]
    agent.run_conversation('big job', conversation_history=[], task_id='e3')
    started = agent._fg_episode.started_at
    clock.advance(100)
    provider.queue[:] = [text('integrated')]

    agent.run_conversation('helper finished', conversation_history=[], task_id='e4',
                           persist_user_display_kind='internal_notification')

    assert agent._fg_episode.started_at == started
    clock.advance(150)  # 250 s into the original episode
    fl.begin_turn(agent, 'internal_notification')  # a third wake-up
    assert fl.remaining_seconds(agent) == pytest.approx(50)


def test_a_queued_human_message_gets_a_fresh_clock(clock, provider, tmp_path):
    agent = make_agent(provider)
    target = tmp_path / 'ok.txt'
    provider.queue[:] = [text('first')]
    agent.run_conversation('one', conversation_history=[], task_id='q1')
    clock.advance(10_000)

    provider.queue[:] = [tool('write_file', {'path': str(target), 'content': 'x'}, 'w1'),
                         text('written')]
    agent.run_conversation('two', conversation_history=[], task_id='q2')  # a human turn

    assert target.read_text() == 'x'


def test_a_fresh_agent_woken_by_an_internal_notification_starts_in_finish_mode(
        clock, provider, tmp_path):
    """Restart wake-up: no episode state must never mint a fresh budget, but
    reads and the final text still work so the helper's result integrates."""
    agent = make_agent(provider)
    readable = tmp_path / 'result.txt'
    readable.write_text('helper output')
    blocked = tmp_path / 'blocked.txt'
    provider.queue[:] = [
        tool('write_file', {'path': str(blocked), 'content': 'x'}, 'w1'),
        tool('read_file', {'path': str(readable)}, 'r1'),
        text('Here is what the helper found: helper output'),
    ]

    result = agent.run_conversation(
        'a helper finished', conversation_history=[], task_id='wake',
        persist_user_display_kind='internal_notification')

    assert result['final_response'].startswith('Here is what the helper found')
    assert not blocked.exists()
    joined = '\n'.join(t for r in provider.captured for t in tool_texts(r))
    assert fl.BLOCK_CODE in joined
    assert 'helper output' in joined, 'read_file must stay open in finish mode'
    assert result['turn_exit_reason'].startswith('text_response')


def test_steer_does_not_reset_the_episode(clock, provider, tmp_path):
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 100)
    target = tmp_path / 'late.txt'
    provider.queue[:] = [
        tool('todo', TODO, 'c1'), tool('todo', TODO, 'c2'), tool('todo', TODO, 'c3'),
        tool('write_file', {'path': str(target), 'content': 'x'}, 'c4'),
        text('done'),
    ]
    seen = {}

    def before(n, req):
        if n == 1:
            seen['started'] = agent._fg_episode.started_at
        if n == 2:
            agent.steer('please also check x')

    provider.before = before
    agent.run_conversation('job', conversation_history=[], task_id='sr')

    assert agent._fg_episode.started_at == seen['started']
    assert not target.exists()


# ── API server (no async delivery): not limited at all ─────────────────────

def test_a_turn_on_a_surface_without_async_delivery_is_not_limited(clock, provider, tmp_path):
    """Fix round 2: the limit is decided once at begin_turn. Where a detached
    helper cannot be delivered (API server, no wake id) there is no notice, no
    refusal and no hard stop -- today's behaviour."""
    import gateway.session_context as sc

    sc.declare_stateless_channel()
    try:
        agent = make_agent(provider, platform='api_server')
        target = tmp_path / 'late.txt'
        provider.queue[:] = [
            tool('todo', TODO, 'c1'),
            tool('write_file', {'path': str(target), 'content': 'x'}, 'c2'),
            tool('todo', TODO, 'c3'), tool('todo', TODO, 'c4'), tool('todo', TODO, 'c5'),
            tool('todo', TODO, 'c6'), text('all done'),
        ]
        provider.before = lambda n, req: clock.advance(1000)
        result = agent.run_conversation('big job', conversation_history=[], task_id='api')
    finally:
        sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)

    assert result['final_response'] == 'all done'
    assert result['turn_exit_reason'].startswith('text_response')
    assert target.read_text() == 'x', 'write_file must not be refused'
    assert len(provider.captured) == 7
    assert all(fl.NOTICE_MARKER not in json.dumps(r) for r in provider.captured)
    assert all(fl.BLOCK_CODE not in json.dumps(r) for r in provider.captured)
    assert agent._fg_episode is not None and agent._fg_episode.enabled is False
