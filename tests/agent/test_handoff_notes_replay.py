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
    assert 'say' not in dispatches[0]['handoff'], 'the line is written after the dispatch, not pre-written'

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
    assert last['role'] == 'user' and 'A subagent is now handling this' in last['content'] and 'ONE short line' in last['content']
    assert REQUEST not in last['content']
    low = last['content'].lower()
    assert 'safety net' not in low.replace('any safety mechanism', '')
    # the live transcript is never changed by the opener call
    assert not any('A subagent is now handling this' in str(m.get('content')) for m in result['messages'])
    _roles_ok(result['messages'])


def test_an_empty_opener_answer_falls_back_to_on_it_never_a_template(
        provider, aux, tmp_path, children, dispatches):
    result = _run(provider, aux, tmp_path, opener='')

    ack = result['final_response']
    assert ack.startswith('on it\n\n'), ack
    assert '\U0001f504 emailing the existing recipient' in ack
    for word in ('limit', 'timeout', 'timed out', 'safety net', 'backstop', 'bigger than it looked'):
        assert word not in ack.lower()
    assert REQUEST not in ack
    assert 'say' not in dispatches[0]['handoff']


# ── the exact complaint from the field: title from the work, voice from the agent ──

COMPLAINT = 'Why the fuck do you keep stopping'
DND_TODO = {'todos': [{'id': '1', 'content': 'add images to DND website', 'status': 'in_progress'}]}
DND_VOICE = "Right, handing the images over to a helper, back with the site shortly."


def _run_complaint(provider, aux, tmp_path, brief=None, voice=DND_VOICE):
    from clover_constants import get_clover_home

    from clover_cli.config_defaults import DEFAULT_CONFIG

    (get_clover_home() / 'config.yaml').write_text(yaml.safe_dump({
        '_config_version': DEFAULT_CONFIG['_config_version'],
        'delegation': {'foreground_time_limit_seconds': 300,
                       'router': {'direct_backstop': {'enabled': True, 'work_calls': 26}}},
    }))
    files = _files(tmp_path, 27)
    aux.replies[:] = [verdict_json('direct', reason='one more step'), brief if brief is not None else BULLETS]
    queue = [tool('todo', DND_TODO, 't0')] + [tool('read_file', {'path': str(f)}, f'c{i}')
                                               for i, f in enumerate(files, 1)]
    provider.queue[:] = queue
    provider.queue.append(text(voice))  # the scripted model turn that writes the opener
    agent = make_agent(provider, session_id='replay-complaint')
    return agent.run_conversation(COMPLAINT, conversation_history=list(HISTORY), task_id='replay-complaint')


def _show(label, result, dispatches):
    print(f'\n--- {label}')
    print('opener + card text :', repr(result['final_response']))
    print('card title (work)  :', dispatches[0]['handoff']['work'])
    print('goal (outcome)     :', dispatches[0]['handoff']['outcome'])


def test_the_complaint_is_titled_from_the_todo_and_opened_by_the_agent(
        provider, aux, tmp_path, children, dispatches):
    """No small-model answer (it times out / is empty): the todo names the work."""
    result = _run_complaint(provider, aux, tmp_path)

    assert result['turn_exit_reason'] == 'delegation_handoff' and len(dispatches) == 1
    _show('complaint, small model gave bullets only (heuristic title)', result, dispatches)
    handoff = dispatches[0]['handoff']
    assert handoff['work'] == 'adding images to DND website'
    blob = (result['final_response'] + str(handoff)).lower()
    for bad in ('fuck', 'answering', 'keep stopping', 'bigger than it looked'):
        assert bad not in blob
    assert result['final_response'].startswith(DND_VOICE), 'the opener is the scripted model turn'
    assert 'say' not in handoff
    assert len(provider.captured) == 29, 'todo + 27 reads + the one voice turn'
    voice_call = provider.captured[-1]
    assert not voice_call.get('tools')
    note = voice_call['messages'][-1]['content']
    assert 'adding images to DND website' in note and 'Why the fuck' not in note


def test_a_small_model_title_is_used_for_the_card_and_the_voice_note(
        provider, aux, tmp_path, children, dispatches):
    brief = ('{"title": "finishing the DND website", "goal": "site live on dndsecurity.ca", '
             '"known": ["Pages are built.", "Images still missing."]}')
    result = _run_complaint(provider, aux, tmp_path, brief=brief)

    _show('complaint, small model JSON', result, dispatches)
    handoff = dispatches[0]['handoff']
    assert handoff['work'] == 'finishing the DND website'
    assert handoff['outcome'] == 'site live on dndsecurity.ca'
    assert '\U0001f504 finishing the DND website' in result['final_response']
    assert 'finishing the DND website' in provider.captured[-1]['messages'][-1]['content']
    assert 'Pages are built.' in dispatches[0]['tasks'][0]['context']


def test_a_model_title_that_echoes_the_complaint_is_dropped(
        provider, aux, tmp_path, children, dispatches):
    brief = '{"title": "answering why the fuck do you keep stopping", "goal": "stop stopping", "known": ["x"]}'
    result = _run_complaint(provider, aux, tmp_path, brief=brief)
    assert dispatches[0]['handoff']['work'] == 'adding images to DND website'
    assert 'fuck' not in result['final_response'].lower()


def test_a_failed_voice_turn_posts_on_it_not_a_template(provider, aux, tmp_path, children, dispatches):
    result = _run_complaint(provider, aux, tmp_path, voice='')
    _show('complaint, voice turn empty', result, dispatches)
    assert result['final_response'].startswith('on it\n\n')
    low = result['final_response'].lower()
    assert 'bigger than it looked' not in low and 'keep stopping' not in low and 'fuck' not in low


PUSH = 'Push it to the domain. dndsecurity.ca'


def test_push_it_to_the_domain_is_not_titled_from_the_message(
        provider, aux, tmp_path, children, dispatches):
    """The live case of 10:47: the card read "working on push it to the domain"."""
    brief = ('{"title": "deploying the DND site to dndsecurity.ca", "goal": "site live on dndsecurity.ca", '
             '"known": ["Build is ready in dist/."]}')
    from clover_constants import get_clover_home

    from clover_cli.config_defaults import DEFAULT_CONFIG

    (get_clover_home() / 'config.yaml').write_text(yaml.safe_dump({
        '_config_version': DEFAULT_CONFIG['_config_version'],
        'delegation': {'foreground_time_limit_seconds': 300,
                       'router': {'direct_backstop': {'enabled': True, 'work_calls': 26}}},
    }))
    files = _files(tmp_path, 27)
    aux.replies[:] = [verdict_json('direct', reason='one more step'), brief]
    provider.queue[:] = [tool('read_file', {'path': str(f)}, f'c{i}') for i, f in enumerate(files, 1)]
    provider.queue.append(text('On it, the deploy is with a helper now.'))
    agent = make_agent(provider, session_id='replay-push')
    result = agent.run_conversation(PUSH, conversation_history=list(HISTORY), task_id='replay-push')

    _show('"Push it to the domain. dndsecurity.ca"', result, dispatches)
    handoff = dispatches[0]['handoff']
    assert handoff['work'] == 'deploying the DND site to dndsecurity.ca'
    assert handoff['outcome'] == 'site live on dndsecurity.ca'
    assert 'push it to the domain' not in result['final_response'].lower()
    assert result['final_response'].startswith('On it, the deploy is with a helper now.')
