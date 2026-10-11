"""Verification episode for a helper's own completion turn (C1.5 B2).

``delegation.verify_time_limit_seconds`` (default 120; 0/null/false = off): the
completion turn of a hand-off THIS agent instance dispatched gets a fresh,
bounded episode so the root can run a short check before it answers.  The
grant keys on trusted completion metadata (``completion_delegation_ids`` from
the gateway), never on notification text or on session identity, is consumed
once per dispatch (durably, across restart), and leaves everything else on
today's start-expired path.

Real ``AIAgent.run_conversation`` against a local scripted provider, the real
async-delegation registry / durable ledger in a scratch ``CLOVER_HOME``
(``delegate_task`` dispatches for real; only the child's own model run is
stubbed) and a fake monotonic clock.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Any, Callable, Optional
from unittest.mock import MagicMock

import pytest
import yaml

from agent import foreground_time_limit as fl
from tools import async_delegation as ad
from tools.process_registry import process_registry


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


def _write_config(foreground: Any = 300, verify: Any = 'unset') -> None:
    from clover_constants import get_clover_home

    from clover_cli.config_defaults import DEFAULT_CONFIG

    delegation: dict[str, Any] = {'foreground_time_limit_seconds': foreground}
    if verify != 'unset':
        delegation['verify_time_limit_seconds'] = verify
    (get_clover_home() / 'config.yaml').write_text(yaml.safe_dump({
        '_config_version': DEFAULT_CONFIG['_config_version'],
        'delegation': delegation,
    }))


@pytest.fixture(autouse=True)
def _defaults():
    _write_config(300)  # verify limit left at its default (120)


@pytest.fixture(autouse=True)
def _clean_async_state():
    def drain():
        while not process_registry.completion_queue.empty():
            process_registry.completion_queue.get_nowait()

    ad._reset_for_tests()
    drain()
    yield
    ad._reset_for_tests()
    drain()


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
    """Scripted OpenAI-compatible endpoint (non-streaming and SSE)."""

    def __init__(self) -> None:
        self.captured: list[dict] = []
        self.queue: list[dict] = []
        self.before: Optional[Callable[[int, dict], None]] = None
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                req = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                if 'messages' not in req:
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


def advance_per_tool_round(agent, clock: FakeClock, seconds: float) -> None:
    original = agent._execute_tool_calls

    def advancing(*a, **k):
        out = original(*a, **k)
        clock.advance(seconds)
        return out

    agent._execute_tool_calls = advancing


def tool_texts(request: dict) -> list[str]:
    return [m['content'] for m in request.get('messages', [])
            if m.get('role') == 'tool' and isinstance(m.get('content'), str)]


def all_tool_text(provider: Provider) -> str:
    return '\n'.join(t for r in provider.captured for t in tool_texts(r))


INTERNAL = 'internal_notification'


@pytest.fixture
def stub_children(monkeypatch):
    """Real ``delegate_task`` dispatch; only the child's own model run is faked."""
    import tools.delegate_tool as dt

    child = MagicMock()
    child._delegate_role = 'leaf'
    child._subagent_id = 'sub-1'
    creds = {'model': 'm', 'provider': None, 'base_url': None, 'api_key': None,
             'api_mode': None, 'command': None, 'args': None}

    def run_child(task_index, goal, child=None, parent_agent=None, **kw):
        return {'task_index': task_index, 'status': 'completed',
                'summary': f'done: {goal}', 'api_calls': 1,
                'duration_seconds': 0.1, 'model': 'm', 'exit_reason': 'completed'}

    monkeypatch.setattr(dt, '_build_child_agent', lambda **kw: child)
    monkeypatch.setattr(dt, '_run_single_child', run_child)
    monkeypatch.setattr(dt, '_resolve_delegation_credentials', lambda *a, **k: creds)


def hand_off(agent, provider: Provider, task_id: str = 'h1') -> str:
    """A human turn in which the agent dispatches a real background helper."""
    provider.queue[:] = [
        tool('delegate_task', {'goal': 'check the numbers', 'background': True}, 'dg'),
        text('A helper is on it and will report back.'),
    ]
    agent.run_conversation('big job', conversation_history=[], task_id=task_id)
    found = re.search(r'"delegation_id":\s*"([^"]+)"', all_tool_text(provider))
    assert found, 'delegate_task did not dispatch a background helper'
    provider.captured.clear()
    return found.group(1)


def completion_turn(agent, ids, task_id: str = 'c1', text_: str = 'helper finished'):
    return agent.run_conversation(
        text_, conversation_history=[], task_id=task_id,
        persist_user_display_kind=INTERNAL, completion_delegation_ids=ids)


