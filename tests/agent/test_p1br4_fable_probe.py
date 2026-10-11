"""Round-4 probe (C1.4 P1b, b9769bdc): sequential CLI preview -- refusal leak + normal previews."""
from __future__ import annotations
import io, json, contextlib
import pytest
from agent import foreground_time_limit as fl
from tests.agent.test_foreground_time_limit_loop import (  # noqa: F401
    Provider, _limit_on, _write_limit, advance_per_tool_round, clock, make_agent, provider, text, tool, TODO,
)

FACTS = '/results/p1br4-facts.txt'
LIMIT_WORDS = ('time limit', 'timeout', 'timed out', 'limit is reached', 'wrap up', 'was not run', 'working longer')
MM = {'_multimodal': True,
      'content': [{'type': 'text', 'text': 'MM-TEXT-P1BR4'},
                  {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + 'QUJD' * 400}}],
      'text_summary': 'MM SUMMARY P1BR4 ' + 'z' * 300}
BIG = ('big-raw-p1br4 ' * 12000)  # ~168K chars, above the default persist threshold


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


def _cli(provider, **kw):
    return make_agent(provider, platform='cli', quiet_mode=False, tool_progress_mode='all', **kw)


def _tool_contents(r):
    return [m.get('content') for m in r['messages'] if m.get('role') == 'tool']


def _completed_lines(out):
    return [l for l in out.splitlines() if 'Tool ' in l and 'completed in' in l]


def _run_refused(clock, provider, **kw):
    """Past the limit: ONE terminal call -> sequential path -> refused."""
    agent = _cli(provider, **kw)
    advance_per_tool_round(agent, clock, 100, after_limit=5)
    provider.queue[:] = [tool('todo', TODO, f'c{i}') for i in range(1, 5)]
    provider.queue += [tool('terminal', {'command': 'sleep 9'}, 't1'), text('Done so far.'), text('Done so far.')]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = agent.run_conversation('big job', conversation_history=[], task_id='t-r4')
    return agent, r, buf.getvalue()


@pytest.mark.parametrize('verbose', [False, True])
def test_seq_refusal_preview_no_leak(clock, provider, verbose):
    agent, r, out = _run_refused(clock, provider, verbose_logging=verbose)
    model_refusals = [c for c in _tool_contents(r) if isinstance(c, str) and fl.BLOCK_CODE in c]
    lines = _completed_lines(out)
    fact('REFUSED verbose=%s' % verbose, 'model_refusals', len(model_refusals), 'lines', [l[:160] for l in lines][-3:],
         'result_lines', [l[:160] for l in out.splitlines() if 'Result:' in l][-2:])
    assert model_refusals, 'refusal never reached the model-facing tool message'
    assert any('skipped' in l for l in out.splitlines()), out
    assert leaks(out) == [], [l for l in out.splitlines() if leaks(l)]


@pytest.mark.parametrize('verbose', [False, True])
def test_probe_sensitivity_identity_display_leaks(clock, provider, monkeypatch, verbose):
    """Prove the probe sees the leak when display_result is a no-op (the fixed line is load-bearing)."""
    import agent.tool_executor as te
    monkeypatch.setattr(te, 'display_result', lambda x: x)
    agent, r, out = _run_refused(clock, provider, verbose_logging=verbose)
    fact('SENSITIVITY verbose=%s' % verbose, leaks(out), [l[:120] for l in out.splitlines() if leaks(l)][:2])
    assert leaks(out), 'probe cannot see the leak -> probe is not exercising the sequential preview'


def _run_normal(provider, calls, **kw):
    agent = _cli(provider, **kw)
    provider.queue[:] = [tool(n, a, f't{i}') for i, (n, a) in enumerate(calls)] + [text('done')]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = agent.run_conversation('small job', conversation_history=[], task_id='t-r4n')
    return agent, r, buf.getvalue()


def _old_short(agent, fr):
    s = fr if isinstance(fr, str) else str(fr)
    return s[:agent.log_prefix_chars] + '...' if len(s) > agent.log_prefix_chars else s


@pytest.mark.parametrize('verbose', [False, True])
def test_normal_string_preview_unchanged(provider, verbose):
    agent, r, out = _run_normal(provider, [('terminal', {'command': 'echo hello-p1br4-marker'})], verbose_logging=verbose)
    fr = _tool_contents(r)[0]
    assert isinstance(fr, str) and 'hello-p1br4-marker' in fr
    assert fl.BLOCK_CODE not in fr and 'skipped' not in out
    lines = _completed_lines(out)
    fact('NORMAL-STR verbose=%s' % verbose, 'n_lines', len(lines), repr(lines[0][:200]) if lines else None)
    assert len(lines) == 1
    if verbose:
        assert lines[0].rstrip().endswith('s'), lines[0]
        assert agent._wrap_verbose('Result: ', fr) in out, out
    else:
        assert lines[0].split(' - ', 1)[1] == _old_short(agent, fr)
    assert leaks(out) == []


# multimodal/dict previews: exact old format is pinned by test_p1br4_astra_probe.py


@pytest.mark.parametrize('verbose', [False, True])
def test_normal_large_string_preview(provider, monkeypatch, verbose):
    import run_agent
    monkeypatch.setattr(run_agent, 'handle_function_call', lambda name, args, *a, **k: BIG)
    agent, r, out = _run_normal(provider, [('terminal', {'command': 'true'})], verbose_logging=verbose)
    fr = _tool_contents(r)[0]
    lines = _completed_lines(out)
    persisted = isinstance(fr, str) and fr != BIG
    fact('NORMAL-BIG verbose=%s' % verbose, 'persisted_for_model', persisted, 'model_len', len(fr) if isinstance(fr, str) else None,
         'preview_starts_raw', lines[0].split(' - ', 1)[-1].startswith('big-raw') if lines else None,
         'out_len', len(out), 'line0', repr(lines[0][:120]) if lines else None,
         'old_short_on_model_msg', repr(_old_short(agent, fr)[:100]))
    assert len(lines) == 1
    # Contract (old behaviour): the preview is built from the post-persistence
    # result the model sees -- a short <persisted-output> stub, never the raw dump.
    assert persisted
    if verbose:
        assert agent._wrap_verbose('Result: ', fr) in out
    else:
        # the stub is multi-line, so compare against the whole printed output
        assert ' - ' + _old_short(agent, fr) in out
    assert len(out) < 20000
