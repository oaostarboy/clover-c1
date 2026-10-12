"""A turn that delivers a helper's result gets its own full foreground clock.

The Octavia incident (2026-10-12 13:04): the completion of a hand-off arrived
after the dispatching request had used its 300 s, inherited that spent clock,
and hit "the time limit is reached" 37 s in -- twice in a row.  A completion
turn (an ``internal_notification`` carrying the gateway's trusted
``completion_delegation_ids``) is the user-facing continuation of the work, so
it starts its OWN episode with the full limit, like a new human message.
Other internal turns (watches, wake-ups, no ids) still inherit.

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


def atomic_write_config(text: str) -> None:
    """Replace config.yaml in one step. ``write_text`` truncates first, and a
    leaked background thread (title generation) that loads the config in that
    gap sees a legacy file, migrates it and writes the defaults over the test's
    settings."""
    import os
    from clover_constants import get_clover_home

    path = get_clover_home() / 'config.yaml'
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text)
    os.replace(tmp, path)


def _write_config(foreground: Any = 300) -> None:
    from clover_constants import get_clover_home

    from clover_cli.config_defaults import DEFAULT_CONFIG

    delegation: dict[str, Any] = {'foreground_time_limit_seconds': foreground}
    atomic_write_config(yaml.safe_dump({
        '_config_version': DEFAULT_CONFIG['_config_version'],
        'delegation': delegation,
    }))


@pytest.fixture(autouse=True)
def _defaults():
    _write_config(300)


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


# ── the fresh clock ────────────────────────────────────────────────────────

@pytest.mark.parametrize('used', [299, 400, 1000])
def test_completion_turn_after_the_parent_used_its_clock_gets_a_fresh_full_limit(
        clock, provider, stub_children, tmp_path, used):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    clock.advance(used)  # the helper ran as long as the parent's episode (or longer)
    marks = [tmp_path / f'm{i}.txt' for i in range(3)]
    provider.queue[:] = [tool('terminal', {'command': f'touch {m}'}, f't{i}')
                         for i, m in enumerate(marks)] + [text('Checked: all right.')]
    advance_per_tool_round(agent, clock, 70)  # 3 rounds: 210 s, inside 300 s

    result = completion_turn(agent, [did])

    assert agent._fg_episode.limit == 300 and agent._fg_episode.completion is True
    assert agent._fg_episode.finish_at is None, 'not started in finish mode'
    assert all(m.exists() for m in marks), 'every check inside the full limit runs'
    assert fl.BLOCK_CODE not in all_tool_text(provider)
    assert result['final_response'] == 'Checked: all right.'


def test_the_completion_clock_expires_normally_after_its_own_full_limit(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    clock.advance(1000)
    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'
    check_script(provider, first, second)
    advance_per_tool_round(agent, clock, 310)  # the first check alone takes 310 s

    completion_turn(agent, [did])

    assert first.exists()
    assert not second.exists(), 'past ITS limit the turn is in finish mode'
    assert fl.BLOCK_CODE in all_tool_text(provider)


@pytest.mark.parametrize('ids', [['deleg_foreign'], ['deleg_a', 'deleg_b']],
                         ids=['foreign', 'several'])
def test_the_fresh_clock_needs_no_state_on_the_agent_instance(
        clock, provider, stub_children, tmp_path, ids):
    """Trusted metadata is the whole test: a rebuilt agent, a restart and
    another generation get the same fresh clock."""
    agent = make_agent(provider)
    hand_off(agent, provider)
    ad._reset_for_tests()
    reborn = make_agent(provider)  # never dispatched anything
    clock.advance(1000)
    target = tmp_path / 'reborn.txt'
    check_script(provider, target)

    completion_turn(reborn, ids, task_id='reborn')

    assert reborn._fg_episode.completion is True and reborn._fg_episode.limit == 300
    assert target.exists()


@pytest.mark.parametrize('ids', [[], None, '', ['', '  ']], ids=['empty', 'none', 'str', 'blank'])
def test_an_internal_turn_without_trusted_ids_still_inherits(
        clock, provider, stub_children, tmp_path, ids):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    clock.advance(1000)
    target = tmp_path / 'inherit.txt'
    check_script(provider, target)

    agent.run_conversation(
        f'[ASYNC DELEGATION COMPLETE delegation_id={did}] done',  # text is never trusted
        conversation_history=[], task_id='txt', persist_user_display_kind=INTERNAL,
        completion_delegation_ids=ids)

    assert agent._fg_episode.completion is False
    assert agent._fg_episode.finish_at is not None, 'spent -> finish mode from the start'
    assert not target.exists()
    assert fl.BLOCK_CODE in all_tool_text(provider)


def test_ids_on_a_human_turn_are_ignored(clock, provider, stub_children):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    clock.advance(400)
    provider.queue[:] = [text('hello')]

    agent.run_conversation('hi again', conversation_history=[], task_id='hum',
                           completion_delegation_ids=[did])

    assert agent._fg_episode.limit == 300 and agent._fg_episode.completion is False


# ── a human message keeps its clock rules ──────────────────────────────────

def test_a_human_message_inside_an_episode_starts_its_own_clock(
        clock, provider, stub_children):
    agent = make_agent(provider)
    hand_off(agent, provider)
    first = agent._fg_episode.started_at
    clock.advance(100)
    provider.queue[:] = [text('ok')]

    agent.run_conversation('next request', conversation_history=[], task_id='hum2')

    assert agent._fg_episode.started_at == first + 100
    assert agent._fg_episode.limit == 300 and agent._fg_episode.completion is False


def test_a_human_message_after_a_completion_gets_its_own_fresh_clock(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    clock.advance(400)
    provider.queue[:] = [text('checked')]
    completion_turn(agent, [did], task_id='ep')
    clock.advance(50)
    target = tmp_path / 'human.txt'
    provider.queue[:] = [tool('terminal', {'command': f'touch {target}'}, 'h1'), text('done')]

    agent.run_conversation('next request', conversation_history=[], task_id='hum3')

    assert agent._fg_episode.limit == 300 and agent._fg_episode.completion is False
    assert agent._fg_episode.started_at == clock.t
    assert target.exists()


def test_steer_style_midturn_input_does_not_reset_a_running_episode(clock, provider):
    """Only a turn BEGINNING resets the clock; a begin_turn-free redirect inside
    the running turn reads the same episode."""
    agent = make_agent(provider)
    fl.begin_turn(agent)
    started = agent._fg_episode.started_at
    clock.advance(120)

    assert fl.remaining_seconds(agent) == pytest.approx(180)
    assert agent._fg_episode.started_at == started


# ── limits / switches / scope ──────────────────────────────────────────────

def test_foreground_limit_off_means_no_episode_for_a_completion_turn(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    _write_config(0)
    clock.advance(400)
    target = tmp_path / 'fgoff.txt'
    check_script(provider, target)

    completion_turn(agent, [did], task_id='fgoff')

    assert agent._fg_episode is None
    assert fl.forces_background(agent) is False
    assert target.exists(), 'with the limit off nothing is refused'


def test_no_clock_where_a_detached_helper_cannot_be_delivered(
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

    assert agent._fg_episode.enabled is False


def test_the_removed_verify_setting_and_claim_are_gone():
    assert not hasattr(fl, 'load_verify_seconds')
    assert not hasattr(ad, 'claim_verification_allowance')


# ── helpers spawned in the episode ─────────────────────────────────────────

def test_helpers_spawned_in_the_episode_are_background_even_after_expiry(
        clock, provider, stub_children, tmp_path):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    clock.advance(400)
    # Rounds run the episode out; the dispatch comes after expiry.  A dispatch
    # on a completion turn is the hand-back of the integration: the runtime
    # acknowledges it and ends the turn (agent.work_lifecycle).
    provider.queue[:] = [
        tool('todo', {'todos': [{'id': '1', 'content': 'x', 'status': 'pending'}]}, 'e1'),
        tool('todo', {'todos': [{'id': '1', 'content': 'x', 'status': 'in_progress'}]}, 'e2'),
        tool('delegate_task', {'goal': 'deeper check', 'background': False}, 'e3'),
    ]
    seen: list[tuple[bool, bool]] = []  # (episode expired, forced background)
    original = agent._execute_tool_calls

    def round_(*a, **k):
        out = original(*a, **k)
        seen.append((agent._fg_episode.expired(), fl.forces_background(agent)))
        clock.advance(160)  # rounds end at 160 s, 320 s (expired), 480 s
        return out

    agent._execute_tool_calls = round_

    result = completion_turn(agent, [did], task_id='bg')

    results = '\n'.join(str(m.get('content')) for m in result['messages']
                        if m.get('role') == 'tool')
    assert results.count('"mode": "background"') >= 1, results
    assert seen[-1][0], 'the episode must have run out before the dispatch'
    assert all(forced for _, forced in seen), seen  # background before AND after expiry
    assert re.match(r'^\U0001f504 ', result['final_response'])
    assert result['turn_exit_reason'] == 'delegation_handoff'


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
    for t in tool_texts(provider.captured[-1]):
        if fl.BLOCK_CODE in t:
            assert '(Clover runtime)' in t
            assert 'do not mention' not in t.lower()


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
            completion=episode.completion, finish_at=episode.finish_at,
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
@pytest.mark.parametrize('foreign', [False, True], ids=['own', 'foreign'])
async def test_queued_completion_gets_its_own_clock_and_internal_presentation(
        clock, provider, stub_children, monkeypatch, tmp_path, foreign):
    agent = make_agent(provider)
    did = hand_off(agent, provider)
    ids = ['deleg_foreign'] if foreign else [did]

    first, second = await run_with_queued(
        monkeypatch, tmp_path, clock, provider, agent, queued_completion(ids))

    assert first.completion is False and first.limit == 300
    assert second.kwargs.get('persist_user_display_kind') == INTERNAL
    assert tuple(second.kwargs.get('completion_delegation_ids')) == tuple(ids)
    assert second.user_row.get('display_kind') == INTERNAL
    assert second.completion is True and second.limit == 300
    assert second.started_at == first.started_at + 310  # its own clock, not the human's
    assert second.finish_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize('ids', [[], None], ids=['empty', 'text-only'])
async def test_queued_text_only_notification_inherits_without_a_fresh_clock(
        clock, provider, stub_children, monkeypatch, tmp_path, ids):
    agent = make_agent(provider)
    did = hand_off(agent, provider)

    first, second = await run_with_queued(
        monkeypatch, tmp_path, clock, provider, agent,
        queued_completion(ids, text_=f'helper finished delegation_id={did}'))

    assert second.kwargs.get('persist_user_display_kind') == INTERNAL
    assert second.completion is False
    assert second.started_at == first.started_at, 'an untrusted internal turn must not mint a clock'
    assert second.finish_at is not None  # spent -> finish mode


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
    assert second.completion is False and second.limit == 300
    assert second.started_at == first.started_at + 310  # a fresh human clock
    assert second.finish_at is None
