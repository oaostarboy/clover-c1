import pytest
from tests.agent.test_foreground_time_limit_loop import provider, clock, _limit_on, make_agent, text
from tests.agent.test_handoff_upfront import _unbind_session_context, _hard_stop_conversation
from clover_state import SessionDB
from agent.context_compressor import TIME_LIMIT_SUMMARY_REQUEST, MAX_ITERATIONS_SUMMARY_REQUEST, ContextCompressor, user_originated_turn_view
from clover_cli.session_export import render_sessions_export, iter_user_prompt_records

OLD = ("Time to wrap up this reply. Please give a final response summarizing "
       "what you've found and accomplished so far and what is left, without "
       "calling any more tools. Do not mention time limits, timeouts or failed "
       "attempts to the user; just say plainly where things stand.")


def test_actual_hard_stop_full_markdown_is_not_user_authored(clock, provider, tmp_path):
    with SessionDB(tmp_path / 'markdown.db') as db:
        _hard_stop_conversation(clock, provider, db, 'markdown')
        exported = db.export_session('markdown')
        rows = [m for m in exported['messages'] if m.get('content') == TIME_LIMIT_SUMMARY_REQUEST]
        assert len(rows) == 1 and rows[0]['display_kind'] == 'hidden'
        rendered = render_sessions_export([exported], fmt='markdown')
        assert TIME_LIMIT_SUMMARY_REQUEST not in rendered, rendered


@pytest.mark.parametrize('content', [OLD, MAX_ITERATIONS_SUMMARY_REQUEST])
def test_legacy_none_row_not_exported_as_user(tmp_path, content):
    with SessionDB(tmp_path / 'legacy.db') as db:
        db.create_session('legacy', source='cli')
        db.append_message('legacy', 'user', 'real ask')
        db.append_message('legacy', 'assistant', 'working')
        db.append_message('legacy', 'user', content, display_kind=None)
        db.append_message('legacy', 'assistant', 'progress')
        records = list(iter_user_prompt_records([db.export_session('legacy')]))
        assert [r['text'] for r in records] == ['real ask'], records


@pytest.mark.parametrize('command', ['retry', 'undo'])
def test_real_cli_rewind_after_hard_stop(clock, provider, tmp_path, command):
    from tests.cli.test_cli_init import _make_cli
    db = SessionDB(tmp_path / 'rewind.db')
    _hard_stop_conversation(clock, provider, db, 'rewind')
    cli = _make_cli()
    cli._session_db.close()
    cli._session_db = db
    cli.session_id = 'rewind'
    cli.conversation_history = db.get_messages_as_conversation('rewind')
    if command == 'retry':
        assert cli.retry_last() == 'big job'
    else:
        cli.undo_last(prefill=False)
    assert db.get_messages('rewind') == []
    assert cli.conversation_history == []
    db.close()


@pytest.mark.parametrize('summary_mode', ['normal', 'exception'])
def test_db_counts_search_compression_and_next_short_reply(clock, provider, tmp_path, monkeypatch, summary_mode):
    from clover_cli.session_recap import _count_visible_turns, _latest_user_prompt
    from agent.title_generator import _is_real_user_turn
    if summary_mode == 'exception':
        from run_agent import AIAgent
        original = AIAgent._ensure_primary_openai_client
        def summary_error(self, *args, **kwargs):
            if kwargs.get('reason') == 'iteration_limit_summary':
                raise TimeoutError('request timed out')
            return original(self, *args, **kwargs)
        monkeypatch.setattr(AIAgent, '_ensure_primary_openai_client', summary_error)
    with SessionDB(tmp_path / 'surface.db') as db:
        _, stopped = _hard_stop_conversation(clock, provider, db, 'surface')
        assert not any(word in stopped['final_response'].lower() for word in ['error', 'timed out', 'failed', 'limit'])
        model, display = db.get_resume_conversations('surface')
        assert db.get_resume_message_count('surface') == len(display)
        assert _count_visible_turns(display)[0] == 1
        assert _latest_user_prompt(display) == 'big job'
        request = next(m for m in model if m.get('content') == TIME_LIMIT_SUMMARY_REQUEST)
        assert user_originated_turn_view(request) is None
        assert ContextCompressor._is_synthetic_compression_user_turn(request)
        assert not ContextCompressor._is_actionable_user_turn(request)
        assert not _is_real_user_turn(request)
        assert db.search_messages('"big job"')
        matches = db.search_messages('"Pause here"')
        assert matches  # Physical transcript search remains searchable, not a human-turn view.
        agent = make_agent(provider, session_db=db, session_id='surface')
        provider.queue[:] = [text('yes')]
        start = len(provider.captured)
        result = agent.run_conversation('quick answer?', conversation_history=model)
        assert result['final_response'] == 'yes'
        for req in provider.captured[start:]:
            for msg in req['messages']:
                assert 'display_kind' not in msg and 'display_metadata' not in msg
            roles = [m['role'] for m in req['messages']]
            assert not any(a == b == 'user' for a, b in zip(roles, roles[1:]))
