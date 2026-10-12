"""Replay: a forced "direct" verdict and 30 scripted work calls, through the real
``AIAgent.run_conversation`` turn machinery (scripted provider, real executor,
real ``delegate_task`` handler and child pool).

The hand-off must happen at work call 25, with exactly one dispatch, an
acknowledgment that reads like any other hand-off, and no word about limits.
Run with ``-s`` to print the replay.
"""
from __future__ import annotations

import json
import re

import pytest

from tests.agent.test_delegation_router import aux, verdict_json  # noqa: F401
from tests.agent.test_foreground_time_limit_loop import (  # noqa: F401
    _limit_on,
    clock,
    make_agent,
    provider,
    text,
    tool,
)
from tests.agent.test_router_backstop import _clean_registry, _wait_started, dispatches  # noqa: F401
from tests.agent.test_router_e2e import _roles_ok, _tool_texts, _wire_users, children  # noqa: F401

pytestmark = pytest.mark.real_delegation_router

REQUEST = 'Email Julia the AutoHDR extension with the instructions I gave you.'


def _run(provider, aux, tmp_path, *, reason='one send after a quick look'):
    aux.replies[:] = [verdict_json('direct', reason=reason)]
    provider.queue[:] = [
        tool('write_file', {'path': str(tmp_path / f'{i}.txt'), 'content': str(i)}, f'c{i}')
        for i in range(1, 31)
    ]  # no 31st reply needed: the runtime ends the turn itself
    agent = make_agent(provider, session_id='replay-direct')
    return agent.run_conversation(REQUEST, conversation_history=[], task_id='replay')


def test_thirty_scripted_work_calls_hand_off_at_the_25th_with_one_dispatch(
        provider, aux, tmp_path, children, dispatches):
    result = _run(provider, aux, tmp_path)

    ran = sorted(int(p.stem) for p in tmp_path.glob('*.txt'))
    handed = [m['content'] for m in result['messages']
              if m.get('role') == 'tool' and 'Handed off to helpers' in m['content']]
    print('\n--- direct-backstop replay ---')
    print('verdict:        direct (forced)')
    print(f'scripted calls: 30   model requests made: {len(provider.captured)}')
    print(f'calls that ran: {len(ran)} ({ran[0]}..{ran[-1]})   first refused call: #{len(ran) + 1}')
    print(f'dispatches:     {len(dispatches)}   helpers started: {len(children)}')
    print(f'turn exit:      {result["turn_exit_reason"]}   work_status: {result["work_status"]}')
    print(f'ack:            {result["final_response"]!r}')
    print('hand-off result:', handed[0][:200] if handed else None)

    assert ran == list(range(1, 25 + 1)), 'calls 1..25 ran, the 26th was the next admission'
    assert len(dispatches) == 1
    assert len(dispatches[0]['tasks']) == 1
    _wait_started(children, 1)
    assert len(children) == 1
    assert result['turn_exit_reason'] == 'delegation_handoff'
    assert result['work_status'] == 'dispatch_ack'
    assert len(provider.captured) == 26, 'the model was asked 26 times; no 27th'
    assert len(handed) == 1
    _roles_ok(result['messages'])


def test_the_replay_ack_is_a_normal_handoff_and_the_helper_gets_the_request(
        provider, aux, tmp_path, children, dispatches):
    result = _run(provider, aux, tmp_path)

    ack = result['final_response']
    assert re.match(r'^\U0001f504 ', ack) and 'taking over the rest of your request' in ack
    assert REQUEST not in ack
    assert not re.search(r'limit|timeout|timed out|backstop|budget|25|180', ack.lower())
    _wait_started(children, 1)
    blob = children[0]['system'] + str(children[0]['user'])
    assert REQUEST in blob and 'Findings already gathered' in blob


def test_the_direct_note_on_the_wire_is_the_neutral_one_and_stable(
        provider, aux, tmp_path, children, dispatches):
    _run(provider, aux, tmp_path)

    first_user = _wire_users(provider.captured[0])[-1]
    assert '(Clover runtime) work plan: direct (one send after a quick look). Handle it here.' in first_user or \
        'work plan: direct (one send after a quick look). Handle it here.' in first_user
    assert 'no helpers' not in first_user.lower()
    # the same user message carries the same note on every later request (cache-stable)
    assert all(_wire_users(r)[-1] == first_user for r in provider.captured[1:])


def test_with_the_flag_off_all_thirty_calls_run(provider, aux, tmp_path, children, dispatches):
    import yaml
    from clover_constants import get_clover_home

    from clover_cli.config_defaults import DEFAULT_CONFIG

    (get_clover_home() / 'config.yaml').write_text(yaml.safe_dump({
        '_config_version': DEFAULT_CONFIG['_config_version'],
        'delegation': {'foreground_time_limit_seconds': 300,
                       'router': {'direct_backstop': {'enabled': False}}},
    }))
    aux.replies[:] = [verdict_json('direct', reason='r')]
    provider.queue[:] = [
        tool('write_file', {'path': str(tmp_path / f'{i}.txt'), 'content': 'x'}, f'c{i}')
        for i in range(1, 31)
    ] + [text('all thirty are written')]
    agent = make_agent(provider, session_id='replay-off')

    result = agent.run_conversation(REQUEST, conversation_history=[], task_id='replay-off')

    assert result['final_response'] == 'all thirty are written'
    assert len(list(tmp_path.glob('*.txt'))) == 30
    assert dispatches == [] and children == []
    assert not any('Handed off' in t for r in provider.captured for t in _tool_texts(r))
