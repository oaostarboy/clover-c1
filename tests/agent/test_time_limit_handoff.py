"""The foreground time limit's hard stop hands the rest to a helper.

The Octavia incident: a delegation-completion turn ran out of time while it
was integrating results, the runtime asked the model to "summarize what is done
and what is left ... and offer to continue", the model obeyed ("I can continue
from here") and STOPPED.  The user had to type "continue".

Now an unfinished hard stop dispatches one background helper through the same
path as the router backstop (hand-off notes, "known so far", title, the agent's
own opener) and ends the turn with that opener.  The old summary request is
only the fallback, and it no longer offers to continue.

Real ``AIAgent.run_conversation`` against a scripted local provider, a fake
monotonic clock, the real ``delegate_task`` handler (only a child's own model
run is stubbed) and a real ``SessionDB`` / durable ledger in a scratch
``CLOVER_HOME``.
"""
from __future__ import annotations

import json
from typing import Any, List

import pytest

import tools.async_delegation as ad
from agent import foreground_time_limit as fl
from agent import time_limit_handoff as tlh
from agent.context_compressor import TIME_LIMIT_SUMMARY_REQUEST
from tests.agent.test_foreground_time_limit_loop import (  # noqa: F401
    TODO,
    Provider,
    _limit_on,
    advance_per_tool_round,
    clock,
    make_agent,
    provider,
    text,
    tool,
)
from tests.agent.test_foreground_verification_episode import (  # noqa: F401
    INTERNAL,
    _clean_async_state,
    stub_children,
)

pytestmark = pytest.mark.real_time_limit_handoff

OPENER = 'on it, a subagent is finishing the rest of the big job'


@pytest.fixture(autouse=True)
def _unbind_session_context():
    yield
    import gateway.session_context as sc

    for var in sc._VAR_MAP.values():
        var.set(sc._UNSET)
    sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)


@pytest.fixture
def dispatches(monkeypatch) -> List[dict]:
    """Every ``delegate_task`` dispatch through the registry, then the real one."""
    from tools.registry import registry

    seen: List[dict] = []
    real = registry.dispatch

    def spy(name, args, **kwargs):
        if name == 'delegate_task':
            seen.append(dict(args))
        return real(name, args, **kwargs)

    monkeypatch.setattr(registry, 'dispatch', spy)
    return seen


def _toolless(provider: Provider) -> List[dict]:
    return [r for r in provider.captured if not r.get('tools')]


def _script_long_job(provider: Provider, answer: str = OPENER) -> None:
    """The model never stops calling tools; any no-tools call gets ``answer``."""
    provider.queue[:] = [tool('todo', TODO, f'c{i}') for i in range(1, 30)]

    def before(n, req):
        if not req.get('tools'):
            provider.queue.insert(0, text(answer))

    provider.before = before


def _summary_requests(provider: Provider) -> List[dict]:
    return [
        r for r in provider.captured
        if any(m.get('content') == TIME_LIMIT_SUMMARY_REQUEST for m in r.get('messages', []))
    ]


# ── the wording ────────────────────────────────────────────────────────────

def test_the_fallback_summary_request_never_offers_to_continue():
    lowered = TIME_LIMIT_SUMMARY_REQUEST.lower()
    assert 'continue' not in lowered and 'offer' not in lowered
    assert 'done' in lowered and 'left' in lowered


def test_the_previous_wording_is_still_recognised_as_scaffolding():
    from agent.context_compressor import (
        PREVIOUS_TIME_LIMIT_SUMMARY_REQUEST,
        is_user_originated_turn,
    )

    assert 'offer to continue' in PREVIOUS_TIME_LIMIT_SUMMARY_REQUEST
    assert not is_user_originated_turn(
        {'role': 'user', 'content': PREVIOUS_TIME_LIMIT_SUMMARY_REQUEST})
    assert not is_user_originated_turn(
        {'role': 'user', 'content': TIME_LIMIT_SUMMARY_REQUEST})


def test_the_no_summary_reply_does_not_ask_the_user_to_continue():
    from agent.chat_completion_helpers import _TIME_LIMIT_STOPPED_REPLY

    assert 'continue' not in _TIME_LIMIT_STOPPED_REPLY.lower()


