"""Round-3 probe (C1.4 P1b, 35147590): display sinks, export, legacy literal, normal failures."""
from __future__ import annotations
import io, json, contextlib
import pytest
from agent import foreground_time_limit as fl
from tests.agent.test_foreground_time_limit_loop import (  # noqa: F401
    Provider, _limit_on, _write_limit, advance_per_tool_round, clock, make_agent, provider, text, tool, TODO,
)
from tests.agent.test_handoff_upfront import BANNED

FACTS = '/results/p1br3-facts.txt'
LIMIT_WORDS = ('time limit', 'timeout', 'timed out', 'limit is reached', 'wrap up', 'was not run', 'working longer')


def fact(*a):
    with open(FACTS, 'a') as fh:
        fh.write(' '.join(str(x) for x in a) + '\n')


def leaks(s):
    low = str(s).lower()
    return [w for w in LIMIT_WORDS if w in low]


@pytest.fixture(autouse=True)
def _unbind():
    yield
    import gateway.session_context as sc
    for var in sc._VAR_MAP.values():
        var.set(sc._UNSET)
    sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)


def multi(calls, cid0):
    tcs = [{'id': f'{cid0}{i}', 'type': 'function', 'function': {'name': n, 'arguments': json.dumps(a)}} for i, (n, a) in enumerate(calls)]
    return {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '', 'tool_calls': tcs}, 'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}


def _run(clock, provider, **kw):
    """Past the limit: one terminal call (sequential path), then a web_search+web_extract batch (concurrent path)."""
    feed, prog, comp = [], [], []
    agent = make_agent(provider, enabled_toolsets=['todo', 'delegation', 'file', 'terminal', 'cronjob', 'clarify', 'web'], **kw)
    if kw.get('platform') != 'cli':
        agent.tool_progress_callback = lambda et, name=None, preview=None, args=None, **k: prog.append((et, name, preview, k.get('result')))
        agent.tool_complete_callback = lambda cid, name, args, result: comp.append((name, result))
        agent._r3_starts = []
        agent.tool_start_callback = lambda cid, name, args: agent._r3_starts.append(name)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    provider.queue[:] = [tool('todo', TODO, f'c{i}') for i in range(1, 5)]
    provider.queue += [tool('terminal', {'command': 'sleep 9'}, 't1'),
                       multi([('web_search', {'query': 'x'}), ('web_extract', {'urls': ['http://u']})], 'w'),
                       text('Done so far.'), text('Done so far.')]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = agent.run_conversation('big job', conversation_history=[], task_id='t-r3')
    return agent, r, buf.getvalue(), prog, comp


def test_real_run_sinks_vs_model(clock, provider):
    agent, r, out, prog, comp = _run(clock, provider)
    msgs = r['messages']
    tool_rows = [m for m in msgs if m.get('role') == 'tool']
    refused = [m for m in tool_rows if fl.BLOCK_CODE in str(m.get('content'))]
    fact('REFUSED tool rows (model sees)', len(refused), [str(m.get('content'))[:90] for m in refused])
    assert len(refused) >= 3, [str(m.get('content'))[:80] for m in tool_rows]
    for m in refused:
        assert 'The time limit is reached' in str(m['content'])  # model still gets the full refusal
    # provider wire: refusal reached the model
    wire_refusals = sum(1 for req in provider.captured for m in req['messages'] if m.get('role') == 'tool' and fl.BLOCK_CODE in str(m.get('content')))
    fact('WIRE refusal tool rows', wire_refusals)
    assert wire_refusals >= 3
    # callbacks (TUI/desktop/gateway/oneshot) got neutral payloads
    fact('PROGRESS completed', [(n, str(res)[:70]) for et, n, p, res in prog if et == 'tool.completed'])
    fact('PROGRESS events for refused tools', [(et, n, str(p)[:60]) for et, n, p, res in prog if n in ('terminal', 'web_search', 'web_extract')])
    fact('START cb for refused', getattr(agent, '_r3_starts', None))
    fact('COMPLETE cb', [(n, str(res)[:70]) for n, res in comp])
    bad_prog = [(n, res) for et, n, p, res in prog if leaks(res) or leaks(p)]
    bad_comp = [(n, res) for n, res in comp if leaks(res)]
    assert bad_prog == [] and bad_comp == [], (bad_prog, bad_comp)
    fact('COMPLETE cb names', [n for n, _ in comp])
    fact('STDOUT leaks', leaks(out), repr(out[:200]))
    assert leaks(out) == []
    assert leaks(r['final_response']) == [] and not any(b in r['final_response'].lower() for b in BANNED)
    # alternation on last wire
    roles = [m['role'] for m in provider.captured[-1]['messages']]
    dup = [(a, b) for a, b in zip(roles, roles[1:]) if a == b and a != 'tool']
    foreign = sorted({k for m in provider.captured[-1]['messages'] for k in m if k not in {'role', 'content', 'tool_calls', 'tool_call_id', 'name'}})
    fact('ROLES', roles, 'DUP', dup, 'FOREIGN', foreign)
    assert dup == [] and 'display_kind' not in foreign and 'display_metadata' not in foreign


