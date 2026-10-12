"""Replay: a forced "direct" verdict, 26 scripted work calls with distinct long
results and two earlier conversation turns, through the real
``AIAgent.run_conversation`` machinery (scripted provider, real executor, real
``delegate_task`` handler and child pool).

At the hand-off the notes file must hold all 26 results and the earlier turns,
the helper's context must point at it, and the parent must open in its own
voice (one short tools-off call on its own model) before the card.  Run with
``-s`` to print the first lines of the notes file and the helper context.
"""
from __future__ import annotations

import json
import os
import re

import pytest
import yaml

from tests.agent.test_delegation_router import aux, verdict_json  # noqa: F401
from tests.agent.test_foreground_time_limit_loop import (  # noqa: F401
    make_agent,
    provider,
    text,
    tool,
)
from tests.agent.test_router_backstop import _clean_registry, _wait_started, dispatches  # noqa: F401
from tests.agent.test_router_e2e import _roles_ok, _tool_texts, _wire_users, children  # noqa: F401

pytestmark = pytest.mark.real_delegation_router

REQUEST = 'Email the existing recipient the AutoHDR extension with the instructions I gave you.'
OPENER = "on it, I'm passing the rest of the email to a subagent, it's pulling the recipient and the instructions together"
BULLETS = ('- The existing recipient is Julia <julia@example.com>, from the HDR plugin thread.\n'
           '- All 26 source files were read; the instructions are in notes-07.\n'
           '- Still to do: write and send the email.')
HISTORY = [
    {'role': 'user', 'content': 'Who did I email about the HDR plugin last week?'},
    {'role': 'assistant', 'content': 'You emailed Julia <julia@example.com> on Tuesday about the HDR plugin.'},
]


def _files(tmp_path, n=26):
    out = []
    for i in range(1, n + 1):
        f = tmp_path / f'notes-{i:02d}.txt'
        f.write_text(f'NOTE-{i:02d}-START ' + (f'detail line {i}; ' * 60) + f' NOTE-{i:02d}-END')
        out.append(f)
    return out


def _run(provider, aux, tmp_path, opener=OPENER):
    from clover_constants import get_clover_home

    from clover_cli.config_defaults import DEFAULT_CONFIG

    # 26 work calls run before the safety net trips on the 27th
    (get_clover_home() / 'config.yaml').write_text(yaml.safe_dump({
        '_config_version': DEFAULT_CONFIG['_config_version'],
        'delegation': {'foreground_time_limit_seconds': 300,
                       'router': {'direct_backstop': {'enabled': True, 'work_calls': 26}}},
    }))
    files = _files(tmp_path, 27)
    aux.replies[:] = [verdict_json('direct', reason='one send after a quick look'), BULLETS]
    provider.queue[:] = [tool('read_file', {'path': str(f)}, f'c{i}') for i, f in enumerate(files, 1)]
    provider.queue.append(text(opener))  # the one short tools-off call after the hand-off
    agent = make_agent(provider, session_id='replay-notes')
    return agent.run_conversation(REQUEST, conversation_history=list(HISTORY), task_id='replay-notes')


def _notes_path(dispatch):
    context = dispatch['tasks'][0]['context']
    return context, [ln for ln in context.splitlines()
                     if ln.startswith('Full handoff notes (read this first): ')][0].split(': ', 1)[1]


def test_the_notes_hold_all_26_results_and_the_earlier_turns(provider, aux, tmp_path, children, dispatches):
    result = _run(provider, aux, tmp_path)

    assert result['turn_exit_reason'] == 'delegation_handoff' and len(dispatches) == 1
    context, path = _notes_path(dispatches[0])
    notes = open(path, encoding='utf-8').read()

    print('\n--- first 40 lines of the notes file:', path)
    print('\n'.join(notes.splitlines()[:40]))
    print('--- helper context (%d chars):' % len(context))
    print(context)
    print('--- opener / ack:', repr(result['final_response']))

    for i in range(1, 27):
        assert f'NOTE-{i:02d}-START' in notes and f'NOTE-{i:02d}-END' in notes, f'result {i} is whole'
    assert 'NOTE-27-START' not in notes, 'the refused 27th call never ran'
    assert notes.count('### ') == 26
    assert 'Who did I email about the HDR plugin last week?' in notes
    assert 'You emailed Julia <julia@example.com> on Tuesday' in notes
    assert REQUEST in notes
    assert oct(os.stat(path).st_mode & 0o777) == '0o600'

    assert 'Full handoff notes (read this first): ' + path in context
    assert 'The existing recipient is Julia' in context and 'Still to do: write and send the email' in context
    assert len(context) <= 7800
    _wait_started(children, 1)
    assert path in children[0]['system'] + str(children[0]['user'])


def test_the_agent_opens_in_its_own_voice_then_the_card(provider, aux, tmp_path, children, dispatches):
    result = _run(provider, aux, tmp_path)

    ack = result['final_response']
    assert ack.startswith(OPENER), 'the opener is the first thing the user reads'
    assert '\U0001f504 emailing the existing recipient' in ack, 'the card follows it'
    assert ack.index(OPENER) < ack.index('\U0001f504')
    assert dispatches[0]['handoff']['say'] == OPENER

    # 27 work requests (26 ran + the refused one), then ONE short tools-off call
    assert len(provider.captured) == 28
    call = provider.captured[-1]
    assert not call.get('tools'), 'the opener call carries no tool schema'
    assert call.get('max_tokens') == 80 or call.get('max_completion_tokens') == 80
    # prompt-cache friendly: the same prefix as the work requests, then one runtime note appended
    prev = provider.captured[-2]['messages']
    sent = call['messages']
    assert sent[: len(prev) - 0][:3] == prev[:3], 'system prompt + earlier turns are byte-identical'
    assert sent[0] == prev[0]
    last = sent[-1]
    assert last['role'] == 'user' and 'ONE short, casual line' in last['content']
    low = last['content'].lower()
    assert 'safety net' not in low.replace('any safety mechanism', '')
    # the live transcript is never changed by the opener call
    assert not any('ONE short, casual line' in str(m.get('content')) for m in result['messages'])
    _roles_ok(result['messages'])


def test_an_empty_opener_answer_falls_back_to_the_template(provider, aux, tmp_path, children, dispatches):
    from agent import handoff_notes as hn

    result = _run(provider, aux, tmp_path, opener='')

    ack = result['final_response']
    assert ack.startswith("this one's bigger than it looked, a subagent's on it: emailing the existing recipient")
    assert '\U0001f504 emailing the existing recipient' in ack
    for word in ('limit', 'timeout', 'timed out', 'safety net', 'backstop'):
        assert word not in ack.lower()
    assert dispatches[0]['handoff']['say'].startswith(hn.fallback_opener('')[:40])
