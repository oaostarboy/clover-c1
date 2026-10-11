"""Reviewer probes (Fable, C1.4 P1) on the real loop. Not part of the PR."""
from __future__ import annotations

import copy
import json

from agent import foreground_time_limit as fl
from tests.agent.test_foreground_time_limit_loop import (  # noqa: F401
    TODO, FakeClock, Provider, _limit_on, advance_per_tool_round, clock, make_agent,
    provider, text, tool, tool_texts,
)


def _rows(msgs):
    return [json.dumps(m, sort_keys=True) for m in msgs]


def _assert_prefix_stable(reqs):
    for i, (a, b) in enumerate(zip(reqs, reqs[1:])):
        ma, mb = _rows(a['messages']), _rows(b['messages'])
        assert ma == mb[:len(ma)], f'cache prefix changed between call {i+1} and {i+2}'


def test_probe_prefix_stable_when_notice_fires(clock, provider):
    agent = make_agent(provider)
    # 115 s rounds: t=115, 230, 345 -> notice + finish mode at call 4, hard stop
    # NOT yet due (345 < 360, 1 finish iteration), so call 4 still carries tools
    advance_per_tool_round(agent, clock, 115)
    provider.queue[:] = [tool('todo', TODO, 'c1'), tool('todo', TODO, 'c2'),
                         tool('todo', TODO, 'c3'), text('ok')]
    result = agent.run_conversation('job', conversation_history=[], task_id='pp')
    reqs = provider.captured
    assert len(reqs) == 4 and all(r.get('tools') for r in reqs)
    assert result['turn_exit_reason'].startswith('text_response')
    _assert_prefix_stable(reqs)
    assert sum(t.count(fl.NOTICE_MARKER) for r in reqs for t in tool_texts(r)) >= 1
    # the notice is attached exactly once in the final transcript
    assert sum(str(m.get('content', '')).count(fl.NOTICE_MARKER)
               for m in result['messages']) == 1
    assert result['final_response'] == 'ok'


def test_probe_hard_stop_single_finalizer_prefix_stable(clock, provider):
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    provider.queue[:] = [tool('todo', TODO, f'c{i}') for i in range(1, 20)]
    calls = {'n': 0}
    orig = agent._handle_max_iterations

    def spy(*a, **k):
        calls['n'] += 1
        return orig(*a, **k)

    agent._handle_max_iterations = spy
    provider.before = lambda n, req: (
        None if req.get('tools') else provider.queue.insert(0, text('SUM')))
    result = agent.run_conversation('job', conversation_history=[], task_id='hs')
    assert calls['n'] == 1, calls
    assert result['final_response'] == 'SUM'
    assert result['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON
    with_tools = [r for r in provider.captured if r.get('tools')]
    assert len(with_tools) == 6 and not provider.captured[-1].get('tools')
    _assert_prefix_stable(with_tools)  # every tool-bearing call keeps the prefix
    # the one accepted miss: the summary call (pre-existing handle_max_iterations
    # serialization, chat_completion_helpers.py:3171) -- record, don't assert
    roles = [m['role'] for m in result['messages']]
    # never two user rows or two assistant rows back to back
    assert all(not (a == b and a in ('user', 'assistant'))
               for a, b in zip(roles, roles[1:])), roles
    assert roles[-2] == 'user', roles  # the single summary request precedes the answer
    assert roles[-1] == 'assistant'


def test_probe_internal_turn_defers_notice_keeps_history_then_human_resets(
        clock, provider, tmp_path):
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 100)
    provider.queue[:] = [tool('todo', TODO, 'h1'), text('handed off')]
    r1 = agent.run_conversation('job', conversation_history=[], task_id='t1')
    assert r1['final_response'] == 'handed off'
    assert agent._fg_episode.notice_sent is False
    clock.advance(150)  # t=250 since the human message: past 80%
    history = [m for m in r1['messages'] if m.get('role') != 'system']
    history = history[:-1]  # tail is the h1 tool row of the previous turn
    assert history[-1]['role'] == 'tool'
    snapshot = copy.deepcopy(history)
    ids = [id(m) for m in history]
    n_before = len(provider.captured)
    provider.queue[:] = [tool('todo', TODO, 'i1'), text('integrated')]
    r2 = agent.run_conversation(
        'helper finished', conversation_history=history, task_id='t2',
        persist_user_display_kind='internal_notification')
    assert r2['final_response'] == 'integrated'
    this_turn = provider.captured[n_before:]
    assert len(this_turn) == 2
    # the previous turn's tool row was neither rewritten nor replaced
    for before, after in zip(snapshot, r2['messages']):
        assert before == after
    assert [id(m) for m in r2['messages'][:len(ids)]] == ids
    assert not any(fl.NOTICE_MARKER in t for t in tool_texts(this_turn[0])), \
        'notice reached back into the previous turn'
    texts = tool_texts(this_turn[1])
    assert fl.NOTICE_MARKER in texts[-1] and sum(t.count(fl.NOTICE_MARKER) for t in texts) == 1
    _assert_prefix_stable(this_turn)
    # a new human message resets: heavy work is admitted again
    clock.advance(100)  # t=350: expired for the inherited episode
    marker = tmp_path / 'ok.txt'
    provider.queue[:] = [tool('terminal', {'command': f'touch {marker}'}, 'x1'), text('fine')]
    r3 = agent.run_conversation('new ask', conversation_history=r2['messages'], task_id='t3')
    assert marker.exists(), 'human turn did not reset the clock'
    assert r3['final_response'] == 'fine'


