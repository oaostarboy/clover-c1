import copy
import re
from pathlib import Path
from types import MethodType
import pytest
from tests.agent.test_foreground_time_limit_loop import clock, _limit_on
from tests.agent.test_handoff_upfront import _unbind_session_context
from tests.agent.test_foreground_time_limit import _agent, run_batch
from tests.agent.test_p1br3_astra_probes import test_sequential_preview_never_prints_refusal

@pytest.mark.parametrize('verbose', [False, True])
@pytest.mark.parametrize('payload', [
    'ordinary successful output',
    {'ok': True, 'count': 2},
    {'_multimodal': True, 'content': [{'type': 'text', 'text': 'screen captured'}], 'text_summary': 'screen captured'},
], ids=['string', 'dict', 'multimodal'])
def test_normal_preview_unchanged(monkeypatch, capsys, verbose, payload):
    import agent.tool_executor as te
    old_globals = dict(te.__dict__)
    exec(compile(Path(__file__).with_name('p1br4_old_executor_source.txt').read_text(), '35147590/agent/tool_executor.py', 'exec'), old_globals)
    outputs = []
    for executor in (old_globals['execute_tool_calls_sequential'], te.execute_tool_calls_sequential):
        agent = _agent(platform='cli', quiet_mode=False, tool_progress_mode='all', verbose_logging=verbose)
        agent._execute_tool_calls = MethodType(executor, agent)
        monkeypatch.setattr('run_agent.handle_function_call', lambda *a, **kw: copy.deepcopy(payload))
        capsys.readouterr()
        error = None
        try:
            run_batch(agent, [('terminal', {'command': 'printf normal'})])
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
        shown = capsys.readouterr().out
        start = shown.index('Tool 1 completed')
        preview = re.sub(r'completed in [0-9.]+s', 'completed in <duration>s', shown[start:])
        outputs.append({'preview': preview, 'error': error})
    assert outputs[0] == outputs[1], {'before': outputs[0], 'after': outputs[1]}
