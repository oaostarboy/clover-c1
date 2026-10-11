"""Hand clearly big jobs to a helper up front; never talk about the time limit (C1.4 P1b).

Real ``AIAgent`` roots, the real prompt builder and conversation loop, a local
mock provider over HTTP and a fake monotonic clock.  Contract:

* A root agent that the foreground limit applies to, on a surface that can
  deliver a detached helper, with ``delegate_task`` available, is told in its
  system prompt to hand clearly big jobs to a helper at the start and to keep
  the chat free.  The decision is made once at prompt build, so the prompt is
  byte-stable for the life of the conversation.
* Everything else (subagents, cron, one-shot runs, limit off, no
  ``delegate_task``, API server) gets no such guidance.
* Nothing the user can read mentions the limit, a timeout or a failed attempt,
  and everything the model reads about the limit tells it not to.
"""
from __future__ import annotations

import re

import pytest

from agent import foreground_time_limit as fl
from tests.agent.test_foreground_time_limit_loop import (  # noqa: F401
    Provider,
    _limit_on,
    _write_limit,
    advance_per_tool_round,
    clock,
    make_agent,
    provider,
    text,
    tool,
    TODO,
)

HEADING = '# Keep the chat free'
# Words a user must never read about an internal limit.
LIMIT_WORDS = ('limit', 'timed out', 'time out', 'timeout', '5 minutes', 'five minutes')
BANNED = ('limit', 'timed out', 'time out', 'timeout', '5 minutes', 'five minutes',
          'error', 'failed', "couldn't", 'could not', 'exception')


@pytest.fixture(autouse=True)
def _unbind_session_context():
    yield
    import gateway.session_context as sc

    for var in sc._VAR_MAP.values():
        var.set(sc._UNSET)
    sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)


def _prompt(agent) -> str:
    return agent._build_system_prompt()


def _assert_no_banned(value: str) -> None:
    lowered = value.lower()
    for word in BANNED:
        assert word not in lowered, f'{word!r} leaked into user-visible text: {value!r}'


# ── system-prompt guidance: who gets it ────────────────────────────────────

@pytest.mark.parametrize('platform', ['telegram', 'discord', 'slack', 'cli', 'tui'])
def test_root_agent_with_the_limit_on_and_delegate_task_gets_the_guidance(provider, platform):
    agent = make_agent(provider, platform=platform)
    assert 'delegate_task' in agent.valid_tool_names
    assert HEADING in _prompt(agent)


def test_guidance_is_short_and_has_no_minute_numbers():
    from agent.prompt_builder import FOREGROUND_HANDOFF_GUIDANCE

    assert FOREGROUND_HANDOFF_GUIDANCE.startswith(HEADING)
    assert len(FOREGROUND_HANDOFF_GUIDANCE.split()) <= 90
    assert not re.search(r'\d', FOREGROUND_HANDOFF_GUIDANCE)
    lowered = FOREGROUND_HANDOFF_GUIDANCE.lower()
    assert 'delegate_task' in lowered
    assert 'never mention' in lowered


def test_guidance_sits_in_the_stable_part_of_the_prompt(provider):
    agent = make_agent(provider)
    parts = agent._build_system_prompt_parts()
    assert HEADING in parts['stable']


def test_a_subagent_gets_no_guidance(provider):
    agent = make_agent(provider, platform='subagent')
    agent._delegate_depth = 1
    agent._subagent_id = 'sa-1'
    assert HEADING not in _prompt(agent)


def test_a_delegated_child_context_gets_no_guidance(provider):
    from agent.delegation_context import delegated_child_context

    agent = make_agent(provider)
    with delegated_child_context():
        assert HEADING not in _prompt(agent)


def test_cron_gets_no_guidance(provider):
    from gateway.session_context import declare_stateless_channel

    declare_stateless_channel()
    agent = make_agent(provider, platform='cron')
    assert HEADING not in _prompt(agent)


