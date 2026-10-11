import pytest
from tests.agent.test_foreground_time_limit_loop import provider, clock, _limit_on, make_agent, text, tool, TODO, advance_per_tool_round
from tests.agent.test_handoff_upfront import _unbind_session_context, _system_contents
from agent.context_compressor import TIME_LIMIT_SUMMARY_REQUEST, ContextCompressor, user_originated_turn_view

@pytest.mark.parametrize('initial,later', [(True,False),(False,True)])
def test_restart_preserves_prompt(provider, tmp_path, initial, later):
    from clover_state import SessionDB
    import gateway.session_context as sc
    db = SessionDB(tmp_path / 'restart.db')
    sc.set_session_vars(platform='telegram', chat_id='chat', session_id='stable', session_key='stable', async_delivery=initial)
    a = make_agent(provider, session_db=db, session_id='stable')
    provider.queue[:] = [text('first'), text('second')]
    first = a.run_conversation('hello', conversation_history=[])
    sc.set_session_vars(platform='telegram', chat_id='chat', session_id='stable', session_key='stable', async_delivery=later)
    b = make_agent(provider, session_db=db, session_id='stable')
    second = b.run_conversation('hello again', conversation_history=db.get_messages_as_conversation('stable'))
    prompts = _system_contents(provider.captured)
    assert len(prompts) == 2
    assert ('# Choosing how to work' in prompts[0]) == initial
    assert prompts[0] == prompts[1]
    assert second['final_response'] == 'second'
    db.close()

def test_error_timeout_is_not_user_visible(provider, monkeypatch):
    from agent.chat_completion_helpers import handle_max_iterations
    a = make_agent(provider)
    def boom(**kwargs):
        raise TimeoutError('request timed out')
    monkeypatch.setattr(a, '_ensure_primary_openai_client', boom)
    result = handle_max_iterations(a, [{'role':'user','content':'big job'}], 3, reason='time_limit')
    assert 'timed out' not in result, result

def test_actual_hard_stop_export_excludes_synthetic_user(provider, clock, tmp_path):
    from clover_state import SessionDB
    from clover_cli.session_export import iter_user_prompt_records
    db = SessionDB(tmp_path / 'export.db')
    a = make_agent(provider, session_db=db, session_id='export')
    advance_per_tool_round(a, clock, 100, after_limit=5)
    provider.queue[:] = [tool('todo', TODO, f'c{i}') for i in range(1,20)]
    def before(n, req):
        if not req.get('tools'):
            provider.queue[:0] = [text('Here is the work so far.')]
    provider.before = before
    result = a.run_conversation('big job', conversation_history=[])
    assert result['turn_exit_reason'] == 'foreground_time_limit_hard_stop'
    exported = db.export_session('export')
    records = list(iter_user_prompt_records([exported]))
    assert [r['text'] for r in records] == ['big job'], records
    db.close()

def test_summary_recognizers():
    from agent.title_generator import _is_real_user_turn
    message = {'role':'user', 'content':TIME_LIMIT_SUMMARY_REQUEST}
    assert ContextCompressor._is_synthetic_compression_user_turn(message)
    assert user_originated_turn_view(message) is None
    assert not _is_real_user_turn(message)