def test_real_run_cli_feed_quiet_mode(clock, provider):
    agent, r, out, prog, comp = _run(clock, provider, platform='cli', quiet_mode=True, tool_progress_mode='all')
    lines = [l for l in out.splitlines() if l.strip()]
    fact('CLI FEED lines', len(lines), [l[:110] for l in lines][:8])
    assert lines, 'feed printed nothing'
    assert any('[skipped]' in l for l in lines), lines
    assert leaks(out) == [], out
    agent2, r2, out2, _, _ = _run(clock, provider, platform='cli', quiet_mode=False, tool_progress_mode='all', verbose_logging=True)
    fact('VERBOSE leaks', leaks(out2), [l[:140] for l in out2.splitlines() if leaks(l)][:3])
    agent3, r3, out3, _, _ = _run(clock, provider, platform='cli', quiet_mode=False, tool_progress_mode='all')
    fact('NONQUIET leaks', leaks(out3), [l[:140] for l in out3.splitlines() if leaks(l)][:3])
    assert leaks(out2) == [] and leaks(out3) == [], (leaks(out2), leaks(out3))


def test_tui_tool_complete_payloads(clock, provider, monkeypatch):
    import tui_gateway.server as srv
    agent = make_agent(provider)
    fl.begin_turn(agent)
    clock.advance(400)
    captured = []
    monkeypatch.setattr(srv, '_emit', lambda ev, sid, payload=None: captured.append((ev, payload)))
    monkeypatch.setattr(srv, '_session_verbose', lambda sid: True)
    for name, args in (('terminal', {'command': 'sleep 9'}), ('web_search', {'query': 'x'}), ('browser_navigate', {'url': 'u'}), ('process', {'action': 'wait', 'pid': 1})):
        v = fl.refusal(agent, name, args)
        assert v is not None, name
        srv._on_tool_complete('nosuch-sid', 'tc-' + name, name, args, v.block_result)
    fl.end_turn(agent)
    for ev, payload in captured:
        s = json.dumps(payload, default=str)
        fact('TUI', ev, s[:160])
        assert leaks(s) == [], s
    assert len(captured) >= 4


def test_normal_failures_unchanged_and_no_false_skip():
    from agent.display import _detect_tool_failure, _get_cute_tool_message
    from agent.foreground_time_limit import display_result
    cases = {
        ('terminal', json.dumps({'exit_code': 1, 'stdout': '', 'stderr': 'boom'})): ' [exit 1]',
        ('terminal', json.dumps({'exit_code': 2, 'error': 'command not found'})): ' [command not found]',
        ('web_search', json.dumps({'error': 'rate limited', 'success': False})): ' [rate limited]',
        ('read_file', json.dumps({'error': 'no such file'})): ' [no such file]',
        ('read_file', 'Error: nope'): ' [error]',
    }
    for (name, raw), want in cases.items():
        assert display_result(raw) == raw
        got = _detect_tool_failure(name, raw)
        fact('NORMAL', name, got)
        assert got == (True, want), (name, got)
    ok = json.dumps({'content': 'mentions foreground_time_limit_reached in a file', 'error_type': 'other'})
    assert display_result(ok) == ok and _detect_tool_failure('read_file', ok) == (False, '')
    assert _detect_tool_failure('terminal', json.dumps({'exit_code': 0, 'stdout': 'hi'})) == (False, '')
    assert display_result({'_multimodal': True}) == {'_multimodal': True}
    line = _get_cute_tool_message('terminal', {'command': 'ls'}, 0.2, json.dumps({'exit_code': 1, 'stderr': 'x'}))
    fact('NORMAL LINE', repr(line)[:120])
    assert '[exit 1]' in line