@pytest.mark.parametrize('attr,value', [
    ('_delegation_checkpoint_exempt', 'oneshot'),
    ('_delegation_checkpoint_exempt', 'batch_runner'),
    ('_foreground_limit_exempt', 'single_query'),
    ('_foreground_limit_exempt', 'background'),
])
def test_oneshot_and_other_noninteractive_runs_get_no_guidance(provider, attr, value):
    agent = make_agent(provider, platform='cli')
    setattr(agent, attr, value)
    assert HEADING not in _prompt(agent)


@pytest.mark.parametrize('value', [0, None, False, 'off'])
def test_limit_off_gets_no_guidance(provider, value):
    _write_limit(value)
    agent = make_agent(provider)
    assert HEADING not in _prompt(agent)


def test_without_delegate_task_there_is_no_guidance(provider):
    agent = make_agent(provider, enabled_toolsets=['todo', 'file', 'terminal'])
    assert 'delegate_task' not in agent.valid_tool_names
    assert HEADING not in _prompt(agent)


def test_api_server_gets_no_guidance(provider):
    from gateway.session_context import declare_stateless_channel

    declare_stateless_channel()
    agent = make_agent(provider, platform='api_server')
    assert HEADING not in _prompt(agent)


def test_api_server_with_a_wake_id_still_gets_no_guidance(provider, monkeypatch):
    # The limit itself can apply here (a wake id makes a helper deliverable),
    # but that is a per-request fact; the prompt only follows the session's
    # surface, which is stateless.
    import gateway.session_context as sc

    monkeypatch.setenv('CLOVER_SESSION_ID', 'raw-sid-9')
    sc.set_session_vars(platform='api_server', chat_id='raw-sid-9',
                        session_key='raw-sid-9', session_id='raw-sid-9',
                        async_delivery=False)
    agent = make_agent(provider, platform='api_server')
    assert HEADING not in _prompt(agent)


def test_kanban_worker_gets_no_guidance(provider, monkeypatch):
    monkeypatch.setenv('CLOVER_KANBAN_TASK', 't-1')
    agent = make_agent(provider, platform='cli')
    assert HEADING not in _prompt(agent)


# ── cache safety ───────────────────────────────────────────────────────────

def _system_contents(requests: list[dict]) -> list[str]:
    out = []
    for req in requests:
        first = (req.get('messages') or [{}])[0]
        if first.get('role') == 'system':
            content = first.get('content')
            if isinstance(content, list):
                content = ''.join(p.get('text', '') for p in content if isinstance(p, dict))
            out.append(content)
    return out


def test_system_prompt_is_byte_identical_across_turns_of_one_conversation(clock, provider):
    agent = make_agent(provider)
    provider.queue[:] = [text('first answer'), text('second answer'), text('third answer')]

    first = agent.run_conversation('hello', conversation_history=[], task_id='bs-1')
    # Config and surface change between turns must not reach the cached prefix.
    _write_limit(0)
    second = agent.run_conversation(
        'and another', conversation_history=first['messages'], task_id='bs-2')
    third = agent.run_conversation(
        'one more', conversation_history=second['messages'], task_id='bs-3')

    system = _system_contents(provider.captured)
    assert len(system) >= 3
    assert HEADING in system[0]
    assert all(s == system[0] for s in system), 'system prompt changed mid-conversation'
    assert third['final_response'] == 'third answer'


def test_guidance_is_the_only_difference_the_limit_makes_to_the_stable_prompt(provider):
    from agent.prompt_builder import FOREGROUND_HANDOFF_GUIDANCE

    on = make_agent(provider)._build_system_prompt_parts()['stable']
    _write_limit(0)
    off = make_agent(provider)._build_system_prompt_parts()['stable']
    assert FOREGROUND_HANDOFF_GUIDANCE in on
    assert on.replace('\n\n' + FOREGROUND_HANDOFF_GUIDANCE, '') == off


# ── model-facing limit text tells the model to keep quiet ──────────────────

@pytest.mark.parametrize('can_delegate', [True, False])
def test_notice_tells_the_model_not_to_mention_the_limit(can_delegate):
    notice = fl.notice_text(240.0, 300.0, can_delegate=can_delegate)
    assert fl.NOTICE_MARKER in notice
    assert 'do not mention' in notice.lower()