def check_script(provider: Provider, marker, second_marker=None) -> None:
    """The model runs one terminal check, then (after the window) tries a second."""
    queue = [tool('terminal', {'command': f'touch {marker}'}, 't1')]
    if second_marker is not None:
        queue.append(tool('terminal', {'command': f'touch {second_marker}'}, 't2'))
    queue.append(text('Checked: the numbers are right.'))
    provider.queue[:] = queue


# ── the grant ──────────────────────────────────────────────────────────────

def test_own_handoff_completion_runs_one_check_then_finish_mode(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    clock.advance(400)  # the helper took longer than the human's 5 minutes
    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'
    check_script(provider, first, second)
    advance_per_tool_round(agent, clock, 130)  # the check "takes" 130 s

    result = completion_turn(agent, [did])

    assert first.exists(), 'the check admitted inside the window must run'
    assert not second.exists(), 'after the window the turn is in finish mode'
    assert fl.BLOCK_CODE in all_tool_text(provider)
    assert result['final_response'] == 'Checked: the numbers are right.'
    assert agent._fg_episode.limit == 120


def test_without_a_grant_the_same_completion_turn_is_start_expired(
        clock, provider, stub_children, tmp_path):
    """Control for the test above: no trusted ids -> today's behaviour."""
    agent = make_agent(provider)
    hand_off(agent, provider)
    clock.advance(400)
    first = tmp_path / 'first.txt'
    check_script(provider, first)

    agent.run_conversation('helper finished', conversation_history=[], task_id='c0',
                           persist_user_display_kind=INTERNAL)

    assert not first.exists()
    assert fl.BLOCK_CODE in all_tool_text(provider)


def test_second_completion_for_the_same_dispatch_gets_no_new_allowance(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    clock.advance(400)
    completion_turn(agent, [did], task_id='c1')
    clock.advance(600)
    again = tmp_path / 'again.txt'
    check_script(provider, again)
    provider.captured.clear()

    completion_turn(agent, [did], task_id='c2')

    assert not again.exists()
    assert fl.BLOCK_CODE in all_tool_text(provider)


def test_the_allowance_stays_consumed_across_a_restart(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    clock.advance(400)
    completion_turn(agent, [did], task_id='c1')  # consumes it

    ad._reset_for_tests()  # the process restarts: memory gone, ledger stays
    reborn = make_agent(provider)
    fl.note_own_dispatch(reborn, did)  # even an instance that claims the id
    clock.advance(600)
    late = tmp_path / 'late.txt'
    check_script(provider, late)
    provider.captured.clear()

    completion_turn(reborn, [did], task_id='c3')

    assert not late.exists()
    assert fl.BLOCK_CODE in all_tool_text(provider)


def test_a_restarted_instance_that_never_dispatched_gets_nothing(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    ad._reset_for_tests()
    clock.advance(400)
    reborn = make_agent(provider)  # previous generation's dispatch
    late = tmp_path / 'late.txt'
    check_script(provider, late)

    completion_turn(reborn, [did], task_id='c4')

    assert not late.exists()
    assert fl.BLOCK_CODE in all_tool_text(provider)


# ── what must NOT earn the grant ───────────────────────────────────────────

def test_foreign_cron_and_mismatched_ids_keep_todays_behaviour(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    own = hand_off(agent, provider)
    cron = ad.dispatch_async_delegation(
        goal='nightly', context=None, toolsets=None, role='leaf', model=None,
        session_key='cron:nightly',
        runner=lambda: {'status': 'completed', 'summary': 'ok'})['delegation_id']
    clock.advance(400)

    for n, ids in enumerate((['deleg_never_dispatched_here'], [cron], [own + 'x'], [])):
        target = tmp_path / f'attempt{n}.txt'
        check_script(provider, target)
        provider.captured.clear()
        completion_turn(agent, ids, task_id=f'm{n}')
        assert not target.exists(), f'ids={ids!r} must not open a window'
        assert fl.BLOCK_CODE in all_tool_text(provider)

    # the real owner's own id was never spent by the attempts above
    mine = tmp_path / 'mine.txt'
    check_script(provider, mine)
    provider.captured.clear()
    clock.advance(10)
    completion_turn(agent, [own], task_id='mine')
    assert mine.exists()


def test_notification_text_naming_the_id_is_not_trusted(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    clock.advance(400)
    target = tmp_path / 'text.txt'
    check_script(provider, target)

    agent.run_conversation(
        f'[ASYNC DELEGATION COMPLETE delegation_id={did}] done',
        conversation_history=[], task_id='txt', persist_user_display_kind=INTERNAL)

    assert not target.exists()


def test_the_same_session_id_alone_does_not_grant(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    clock.advance(400)
    sibling = make_agent(provider, session_id=agent.session_id)  # same session, other instance
    target = tmp_path / 'session.txt'
    check_script(provider, target)

    completion_turn(sibling, [did], task_id='sess')

    assert not target.exists()


def test_ids_on_a_human_turn_are_ignored(clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    clock.advance(400)
    provider.queue[:] = [text('hello')]

    agent.run_conversation('hi again', conversation_history=[], task_id='hum',
                           completion_delegation_ids=[did])

    assert agent._fg_episode.limit == 300  # the human's own limit, not 120
    assert ad.claim_verification_allowance(did) is True, 'a human turn must not consume it'


def test_a_still_running_human_clock_is_not_replaced(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    started = agent._fg_episode.started_at
    clock.advance(100)
    provider.queue[:] = [text('integrated')]

    completion_turn(agent, [did], task_id='live')

    assert agent._fg_episode.started_at == started
    assert agent._fg_episode.limit == 300
    assert ad.claim_verification_allowance(did) is True, 'nothing was spent'


# ── limits / switches ──────────────────────────────────────────────────────

@pytest.mark.parametrize('off', [0, None, False, 'nope'])
def test_verify_limit_off_means_no_episode(
        clock, provider, stub_children, tmp_path, off):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    _write_config(300, off)
    clock.advance(400)
    target = tmp_path / 'off.txt'
    check_script(provider, target)

    completion_turn(agent, [did], task_id='off')

    assert not target.exists()
    assert fl.BLOCK_CODE in all_tool_text(provider)


def test_foreground_limit_off_disables_the_verification_episode_too(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    _write_config(0, 120)
    clock.advance(400)
    target = tmp_path / 'fgoff.txt'
    check_script(provider, target)

    completion_turn(agent, [did], task_id='fgoff')

    assert agent._fg_episode is None
    assert fl.forces_background(agent) is False
    assert target.exists(), 'with the limit off nothing is refused'


def test_verify_limit_is_read_from_config(clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    _write_config(300, 45)
    clock.advance(400)
    provider.queue[:] = [text('ok')]

    completion_turn(agent, [did], task_id='cfg')

    assert agent._fg_episode.limit == 45


def test_default_and_normalisation_of_the_verify_limit(monkeypatch):
    assert fl.DEFAULT_VERIFY_SECONDS == 120.0
    assert fl.load_verify_seconds() == 120.0  # default config, key absent
    _write_config(300, 'unset')
    assert fl.load_verify_seconds() == 120.0
    for off in (0, None, False, True, '', 'x', -5):
        _write_config(300, off)
        assert fl.load_verify_seconds() is None, off
    _write_config(300, 90)
    assert fl.load_verify_seconds() == 90.0


# ── helpers spawned in the episode ─────────────────────────────────────────

def test_helpers_spawned_in_the_episode_are_background_even_after_expiry(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    clock.advance(400)
    provider.queue[:] = [
        tool('delegate_task', {'goal': 'deeper check', 'background': False}, 'e1'),
        tool('todo', {'todos': [{'id': '1', 'content': 'x', 'status': 'pending'}]}, 'e2'),
        tool('delegate_task', {'goal': 'one more', 'background': False}, 'e3'),
        text('A helper is checking further and will report back.'),
    ]
    seen: list[tuple[bool, bool]] = []  # (episode expired, forced background)
    original = agent._execute_tool_calls

    def round_(*a, **k):
        out = original(*a, **k)
        seen.append((agent._fg_episode.expired(), fl.forces_background(agent)))
        clock.advance(70)  # rounds end at 70 s, 140 s (expired), 210 s
        return out

    agent._execute_tool_calls = round_

    result = completion_turn(agent, [did], task_id='bg')

    results = all_tool_text(provider)
    assert results.count('"mode": "background"') >= 2, results
    assert any(expired for expired, _ in seen), 'the episode must have run out'
    assert all(forced for _, forced in seen), seen  # background before AND after expiry
    assert result['final_response'].startswith('A helper is checking further')


def test_no_limit_or_timeout_wording_reaches_the_user_reply(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    clock.advance(400)
    target = tmp_path / 'late.txt'
    provider.queue[:] = [
        tool('terminal', {'command': f'touch {target}'}, 't1'),
        text('Everything checks out.'),
    ]
    advance_per_tool_round(agent, clock, 130)

    result = completion_turn(agent, [did], task_id='quiet')

    assert result['final_response'] == 'Everything checks out.'
    # any model-facing refusal text is a marked Clover note that never asks to
    # be hidden (the keep-quiet rule is in the static prompt)
    for t in tool_texts(provider.captured[-1]):
        if fl.BLOCK_CODE in t:
            assert '[Clover runtime]' in t
            assert 'do not mention' not in t.lower()


# ── human clocks and cache stability ───────────────────────────────────────

def test_a_human_message_after_the_episode_gets_its_own_fresh_clock(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    clock.advance(400)
    provider.queue[:] = [text('checked')]
    completion_turn(agent, [did], task_id='ep')
    clock.advance(50)  # inside / around the verification window
    target = tmp_path / 'human.txt'
    provider.queue[:] = [tool('terminal', {'command': f'touch {target}'}, 'h1'), text('done')]

    agent.run_conversation('next request', conversation_history=[], task_id='hum2')

    assert agent._fg_episode.limit == 300
    assert agent._fg_episode.started_at == clock.t
    assert target.exists()


def test_system_prompt_and_tool_schemas_are_identical_in_the_episode(
        clock, provider, stub_children):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    provider.queue[:] = [text('first')]
    agent.run_conversation('anything', conversation_history=[], task_id='p1')
    human_req = provider.captured[-1]
    clock.advance(400)
    provider.queue[:] = [text('second')]

    completion_turn(agent, [did], task_id='p2')
    episode_req = provider.captured[-1]

    def system(req):
        return next(m['content'] for m in req['messages'] if m['role'] == 'system')

    assert system(human_req) == system(episode_req)
    assert human_req.get('tools') == episode_req.get('tools')


# ── batch children ─────────────────────────────────────────────────────────

def test_batch_child_completions_share_one_allowance_per_dispatch(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    batch = hand_off(agent, provider)
    clock.advance(400)
    first, second = tmp_path / 'c0.txt', tmp_path / 'c1.txt'

    check_script(provider, first)
    completion_turn(agent, [f'{batch}:child:0', batch], task_id='b0')
    assert first.exists()

    clock.advance(600)
    check_script(provider, second)
    provider.captured.clear()
    completion_turn(agent, [f'{batch}:child:1', batch], task_id='b1')
    assert not second.exists()
    assert fl.BLOCK_CODE in all_tool_text(provider)


# ── the durable claim itself ───────────────────────────────────────────────

def test_claim_is_once_only_and_fails_closed_for_unknown_ids(provider, stub_children, clock):
    agent = make_agent(provider)
    did = hand_off(agent, provider)

    assert ad.claim_verification_allowance(did) is True
    assert ad.claim_verification_allowance(did) is False
    assert ad.claim_verification_allowance('deleg_missing') is False
    assert ad.claim_verification_allowance('') is False


# ── a completion queued behind a busy turn keeps its identity ──────────────

async def run_with_queued(monkeypatch, tmp_path, clock, provider, agent, queued, *,
                          first_turn_seconds: float = 310):
    """Real ``GatewayRunner._run_agent``: a human turn runs, ``queued`` waits in
    the adapter's pending slot and is drained in-band afterwards.

    Returns one record per ``run_conversation`` call: the kwargs the gateway
    passed, the episode the turn ran under and the persisted user row.
    """
    import gateway.run as gr
    import run_agent
    from gateway.config import Platform
    from gateway.session import SessionSource
    from tests.gateway.test_queued_native_image_session_key import (
        CaptureAdapter, _make_runner,
    )

    adapter = CaptureAdapter()
    runner = _make_runner(adapter)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='-1001', chat_type='group')
    key = 'agent:main:telegram:group:-1001'
    queued.source = source
    adapter._pending_messages[key] = queued
    monkeypatch.setattr(gr, '_clover_home', tmp_path)
    monkeypatch.setattr(gr, '_resolve_runtime_agent_kwargs', lambda: {'api_key': 'fake'})

    class ExistingAgent(run_agent.AIAgent):
        def __new__(cls, **kwargs):
            return agent

    monkeypatch.setattr(run_agent, 'AIAgent', ExistingAgent)

    calls: list[SimpleNamespace] = []
    original = agent.run_conversation

    def record(*args, **kwargs):
        result = original(*args, **kwargs)
        episode = agent._fg_episode
        user_rows = [m for m in result['messages'] if m.get('role') == 'user']
        calls.append(SimpleNamespace(
            kwargs=kwargs, started_at=episode.started_at, limit=episode.limit,
            verification=episode.verification, finish_at=episode.finish_at,
            user_row=user_rows[-1]))
        clock.advance(first_turn_seconds if len(calls) == 1 else 30)
        return result

    monkeypatch.setattr(agent, 'run_conversation', record)
    provider.queue[:] = [text('human answer'), text('checked')]
    await runner._run_agent(message='human request', context_prompt='', history=[],
                            source=source, session_id=agent.session_id, session_key=key)
    assert len(calls) == 2, calls
    return calls


def queued_completion(ids, *, text_: str = 'helper finished'):
    from gateway.platforms.base import MessageEvent

    return MessageEvent(
        text=text_, source=None, internal=True, message_id='completion-1',
        metadata={'completion_delegation_ids': list(ids)} if ids is not None else {})


@pytest.mark.asyncio
async def test_queued_own_completion_gets_the_window_and_internal_presentation(
        clock, provider, stub_children, monkeypatch, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)

    first, second = await run_with_queued(
        monkeypatch, tmp_path, clock, provider, agent, queued_completion([did]))

    assert first.verification is False and first.limit == 300
    assert second.kwargs.get('persist_user_display_kind') == INTERNAL
    assert tuple(second.kwargs.get('completion_delegation_ids')) == (did,)
    assert second.user_row.get('display_kind') == INTERNAL
    assert second.verification is True and second.limit == 120
    assert second.started_at == first.started_at + 310  # its own window, not the human's
    assert ad.claim_verification_allowance(did) is False, 'spent exactly once'


@pytest.mark.asyncio
@pytest.mark.parametrize('ids', [['deleg_foreign'], [], None], ids=['foreign', 'empty', 'text-only'])
async def test_queued_foreign_or_text_only_completion_inherits_without_a_fresh_clock(
        clock, provider, stub_children, monkeypatch, tmp_path, ids):
    agent = make_agent(provider)
    did = hand_off(agent, provider)

    first, second = await run_with_queued(
        monkeypatch, tmp_path, clock, provider, agent,
        queued_completion(ids, text_=f'helper finished delegation_id={did}'))

    assert second.kwargs.get('persist_user_display_kind') == INTERNAL
    assert second.user_row.get('display_kind') == INTERNAL
    assert second.verification is False
    assert second.started_at == first.started_at, 'an internal turn must not mint a human clock'
    assert second.limit == 300 and second.finish_at is not None  # spent -> finish mode
    assert ad.claim_verification_allowance(did) is True, 'the own allowance was never spent'


@pytest.mark.asyncio
@pytest.mark.parametrize('forged', [False, True], ids=['plain', 'forged-metadata'])
async def test_queued_genuine_human_message_keeps_a_fresh_human_clock(
        clock, provider, stub_children, monkeypatch, tmp_path, forged):
    from gateway.platforms.base import MessageEvent

    agent = make_agent(provider)
    did = hand_off(agent, provider)
    human = MessageEvent(
        text='and another thing', source=None, internal=False, message_id='human-2',
        metadata={'completion_delegation_ids': [did]} if forged else {})

    first, second = await run_with_queued(
        monkeypatch, tmp_path, clock, provider, agent, human)

    assert second.kwargs.get('persist_user_display_kind') is None
    assert second.kwargs.get('completion_delegation_ids') is None
    assert 'display_kind' not in second.user_row
    assert second.verification is False and second.limit == 300
    assert second.started_at == first.started_at + 310  # a fresh human clock
    assert second.finish_at is None
    assert ad.claim_verification_allowance(did) is True


# ── one allowance per window; the grant stays local to delivery ────────────

def test_several_coalesced_dispatches_spend_only_one_allowance(
        clock, provider, stub_children):
    agent = make_agent(provider)
    first = hand_off(agent, provider, task_id='h1')
    second = hand_off(agent, provider, task_id='h2')
    clock.advance(400)
    provider.queue[:] = [text('checked both')]

    completion_turn(agent, [first, second], task_id='co')

    assert agent._fg_episode.verification is True
    assert ad.claim_verification_allowance(first) is False, 'the matched id is spent'
    assert ad.claim_verification_allowance(second) is True, 'the other is left unspent'


def test_no_window_where_a_detached_helper_cannot_be_delivered(
        clock, provider, stub_children):
    import gateway.session_context as sc

    agent = make_agent(provider)
    did = hand_off(agent, provider)
    clock.advance(400)
    sc.declare_stateless_channel()  # the surface can no longer take a detached result
    try:
        provider.queue[:] = [text('ok')]
        completion_turn(agent, [did], task_id='nodeliv')
    finally:
        sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)

    assert agent._fg_episode.verification is False
    assert ad.claim_verification_allowance(did) is True, 'nothing was spent'
