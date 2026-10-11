import pytest
from tests.agent.test_foreground_time_limit_loop import clock, _limit_on
from tests.agent.test_handoff_upfront import _unbind_session_context, _refused_agent, _assert_refused_for_the_model
from tests.agent.test_foreground_time_limit import run_batch


@pytest.mark.parametrize('verbose', [False, True])
def test_sequential_preview_never_prints_refusal(clock, capsys, verbose):
    agent = _refused_agent(clock, platform='cli', quiet_mode=False,
                           tool_progress_mode='all', verbose_logging=verbose)
    capsys.readouterr()
    results = run_batch(agent, [('terminal', {'command': 'sleep 1'})])
    _assert_refused_for_the_model(results[0])
    shown = capsys.readouterr().out
    assert 'Tool 1 completed' in shown, shown
    assert 'The time limit is reached' not in shown, shown


@pytest.mark.parametrize('verbose', [False, True])
def test_concurrent_preview_never_prints_refusal(clock, capsys, verbose):
    from types import MethodType
    from agent.tool_executor import execute_tool_calls_concurrent
    agent = _refused_agent(clock, platform='cli', quiet_mode=False,
                           tool_progress_mode='all', verbose_logging=verbose)
    agent._execute_tool_calls = MethodType(execute_tool_calls_concurrent, agent)
    capsys.readouterr()
    results = run_batch(agent, [('web_search', {'query': 'x'}),
                                ('browser_navigate', {'url': 'https://example.com'})])
    for result in results:
        _assert_refused_for_the_model(result)
    shown = capsys.readouterr().out
    assert 'The time limit is reached' not in shown, shown