def test_notice_with_delegate_says_what_to_tell_the_user():
    notice = fl.notice_text(240.0, 300.0, can_delegate=True).lower()
    assert 'report back' in notice
    assert 'failed attempt' in notice or 'failed attempts' in notice


def test_notice_without_delegate_asks_for_the_best_answer_now():
    notice = fl.notice_text(240.0, 300.0, can_delegate=False).lower()
    assert 'best answer' in notice


@pytest.mark.parametrize('tools_enabled,delegate', [
    (['todo', 'delegation', 'file', 'terminal'], True),
    (['todo', 'file', 'terminal'], False),
])
def test_refusal_tells_the_model_not_to_mention_the_limit(
        clock, provider, tools_enabled, delegate):
    agent = make_agent(provider, enabled_toolsets=tools_enabled)
    assert ('delegate_task' in agent.valid_tool_names) is delegate
    fl.begin_turn(agent)
    clock.advance(301)

    for name, args in (('terminal', {'command': 'ls'}),
                       ('process', {'action': 'wait', 'session_id': 'p1'})):
        verdict = fl.refusal(agent, name, args)
        assert verdict is not None
        assert 'do not mention' in verdict.block_message.lower()
        assert 'do not mention' in verdict.block_result.lower()


# ── nothing the user can read mentions the limit ───────────────────────────

def _messages():
    return [{'role': 'user', 'content': 'big job'},
            {'role': 'assistant', 'content': 'working'}]


def test_hard_stop_text_the_user_can_read_never_mentions_a_limit(clock, provider, monkeypatch):
    from agent.chat_completion_helpers import handle_max_iterations

    agent = make_agent(provider)
    shown: list[str] = []
    monkeypatch.setattr(agent, '_safe_print', lambda *a, **k: shown.append(' '.join(map(str, a))))
    provider.queue[:] = [text(''), text('')]  # no summary either attempt

    final = handle_max_iterations(agent, _messages(), 3, reason='time_limit')

    assert final.strip()
    _assert_no_banned(final)
    assert shown, 'the hard stop printed nothing at all'
    for line in shown:
        _assert_no_banned(line)


@pytest.mark.parametrize('raised', [
    TimeoutError('request timed out'),
    RuntimeError('provider exploded'),
])
def test_hard_stop_error_fallback_shows_no_error_or_failure_talk(
        clock, provider, monkeypatch, raised):
    from agent.chat_completion_helpers import handle_max_iterations

    agent = make_agent(provider)
    shown: list[str] = []
    monkeypatch.setattr(agent, '_safe_print', lambda *a, **k: shown.append(' '.join(map(str, a))))

    def boom(**kwargs):
        raise raised

    monkeypatch.setattr(agent, '_ensure_primary_openai_client', boom)
    final = handle_max_iterations(agent, _messages(), 3, reason='time_limit')

    assert final.strip()
    assert str(raised) not in final
    _assert_no_banned(final)
    for line in shown:
        _assert_no_banned(line)


def test_hard_stop_request_to_the_model_keeps_mechanics_out_of_the_reply(clock, provider, monkeypatch):
    from agent.chat_completion_helpers import handle_max_iterations

    agent = make_agent(provider)
    monkeypatch.setattr(agent, '_safe_print', lambda *a, **k: None)
    provider.queue[:] = [text('Here is where this stands.')]
    messages = _messages()

    final = handle_max_iterations(agent, messages, 3, reason='time_limit')

    assert final == 'Here is where this stands.'
    request = provider.captured[-1]['messages'][-1]
    assert request['role'] == 'user'
    lowered = request['content'].lower()
    assert 'internal mechanics' in lowered
    assert 'without calling' in lowered or 'no more tools' in lowered


def test_hard_stop_request_reads_neutrally_if_it_is_ever_shown():
    from agent.context_compressor import TIME_LIMIT_SUMMARY_REQUEST

    _assert_no_banned(TIME_LIMIT_SUMMARY_REQUEST)
    assert 'attempt' not in TIME_LIMIT_SUMMARY_REQUEST.lower()