def test_markdown_export_and_legacy_literal(clock, provider, tmp_path):
    from clover_state import SessionDB
    from agent.context_compressor import (TIME_LIMIT_SUMMARY_REQUEST, LEGACY_TIME_LIMIT_SUMMARY_REQUEST,
                                          MAX_ITERATIONS_SUMMARY_REQUEST, user_originated_turn_view, is_user_originated_turn)
    from clover_cli.session_export import render_sessions_export
    from agent.title_generator import _is_real_user_turn, is_titleable_user_message
    from clover_cli.session_recap import _count_visible_turns, _latest_user_prompt
    db = SessionDB(tmp_path / 'r3.db')
    sid = 'r3'
    agent = make_agent(provider, session_db=db, session_id=sid)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    provider.queue[:] = [tool('todo', TODO, f'c{i}') for i in range(1, 20)]
    provider.before = lambda n, req: (not req.get('tools')) and provider.queue.insert(0, text('Here is the work so far.'))
    r = agent.run_conversation('big job', conversation_history=[], task_id='t-' + sid)
    provider.before = None
    assert r['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON
    # add legacy rows the way 71b27a31 persisted them (no display kind) + a normal second turn
    db.append_messages_batch(sid, [
        {'role': 'user', 'content': LEGACY_TIME_LIMIT_SUMMARY_REQUEST},
        {'role': 'assistant', 'content': 'legacy summary'},
        {'role': 'user', 'content': MAX_ITERATIONS_SUMMARY_REQUEST},
        {'role': 'assistant', 'content': 'old max summary'},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'second real ask'}]},
        {'role': 'assistant', 'content': 'second answer'},
    ])
    rows = db.get_messages(sid)
    session = {'id': sid, 'messages': rows}
    md = render_sessions_export([session], fmt='markdown')
    user_sections = [blk.strip().splitlines()[0] if blk.strip() else '' for blk in md.split('## User')[1:]]
    user_bodies = [blk.split('\n## ')[0].strip() for blk in md.split('## User')[1:]]
    fact('MD user sections', len(user_bodies), [b[:40] for b in user_bodies])
    assert len(user_bodies) == 2, user_bodies
    assert user_bodies[0].endswith('big job') or 'big job' in user_bodies[0]
    assert 'second real ask' in user_bodies[1]
    for lit in (TIME_LIMIT_SUMMARY_REQUEST, LEGACY_TIME_LIMIT_SUMMARY_REQUEST, MAX_ITERATIONS_SUMMARY_REQUEST):
        assert lit not in md
    assert 'Here is the work so far.' in md and 'legacy summary' in md
    for fmt in ('jsonl',):
        raw = render_sessions_export([session], fmt=fmt)
        fact('RAW export keeps rows', fmt, TIME_LIMIT_SUMMARY_REQUEST in raw, LEGACY_TIME_LIMIT_SUMMARY_REQUEST in raw)
    for fmt in ('markdown', 'jsonl'):
        up = render_sessions_export([session], fmt=fmt, only='user-prompts')
        assert LEGACY_TIME_LIMIT_SUMMARY_REQUEST not in up and TIME_LIMIT_SUMMARY_REQUEST not in up
        assert 'big job' in up and 'second real ask' in up
    leg = next(m for m in rows if m.get('content') == LEGACY_TIME_LIMIT_SUMMARY_REQUEST)
    fact('LEGACY row display_kind', leg.get('display_kind'))
    assert user_originated_turn_view(leg) is None and is_user_originated_turn(leg) is False
    assert _is_real_user_turn(leg) is False and is_titleable_user_message(LEGACY_TIME_LIMIT_SUMMARY_REQUEST) is False
    users, _, _ = _count_visible_turns(rows)
    fact('RECAP users', users, 'last', repr(_latest_user_prompt(rows)))
    assert users == 2
    # next turn after legacy rows: alternation + short reply
    agent2 = make_agent(provider, session_db=db, session_id=sid)
    provider.queue[:] = [text('next answer')]
    n = len(provider.captured)
    r2 = agent2.run_conversation('and then?', conversation_history=db.get_messages_as_conversation(sid), task_id='n-' + sid)
    assert r2['final_response'] == 'next answer'
    wire = provider.captured[n]['messages']
    roles = [m['role'] for m in wire]
    dup = [(a, b) for a, b in zip(roles, roles[1:]) if a == b and a != 'tool']
    foreign = sorted({k for m in wire for k in m if k not in {'role', 'content', 'tool_calls', 'tool_call_id', 'name'}})
    fact('LEGACY next-turn ROLES', roles[-8:], 'DUP', dup, 'FOREIGN', foreign)
    assert dup == [] and 'display_kind' not in foreign
    db.close()