def test_probe_late_internal_turn_runs_finish_mode_then_hard_stops(clock, provider, tmp_path):
    agent = make_agent(provider)
    provider.queue[:] = [text('handed off')]
    r1 = agent.run_conversation('job', conversation_history=[], task_id='l1')
    clock.advance(600)  # helper finishes long after the limit
    blocked = tmp_path / 'blocked.txt'
    provider.queue[:] = [tool('write_file', {'path': str(blocked), 'content': 'x'}, 'w1')] + \
        [tool('todo', TODO, f'i{i}') for i in range(10)]
    provider.before = lambda n, req: (
        None if req.get('tools') else provider.queue.insert(0, text('LATE SUMMARY')))
    n_before = len(provider.captured)
    r2 = agent.run_conversation(
        'helper finished', conversation_history=r1['messages'], task_id='l2',
        persist_user_display_kind='internal_notification')
    assert r2['final_response'] == 'LATE SUMMARY'
    assert r2['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON
    assert not blocked.exists()
    with_tools = [r for r in provider.captured[n_before:] if r.get('tools')]
    toolless = [r for r in provider.captured[n_before:] if not r.get('tools')]
    assert len(with_tools) == 3 and len(toolless) == 1


def test_probe_turn_flag_cleared_after_turn_and_after_exception(clock, provider):
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 400)
    provider.queue[:] = [tool('todo', TODO, 'c1'), text('done')]
    agent.run_conversation('job', conversation_history=[], task_id='s1')
    assert agent._fg_turn_active is False
    assert fl.refusal(agent, 'terminal', {'command': 'true'}) is None

    agent2 = make_agent(provider)

    def boom(*a, **k):
        raise RuntimeError('boom')

    agent2._execute_tool_calls = boom
    provider.queue[:] = [tool('todo', TODO, 'c1'), text('done')]
    escaped = False
    try:
        agent2.run_conversation('job', conversation_history=[], task_id='s2')
    except RuntimeError:
        escaped = True
    clock.advance(400)
    stale = fl.refusal(agent2, 'terminal', {'command': 'true'})
    assert stale is None, (
        f'stale flag limits out-of-turn call: escaped={escaped} '
        f'active={agent2._fg_turn_active}')


def test_probe_scope_and_verdict_truthiness(clock, provider):
    for plat, expected in [('telegram', True), ('cli', True), ('tui', True),
                           ('api_server', True), ('discord', True),
                           ('cron', False), ('local', False), ('webhook', False)]:
        a = make_agent(provider, platform=plat)
        assert fl.applies(a) is expected, plat
    a = make_agent(provider)
    a._delegate_depth = 1
    assert not fl.applies(a)
    a = make_agent(provider)
    a._delegation_checkpoint_exempt = 'oneshot'
    assert not fl.applies(a)
    a = make_agent(provider)
    a._foreground_limit_exempt = 'single_query'
    assert not fl.applies(a)
    a = make_agent(provider)
    fl.begin_turn(a)
    clock.advance(301)
    v = fl.refusal(a, 'terminal', {'command': 'x'})
    assert v is not None and bool(v) is True and v.blocked