def test_hard_stop_request_is_still_recognised_as_internal_scaffolding(clock, provider, monkeypatch):
    from agent.chat_completion_helpers import handle_max_iterations
    from agent.context_compressor import ContextCompressor

    agent = make_agent(provider)
    monkeypatch.setattr(agent, '_safe_print', lambda *a, **k: None)
    provider.queue[:] = [text('stand-by')]
    messages = _messages()
    handle_max_iterations(agent, messages, 3, reason='time_limit')

    request = next(m for m in messages if m['role'] == 'user' and m['content'] != 'big job')
    assert ContextCompressor._is_synthetic_compression_user_turn(request)


def test_iteration_limit_request_is_unchanged(clock, provider, monkeypatch):
    # Only the time-limit hard stop changed; the max-iterations path keeps the
    # shared constant the compaction recognizers already know.
    from agent.chat_completion_helpers import handle_max_iterations
    from agent.context_compressor import MAX_ITERATIONS_SUMMARY_REQUEST

    agent = make_agent(provider)
    monkeypatch.setattr(agent, '_safe_print', lambda *a, **k: None)
    provider.queue[:] = [text('stand-by')]
    messages = _messages()
    handle_max_iterations(agent, messages, 3)

    assert messages[-2]['content'] == MAX_ITERATIONS_SUMMARY_REQUEST


