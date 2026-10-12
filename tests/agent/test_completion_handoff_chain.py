"""Replay of Octavia's 13:04-13:07 chain (2026-10-12), scripted provider.

13:04:01 a hand-off was dispatched after the turn had used its whole clock;
13:04:37 the helper's completion arrived and 37 s later said "the time limit is
reached" (it had inherited the spent clock); a pointless second hand-off
followed, with the SAME card title; its completion hit the limit again.

Now: the completion turn has its own full clock, so it finishes its check and
answers -- exactly ONE hand-off in the whole chain.  If even that turn runs out,
it hands off once, with a DIFFERENT, truthful title, and the completion of THAT
ends with the plain summary.
"""
from __future__ import annotations

import pytest

from agent import foreground_time_limit as fl
from agent import handoff_notes as hn
from agent import time_limit_handoff as tlh
from tests.agent.test_completion_clock import all_tool_text  # noqa: F401
from tests.agent.test_time_limit_handoff import (  # noqa: F401
    INTERNAL,
    OPENER,
    _clean_async_state,
    _script_long_job,
    _summary_requests,
    _unbind_session_context,
    advance_per_tool_round,
    clock,
    dispatches,
    make_agent,
    provider,
    stub_children,
    text,
    tool,
)

pytestmark = pytest.mark.real_time_limit_handoff

REQUEST = 'wtf happened, restore the Clover gateway'


def _first_handoff(agent, provider, clock):
    """13:04:01: the work was handed to a helper (as the router backstop did)
    after the human turn had used its whole clock; its card was titled
    "restoring the Clover gateway"."""
    import re

    provider.queue[:] = [
        tool('delegate_task', {
            'goal': 'restore the Clover gateway', 'background': True,
            'handoff': {'work': 'restoring the Clover gateway', 'outcome': 'gateway running'},
        }, 'dg'),
        text('A helper is on it.'),
    ]
    agent.run_conversation(REQUEST, conversation_history=[], task_id='r1')
    found = re.search(r'"delegation_id":\s*"([^"]+)"', all_tool_text(provider))
    assert found, 'delegate_task did not dispatch a background helper'
    provider.captured.clear()
    clock.advance(310)  # the parent's episode is used up
    return found.group(1)


def test_the_completion_37_seconds_later_finishes_its_check_and_answers_without_a_second_handoff(
        clock, provider, stub_children, dispatches, tmp_path):
    agent = make_agent(provider)
    did = _first_handoff(agent, provider, clock)
    assert len(dispatches) == 0, 'the first hand-off was the model\'s own'

    clock.advance(37)  # 13:04:01 -> 13:04:37: the parent's own episode is long spent
    advance_per_tool_round(agent, clock, 20)
    marker = tmp_path / 'checked.txt'
    provider.queue[:] = [
        tool('terminal', {'command': f'touch {marker}'}, 'k1'),
        text('The gateway is back: I checked the unit and it is running.'),
    ]
    provider.before = None

    result = agent.run_conversation(
        '[ASYNC DELEGATION COMPLETE] the helper restored the gateway',
        conversation_history=[], task_id='r2', persist_user_display_kind=INTERNAL,
        completion_delegation_ids=[did])

    assert marker.exists(), 'the check ran: the clock was not the spent one'
    assert fl.BLOCK_CODE not in '\n'.join(
        m['content'] for m in result['messages']
        if m.get('role') == 'tool' and isinstance(m.get('content'), str))
    assert len(dispatches) == 0, 'no second hand-off from the completion turn'
    assert result['final_response'].startswith('The gateway is back')
    assert result['turn_exit_reason'] != 'delegation_handoff'


def test_if_even_the_fresh_clock_runs_out_it_hands_off_once_with_a_different_title_and_then_stops(
        clock, provider, stub_children, dispatches):
    agent = make_agent(provider)
    did = _first_handoff(agent, provider, clock)
    (first_title,) = hn.recent_titles(agent.session_id)
    assert first_title == 'restoring the Clover gateway'

    clock.advance(37)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    _script_long_job(provider, 'on it, a subagent is checking the fix')
    agent.run_conversation(
        '[ASYNC DELEGATION COMPLETE] the helper restored the gateway',
        conversation_history=[], task_id='r3', persist_user_display_kind=INTERNAL,
        completion_delegation_ids=[did])

    assert len(dispatches) == 1
    second_title = dispatches[0]['handoff']['work']
    assert second_title != first_title, 'two hand-offs, two different cards'
    assert second_title.split()[0].endswith('ing')
    (second_did,) = [d for d in tlh._own(agent) if d != did]

    # the completion of THAT hand-off ends with the plain summary
    provider.captured.clear()
    clock.advance(37)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    _script_long_job(provider, 'Done: the gateway fix. Left: nothing urgent.')
    result = agent.run_conversation(
        '[ASYNC DELEGATION COMPLETE] the second helper finished',
        conversation_history=[], task_id='r4', persist_user_display_kind=INTERNAL,
        completion_delegation_ids=[second_did])

    assert len(dispatches) == 1, 'no further hand-off'
    assert result['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON
    assert len(_summary_requests(provider)) == 1


def test_a_hand_off_from_a_completion_turn_closes_the_chain_for_its_sibling_completions(
        clock, provider, stub_children, dispatches):
    agent = make_agent(provider)
    did = _first_handoff(agent, provider, clock)
    clock.advance(37)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    _script_long_job(provider)
    agent.run_conversation(
        '[ASYNC DELEGATION COMPLETE] first', conversation_history=[], task_id='s1',
        persist_user_display_kind=INTERNAL, completion_delegation_ids=[did, 'deleg_sibling'])
    assert len(dispatches) == 1

    # the sibling's completion arrives next: the chain is already closed
    agent._router_completion_ids = ('deleg_sibling',)
    assert tlh._completion_of_a_handoff(agent) is True