# ── a human turn that runs out of time ─────────────────────────────────────

def test_hard_stop_with_remaining_work_dispatches_one_helper_and_ends_with_an_opener(
        clock, provider, stub_children, dispatches):
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    _script_long_job(provider)

    result = agent.run_conversation('big job', conversation_history=[], task_id='tlh1')

    assert len(dispatches) == 1, 'exactly one helper'
    assert result['turn_exit_reason'] == 'delegation_handoff'
    assert OPENER in result['final_response']
    assert result['completed'] is True and not result['failed']
    # the model was never asked to summarize / offer to continue
    assert _summary_requests(provider) == []
    assert 'continue' not in result['final_response'].lower()
    # nothing about limits reaches the user
    for word in ('time limit', 'timed out', 'timeout', 'limit'):
        assert word not in result['final_response'].lower()
    roles = [m['role'] for m in result['messages']]
    assert roles[-1] == 'assistant'
    assert all(not (a == b == 'user') for a, b in zip(roles, roles[1:]))


def test_the_helper_gets_the_request_and_the_turns_work_so_far(
        clock, provider, stub_children, dispatches, tmp_path):
    from clover_constants import get_clover_home

    source = tmp_path / 'billing.py'
    source.write_text('def charge(): return 42  # BILLING-SOURCE-MARKER\n')
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    _script_long_job(provider)
    provider.queue.insert(0, tool('read_file', {'path': str(source)}, 'rf1'))

    agent.run_conversation('refactor the billing module', conversation_history=[], task_id='tlh2')

    (call,) = dispatches
    (task,) = call['tasks']
    assert 'refactor the billing module' in task['context']
    assert 'Full handoff notes' in task['context']  # the notes file rides along
    notes = list((get_clover_home() / 'cache' / 'handoff').glob('*.md'))
    assert len(notes) == 1
    body = notes[0].read_text()
    assert 'refactor the billing module' in body
    assert 'BILLING-SOURCE-MARKER' in body  # what the turn read so far, in full
    # the hand-off carries a real title, not the placeholder
    assert call['handoff']['work'] == 'refactoring the billing module'
    assert 'say' not in call['handoff'], 'the line is the agent\'s own turn after the dispatch'


def test_the_opener_is_the_replay_of_the_agents_own_voice_and_is_streamed(
        clock, provider, stub_children, dispatches):
    agent = make_agent(provider)
    streamed: List[Any] = []
    agent.stream_delta_callback = streamed.append
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    _script_long_job(provider)

    result = agent.run_conversation('big job', conversation_history=[], task_id='tlh3')

    assert streamed[-1] is None
    assert any(isinstance(s, str) and OPENER in s for s in streamed)
    assert result['messages'][-1]['content'] == result['final_response']


# ── a completion turn that runs out of time ────────────────────────────────

def test_a_completion_turn_hard_stop_hands_off_the_integration(
        clock, provider, stub_children, dispatches):
    agent = make_agent(provider)
    history = [
        {'role': 'user', 'content': 'finish the DND Security website'},
        {'role': 'assistant', 'content': 'Two helpers are on the images and the text.'},
    ]
    clock.advance(1000)  # the helpers took longer than the human's episode
    # the verification window is 120 s: spend it, then the iterations
    advance_per_tool_round(agent, clock, 70, after_limit=5, limit=120)
    _script_long_job(provider, 'on it, a subagent is wrapping up the website')

    result = agent.run_conversation(
        '[ASYNC DELEGATION BATCH COMPLETE] 2 subagents finished the images and the text',
        conversation_history=history, task_id='tlh4',
        persist_user_display_kind=INTERNAL, completion_delegation_ids=['deleg_unknown'],
    )

    assert len(dispatches) == 1
    assert result['turn_exit_reason'] == 'delegation_handoff'
    assert 'subagent' in result['final_response']
    assert _summary_requests(provider) == []
    (task,) = dispatches[0]['tasks']
    # the completion results AND the user's original request ride along
    assert 'finish the DND Security website' in task['context']
    assert 'Finish integrating the helper results' in task['goal']
    notes_path = task['context'].split('Full handoff notes (read this first): ', 1)[1].split('\n', 1)[0]
    with open(notes_path) as fh:
        body = fh.read()
    assert '2 subagents finished the images and the text' in body