def test_real_hard_stop_turn_ends_quietly(clock, provider):
    """End to end: the loop reaches the hard stop; the model is told to keep
    quiet and the user's final text, even with no summary, names no limit."""
    agent = make_agent(provider)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    provider.queue[:] = [tool('todo', TODO, f'c{i}') for i in range(1, 20)]

    def before(n, req):
        if not req.get('tools'):  # the final no-tools call: give it nothing
            provider.queue[:0] = [text(''), text('')]

    provider.before = before
    result = agent.run_conversation('big job', conversation_history=[], task_id='hs-quiet')

    assert result['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON
    assert result['final_response'].strip()
    _assert_no_banned(result['final_response'])
    final_request = next(r for r in reversed(provider.captured) if not r.get('tools'))
    assert 'internal mechanics' in final_request['messages'][-1]['content'].lower()


def test_codex_deadline_text_never_mentions_a_limit(clock, provider, monkeypatch):
    from tests.agent.test_p1_astra_review import _codex_agent

    agent, client = _codex_agent(provider, clock, monkeypatch, slow=True)
    # Nothing from Codex survives the interruption, so the fallback text is used.
    result = agent.run_conversation('long interactive task', task_id='codex-quiet')

    assert result.get('turn_exit_reason') == fl.HARD_STOP_EXIT_REASON, result
    _assert_no_banned(result['final_response'])


# ── the synthetic summary request is never a human turn anywhere ────────────

SUMMARY_REQUESTS = ['TIME_LIMIT_SUMMARY_REQUEST', 'MAX_ITERATIONS_SUMMARY_REQUEST']


def _summary_request(name: str) -> str:
    import agent.context_compressor as cc

    return getattr(cc, name)


@pytest.mark.parametrize('name', SUMMARY_REQUESTS)
def test_export_of_user_prompts_skips_the_runtime_summary_request(name):
    from clover_cli.session_export import export_record_count, iter_user_prompt_records

    session = {'id': 's1', 'messages': [
        {'id': 1, 'role': 'user', 'content': 'big job'},
        {'id': 2, 'role': 'assistant', 'content': 'working'},
        {'id': 3, 'role': 'user', 'content': _summary_request(name)},
        {'id': 4, 'role': 'assistant', 'content': 'Here is where this stands.'},
    ]}

    records = list(iter_user_prompt_records([session]))
    assert [r['text'] for r in records] == ['big job']
    assert [r['index'] for r in records] == [1]
    assert export_record_count([session], only='user-prompts') == (1, 'prompt')


@pytest.mark.parametrize('name', SUMMARY_REQUESTS)
def test_title_generation_does_not_treat_the_summary_request_as_a_turn(name):
    from agent.title_generator import _is_real_user_turn, is_titleable_user_message

    content = _summary_request(name)
    assert not _is_real_user_turn({'role': 'user', 'content': content})
    assert not is_titleable_user_message(content)
    assert _is_real_user_turn({'role': 'user', 'content': 'plan my trip'})


@pytest.mark.parametrize('name', SUMMARY_REQUESTS)
def test_session_recap_never_shows_the_summary_request_as_the_last_ask(name):
    from clover_cli.session_recap import build_recap

    recap = build_recap([
        {'role': 'user', 'content': 'rank the best indie horror games'},
        {'role': 'assistant', 'content': 'working on it'},
        {'role': 'user', 'content': _summary_request(name)},
        {'role': 'assistant', 'content': 'Here is where this stands.'},
    ])

    assert 'rank the best indie horror games' in recap
    assert _summary_request(name)[:30] not in recap
    assert '1 user turn' in recap


# ── the summary request is machine scaffolding, hidden from human display ───

def _hard_stop_conversation(clock, provider, db, session_id):
    agent = make_agent(provider, session_db=db, session_id=session_id)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    provider.queue[:] = [tool('todo', TODO, f'c{i}') for i in range(1, 20)]

    def before(n, req):
        if not req.get('tools'):
            provider.queue[:0] = [text('Here is the work so far.')]

    provider.before = before
    result = agent.run_conversation('big job', conversation_history=[], task_id='hs-hidden')
    provider.before = None
    assert result['turn_exit_reason'] == fl.HARD_STOP_EXIT_REASON
    return agent, result


def test_hard_stop_request_is_persisted_hidden_and_absent_from_human_projections(
        clock, provider, tmp_path):
    from agent.context_compressor import TIME_LIMIT_SUMMARY_REQUEST
    from agent.context_compressor import is_user_originated_turn
    from clover_state import SessionDB
    from tui_gateway.server import _history_to_messages

    db = SessionDB(tmp_path / 'hidden.db')
    _hard_stop_conversation(clock, provider, db, 'hidden')

    rows = [m for m in db.get_messages('hidden') if m['content'] == TIME_LIMIT_SUMMARY_REQUEST]
    assert len(rows) == 1 and rows[0]['role'] == 'user'
    assert rows[0]['display_kind'] == 'hidden'

    model_history, display_history = db.get_resume_conversations('hidden')
    # what a TUI / desktop resume renders as bubbles
    shown = _history_to_messages(display_history)
    assert [m for m in shown if TIME_LIMIT_SUMMARY_REQUEST in str(m.get('text') or m.get('content'))] == []
    assert any('big job' in str(m.get('text') or m.get('content')) for m in shown)
    assert not any(is_user_originated_turn(m) and m.get('content') == TIME_LIMIT_SUMMARY_REQUEST
                   for m in display_history)
    db.close()


def test_provider_history_still_carries_the_summary_request_unchanged(
        clock, provider, tmp_path):
    from agent.context_compressor import TIME_LIMIT_SUMMARY_REQUEST
    from clover_state import SessionDB

    db = SessionDB(tmp_path / 'wire.db')
    _hard_stop_conversation(clock, provider, db, 'wire')

    # the toolless summary call itself: no Clover display field on the wire
    final_call = next(r for r in reversed(provider.captured) if not r.get('tools'))
    assert final_call['messages'][-1] == {'role': 'user', 'content': TIME_LIMIT_SUMMARY_REQUEST}
    for message in final_call['messages']:
        assert 'display_kind' not in message and 'display_metadata' not in message

    # the next turn replays it, in place, as an ordinary user message
    agent = make_agent(provider, session_db=db, session_id='wire')
    provider.queue[:] = [text('next answer')]
    n_before = len(provider.captured)
    agent.run_conversation(
        'what now?', conversation_history=db.get_messages_as_conversation('wire'))
    replay = provider.captured[n_before]['messages']
    contents = [m.get('content') for m in replay if m.get('role') == 'user']
    assert TIME_LIMIT_SUMMARY_REQUEST in contents
    i = contents.index(TIME_LIMIT_SUMMARY_REQUEST)
    after = [m for m in replay if m.get('role') == 'user'][i]
    assert set(after) == {'role', 'content'}
    roles = [m['role'] for m in replay]
    assert 'user' not in [a for a, b in zip(roles, roles[1:]) if a == b]  # alternation intact
    db.close()


def test_max_iterations_request_row_is_hidden_the_same_way(clock, provider, monkeypatch):
    from agent.chat_completion_helpers import handle_max_iterations
    from agent.context_compressor import MAX_ITERATIONS_SUMMARY_REQUEST

    agent = make_agent(provider)
    monkeypatch.setattr(agent, '_safe_print', lambda *a, **k: None)
    provider.queue[:] = [text('stand-by')]
    messages = _messages()
    handle_max_iterations(agent, messages, 3)

    row = next(m for m in messages if m.get('content') == MAX_ITERATIONS_SUMMARY_REQUEST)
    assert row['role'] == 'user' and row['display_kind'] == 'hidden'
    sent = provider.captured[-1]['messages'][-1]
    assert sent == {'role': 'user', 'content': MAX_ITERATIONS_SUMMARY_REQUEST}


# ── human-readable export shows only what a person wrote and read ───────────

def test_markdown_export_does_not_attribute_the_summary_request_to_the_user(
        clock, provider, tmp_path):
    from agent.context_compressor import TIME_LIMIT_SUMMARY_REQUEST
    from clover_cli.session_export import render_sessions_export
    from clover_state import SessionDB

    with SessionDB(tmp_path / 'md.db') as db:
        _hard_stop_conversation(clock, provider, db, 'md')
        exported = db.export_session('md')

        markdown = render_sessions_export([exported], fmt='markdown')
        assert TIME_LIMIT_SUMMARY_REQUEST not in markdown
        assert 'big job' in markdown and '## User' in markdown
        assert 'Here is the work so far.' in markdown

        # the raw archive keeps every row, hidden or not
        archive = render_sessions_export([exported], fmt='jsonl')
        assert TIME_LIMIT_SUMMARY_REQUEST in archive


def test_legacy_and_typed_synthetic_rows_never_render_under_user(tmp_path):
    from agent.context_compressor import (
        LEGACY_TIME_LIMIT_SUMMARY_REQUEST,
        MAX_ITERATIONS_SUMMARY_REQUEST,
    )
    from clover_cli.session_export import render_sessions_export
    from clover_state import SessionDB

    with SessionDB(tmp_path / 'legacy-md.db') as db:
        db.create_session('legacy', source='cli')
        db.append_message('legacy', 'user', 'real ask')
        db.append_message('legacy', 'assistant', 'working')
        db.append_message('legacy', 'user', LEGACY_TIME_LIMIT_SUMMARY_REQUEST, display_kind=None)
        db.append_message('legacy', 'assistant', 'progress one')
        db.append_message('legacy', 'user', MAX_ITERATIONS_SUMMARY_REQUEST, display_kind=None)
        db.append_message('legacy', 'assistant', 'progress two')
        db.append_message('legacy', 'user', 'helper finished', display_kind='internal_notification')
        db.append_message('legacy', 'assistant', 'noted')

        markdown = render_sessions_export([db.export_session('legacy')], fmt='markdown')

    assert 'real ask' in markdown and 'progress one' in markdown and 'progress two' in markdown
    assert LEGACY_TIME_LIMIT_SUMMARY_REQUEST not in markdown
    assert MAX_ITERATIONS_SUMMARY_REQUEST not in markdown
    assert 'helper finished' not in markdown


def test_previous_hard_stop_wording_is_still_scaffolding_everywhere():
    from agent.context_compressor import (
        LEGACY_TIME_LIMIT_SUMMARY_REQUEST,
        ContextCompressor,
        is_user_originated_turn,
    )
    from agent.title_generator import _is_real_user_turn, is_titleable_user_message

    row = {'role': 'user', 'content': LEGACY_TIME_LIMIT_SUMMARY_REQUEST}
    assert ContextCompressor._is_synthetic_compression_user_turn(row)
    assert not is_user_originated_turn(row)
    assert not _is_real_user_turn(row)
    assert not is_titleable_user_message(LEGACY_TIME_LIMIT_SUMMARY_REQUEST)


# ── refused tools never put the limit on the activity feed ──────────────────

REFUSED = [
    ('web_search', {'query': 'x'}),
    ('browser_navigate', {'url': 'https://example.com'}),
    ('process', {'action': 'wait', 'session_id': 'p1'}),
    ('terminal', {'command': 'sleep 1'}),
    ('execute_code', {'code': 'print(1)'}),
]


def _refused_agent(clock, platform='cli', **kw):
    from tests.agent.test_foreground_time_limit import _agent

    agent = _agent(platform=platform, **kw)
    fl.begin_turn(agent)
    clock.advance(301)
    return agent


def _assert_refused_for_the_model(result):
    # the model still reads the full mechanics, untouched (web tools wrap it in
    # an untrusted-content block, so compare on text)
    import json

    body = result if isinstance(result, str) else json.dumps(result)
    lowered = body.lower()
    assert fl.BLOCK_CODE in body, body
    assert 'time limit' in lowered
    assert 'do not mention' in lowered


@pytest.mark.parametrize('name,args', REFUSED)
def test_cli_activity_feed_shows_a_neutral_label_for_a_refused_tool(
        clock, monkeypatch, name, args):
    from tests.agent.test_foreground_time_limit import run_batch

    agent = _refused_agent(clock, platform='cli')
    assert agent._should_emit_quiet_tool_messages()
    lines: list[str] = []
    monkeypatch.setattr(agent, '_safe_print', lambda *a, **k: lines.append(' '.join(map(str, a))))

    [result] = run_batch(agent, [(name, args)])

    _assert_refused_for_the_model(result)
    assert lines, 'the feed printed nothing for the refused tool'
    for line in lines:
        _assert_no_banned(line)
    assert any('skipped' in line.lower() for line in lines)


def test_cli_activity_feed_is_neutral_for_a_concurrent_batch_of_refused_tools(
        clock, monkeypatch):
    from tests.agent.test_foreground_time_limit import run_batch

    agent = _refused_agent(clock, platform='cli')
    lines: list[str] = []
    monkeypatch.setattr(agent, '_safe_print', lambda *a, **k: lines.append(' '.join(map(str, a))))

    results = run_batch(agent, REFUSED[:3])

    assert len(results) == 3
    for result in results:
        _assert_refused_for_the_model(result)
    assert len(lines) >= 3
    for line in lines:
        _assert_no_banned(line)


@pytest.mark.parametrize('name,args', REFUSED)
def test_ui_callbacks_get_a_neutral_result_for_a_refused_tool(clock, name, args):
    import json

    from tests.agent.test_foreground_time_limit import run_batch

    complete: list[tuple] = []
    progress: list[tuple] = []
    agent = _refused_agent(clock, platform='telegram')
    agent.tool_complete_callback = lambda *a: complete.append(a)
    agent.tool_progress_callback = lambda *a, **k: progress.append((a, k))

    [result] = run_batch(agent, [(name, args)])
    _assert_refused_for_the_model(result)

    shown = json.dumps([complete, progress], default=str).lower()
    for word in LIMIT_WORDS:
        assert word not in shown, f'{word!r} reached a UI callback: {shown[:300]}'


@pytest.mark.parametrize('name,args', REFUSED)
def test_tui_tool_complete_event_is_neutral_for_a_refused_tool(clock, name, args):
    import json

    import tui_gateway.server as server

    agent = _refused_agent(clock, platform='tui')
    raw = fl.refusal(agent, name, args).block_result
    assert 'time limit' in raw.lower()  # what the model receives

    emitted: list[tuple] = []
    original_emit = server._emit
    server._emit = lambda *a, **k: emitted.append((a, k))
    try:
        server._on_tool_complete('sid-1', 'call-x', name, args, raw)
    finally:
        server._emit = original_emit

    assert emitted, 'tool.complete was never emitted'
    shown = json.dumps(emitted, default=str).lower()
    for word in LIMIT_WORDS:
        assert word not in shown, f'{word!r} in tool.complete: {shown[:300]}'
    assert 'skipped' in shown


def test_display_result_leaves_everything_else_alone():
    ok = '{"output": "fine", "exit_code": 0}'
    other_error = '{"error": "file not found", "error_type": "not_found"}'
    assert fl.display_result(ok) == ok
    assert fl.display_result(other_error) == other_error
    assert fl.display_result('plain text') == 'plain text'
    assert fl.display_result(None) is None