# ── the fallback: no hand-off possible ─────────────────────────────────────

def test_no_delegate_tool_falls_back_to_a_summary_that_does_not_offer_to_continue(
        clock, provider, dispatches):
    agent = make_agent(provider, enabled_toolsets=['todo', 'file', 'terminal', 'cronjob', 'clarify'])
    assert 'delegate_task' not in agent.valid_tool_names
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    _script_long_job(provider, 'Done: the table. Left: the second table.')

    result = agent.run_conversation('big job', conversation_history=[], task_id='tlh5')

    assert dispatches == []
    assert result['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON
    assert result['final_response'] == 'Done: the table. Left: the second table.'
    (request,) = _summary_requests(provider)
    asked = request['messages'][-1]['content']
    assert 'continue' not in asked.lower() and 'offer' not in asked.lower()


def test_the_flag_off_keeps_the_old_behaviour(clock, provider, dispatches, monkeypatch):
    monkeypatch.setattr(tlh, 'enabled', lambda: False)
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    _script_long_job(provider, 'Here is where this stands.')

    result = agent.run_conversation('big job', conversation_history=[], task_id='tlh6')

    assert dispatches == []
    assert result['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON
    assert result['final_response'] == 'Here is where this stands.'
    assert len(_summary_requests(provider)) == 1


def test_a_refused_dispatch_falls_back_to_the_summary(
        clock, provider, stub_children, dispatches, monkeypatch):
    import agent.tool_executor as te

    monkeypatch.setattr(
        te, 'dispatch_generated_tool',
        lambda *a, **k: json.dumps({'error': 'blocked by a policy'}))
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    _script_long_job(provider, 'Done so far: x. Left: y.')

    result = agent.run_conversation('big job', conversation_history=[], task_id='tlh7')

    assert result['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON
    assert result['final_response'] == 'Done so far: x. Left: y.'
    assert len(_summary_requests(provider)) == 1


def test_a_dispatch_that_raises_falls_back_to_the_summary(
        clock, provider, stub_children, dispatches, monkeypatch):
    import agent.tool_executor as te

    def boom(*a, **k):
        raise RuntimeError('registry down')

    monkeypatch.setattr(te, 'dispatch_generated_tool', boom)
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    _script_long_job(provider, 'Done so far: x. Left: y.')

    result = agent.run_conversation('big job', conversation_history=[], task_id='tlh8')

    assert result['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON
    assert result['final_response'] == 'Done so far: x. Left: y.'


def test_do_it_yourself_is_never_handed_off(clock, provider, dispatches):
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    _script_long_job(provider, 'Done so far: x. Left: y.')

    result = agent.run_conversation(
        'do it yourself, no helpers: rename every file', conversation_history=[], task_id='tlh9')

    assert dispatches == []
    assert result['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON


def test_a_pending_stop_is_respected(clock, provider, stub_children, dispatches):
    agent = make_agent(provider)
    messages = [{'role': 'user', 'content': 'big job'},
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'c1', 'type': 'function',
                     'function': {'name': 'todo', 'arguments': json.dumps(TODO)}}]},
                {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'}]
    agent._interrupt_requested = True

    assert tlh.hand_off(agent, messages) is None
    assert dispatches == []


def test_a_surface_that_cannot_deliver_a_detached_helper_never_hands_off(
        clock, provider, stub_children, dispatches, monkeypatch):
    agent = make_agent(provider)
    monkeypatch.setattr(fl, '_detached_delivery_available', lambda: False)
    messages = [{'role': 'user', 'content': 'big job'},
                {'role': 'assistant', 'content': 'working'}]

    assert tlh.hand_off(agent, messages) is None
    assert dispatches == []


# ── never twice ────────────────────────────────────────────────────────────

def test_a_second_hard_stop_call_acknowledges_the_first_helper_and_dispatches_nothing(
        clock, provider, stub_children, dispatches):
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    _script_long_job(provider)
    result = agent.run_conversation('big job', conversation_history=[], task_id='tlh10')
    assert len(dispatches) == 1

    # the same turn's state asked again (e.g. a retried final step)
    again = tlh.hand_off(agent, list(result['messages'][:-1]))

    assert len(dispatches) == 1, 'never hand off twice for the same remaining work'
    assert again is not None  # it acknowledges the helper that already owns it


def test_a_turn_the_router_backstop_already_handed_off_is_acknowledged_not_redispatched(
        clock, provider, stub_children, dispatches):
    from agent import delegation_router as dr
    from agent import router_backstop as rb

    agent = make_agent(provider)
    verdict = dr.Verdict(
        mode='single', reason='big',
        tasks=(dr.PlanTask(goal='do the whole job', acceptance='done'),))
    rb.begin(agent, verdict, 'big job', dr.RouterSettings(enabled=True, backstop_calls=1))
    state = rb._state(agent)
    info = rb._dispatch(agent, state)
    assert info['ok'] and len(dispatches) == 1
    state.handoff = info

    reply = tlh.hand_off(
        agent, [{'role': 'user', 'content': 'big job'}, {'role': 'assistant', 'content': 'x'}])

    assert reply is not None
    assert len(dispatches) == 1


def test_the_completion_of_a_time_limit_handoff_is_never_handed_off_again(
        clock, provider, stub_children, dispatches):
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    _script_long_job(provider)
    agent.run_conversation('big job', conversation_history=[], task_id='tlh11')
    (did,) = list(tlh._own(agent))
    assert len(dispatches) == 1
    provider.captured.clear()

    # the helper's completion arrives (after the human's episode) and THAT turn
    # also runs out of its verification window
    clock.advance(1000)
    _script_long_job(provider, 'Done: the first part. Left: the checks.')
    result = agent.run_conversation(
        '[ASYNC DELEGATION COMPLETE] the helper finished', conversation_history=[],
        task_id='tlh11b', persist_user_display_kind=INTERNAL, completion_delegation_ids=[did],
    )

    assert len(dispatches) == 1, 'one hand-off per remaining work, never a chain'
    assert result['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON
    assert result['final_response'] == 'Done: the first part. Left: the checks.'
    assert len(_summary_requests(provider)) == 1


def test_the_marker_survives_a_rebuilt_agent(
        clock, provider, stub_children, dispatches):
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    _script_long_job(provider)
    agent.run_conversation('big job', conversation_history=[], task_id='tlh12')
    (did,) = list(tlh._own(agent))

    reborn = make_agent(provider)
    reborn._router_completion_ids = (did,)

    assert tlh._completion_of_a_handoff(reborn) is True


# ── config ─────────────────────────────────────────────────────────────────

def test_setting_defaults_to_on_and_reads_the_config(monkeypatch):
    import yaml
    from clover_cli.config_defaults import DEFAULT_CONFIG
    from clover_constants import get_clover_home

    assert DEFAULT_CONFIG['delegation']['time_limit_handoff'] is True
    monkeypatch.delenv('CLOVER_IGNORE_USER_CONFIG', raising=False)
    path = get_clover_home() / 'config.yaml'

    path.write_text(yaml.safe_dump({'delegation': {}}))
    assert tlh.enabled() is True
    path.write_text(yaml.safe_dump({'delegation': {'time_limit_handoff': False}}))
    assert tlh.enabled() is False
    path.write_text(yaml.safe_dump({'delegation': {'time_limit_handoff': 'garbage'}}))
    assert tlh.enabled() is True


def test_a_complaint_at_the_hard_stop_is_titled_from_the_earlier_request_not_the_complaint(
        clock, provider, stub_children, dispatches):
    """"Why the fuck do you keep stopping" must not become the card or the line."""
    agent = make_agent(provider)
    history = [
        {'role': 'user', 'content': 'finish the DND Security website'},
        {'role': 'assistant', 'content': 'Working on the pages now.'},
    ]
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    _script_long_job(provider)

    result = agent.run_conversation(
        'Why the fuck do you keep stopping', conversation_history=history, task_id='tlh-complaint')

    (call,) = dispatches
    blob = (result['final_response'] + str(call['handoff'])).lower()
    for bad in ('fuck', 'answering', 'keep stopping', 'bigger than it looked'):
        assert bad not in blob
    assert call['handoff']['work'] == 'finishing the DND Security website'
    assert result['final_response'].startswith(OPENER), 'the agent\'s own scripted turn is the opener'
