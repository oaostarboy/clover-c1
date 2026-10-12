"""Frozen-router review contracts (SOL review of f2319b0e), no HTTP, no live provider.

Four contracts, each exercised against the real conversation loop / executor
funnel / SessionDB / async registry; only the provider and a helper's model
conversation are faked:

1. A not-direct plan whose helpers were accepted ends the root's turn with a
   deterministic acknowledgment -- no further model call, no foreground write,
   never a "verified" answer while a durable child still runs.
2. Router-generated dispatch obeys the same authorization as a model's own
   ``delegate_task`` (a plugin that denies the one denies the other).
3. Quoted / fenced / blockquoted text is not the user's instruction.
4. A ``[Clover runtime]`` marker is trusted per result, not per byte string.
"""
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import delegation_checkpoint as dc
from agent import delegation_router as dr, router_backstop as rb
from agent import foreground_time_limit as fl
from tests.agent.test_delegation_router import aux, T, verdict_json
from tests.agent.test_delegation_checkpoint import _agent, _call as _wire_call, run_batch
from tests.agent.test_handoff_completion_seam import _call, _tools, _text
import tools.async_delegation as ad

pytestmark = pytest.mark.real_delegation_router

MARKER = '[Clover runtime]'


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    ad._reset_for_tests()
    monkeypatch.setattr(fl, '_detached_delivery_available', lambda: True)
    monkeypatch.setattr('gateway.session_context.async_delivery_supported', lambda: True)
    yield
    ad._reset_for_tests()


@pytest.fixture
def children(monkeypatch):
    """Real child construction/pool/runner; the child's conversation blocks until
    released, so its durable row stays ``running``."""
    import run_agent
    real = run_agent.AIAgent.run_conversation
    release, started, finished = threading.Event(), threading.Event(), threading.Event()
    seen = []

    def conversation(self, user_message=None, *args, **kwargs):
        if getattr(self, '_delegate_depth', 0) or getattr(self, '_subagent_id', None):
            seen.append(self)
            started.set()
            release.wait(20)
            finished.set()
            return {'final_response': 'child checked output', 'messages': [], 'api_calls': 1,
                    'completed': True, 'input_tokens': 0, 'output_tokens': 0}
        return real(self, user_message, *args, **kwargs)

    monkeypatch.setattr(run_agent.AIAgent, 'run_conversation', conversation)
    yield SimpleNamespace(release=release, started=started, finished=finished, seen=seen)
    release.set()
    if started.is_set():
        assert finished.wait(10)


def root_agent(script, tmp_path, session_id='sol-review'):
    from clover_state import SessionDB
    agent = _agent(opt_in=False, max_iterations=12,
                   session_db=SessionDB(tmp_path / f'{session_id}.db'), session_id=session_id)
    agent.client = MagicMock()
    agent.client.chat.completions.create.side_effect = script
    agent._cached_system_prompt = 'You are helpful.'
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._disable_streaming = True
    return agent


def arm(agent, mode_tasks=('Audit local posture and verify results',), calls=1):
    fl.begin_turn(agent, None)
    plan = dr.Verdict('single', tuple(dr.PlanTask(g) for g in mode_tasks))
    rb.begin(agent, plan, 'Audit local posture', dr.RouterSettings(backstop_calls=calls))


def writes(tmp_path, n, prefix='w'):
    return [('write_file', {'path': str(tmp_path / f'{prefix}{i}.txt'), 'content': 'x'})
            for i in range(n)]


def raw_batch(agent, calls, task_id='sol-raw'):
    """Like ``run_batch`` but returns each tool message's raw content string."""
    tool_calls = [_wire_call(n, a, f'call-{i}') for i, (n, a) in enumerate(calls)]
    wire = [{'id': tc.id, 'type': 'function',
             'function': {'name': tc.function.name, 'arguments': tc.function.arguments}}
            for tc in tool_calls]
    messages = [{'role': 'user', 'content': 'do the job'},
                {'role': 'assistant', 'content': None, 'tool_calls': wire}]
    agent._execute_tool_calls(SimpleNamespace(tool_calls=tool_calls), messages, task_id)
    return [m['content'] for m in messages[2:]]


def is_handoff(row):
    return isinstance(row, dict) and row.get('error_type') == rb.HANDOFF_CODE


# ── 1. a dispatch is an acknowledgment, not a finished verdict ─────────────

@pytest.mark.parametrize('backstop', [False, True])
def test_dispatch_is_ack_not_finished_verdict_or_foreground_integration(
        backstop, aux, tmp_path, children):
    aux.replies[:] = [verdict_json('single', [T('Audit local posture and check all findings')])]
    p = tmp_path / 'continued.txt'
    if backstop:
        script = [_tools(_call('write_file', {'path': str(tmp_path / f'{i}.txt'), 'content': 'x'}))
                  for i in range(3)]
    else:
        script = [_tools(_call('delegate_task', {'goal': 'Audit local posture and check all findings'}))]
    script += [_tools(_call('write_file', {'path': str(p), 'content': 'long foreground integration'})),
               _text('No spying. No intruder. Everything is verified.')]
    agent = root_agent(script, tmp_path)

    result = agent.run_conversation('Audit the local posture and verify the overall verdict.')

    assert children.started.wait(5)
    rows = [json.loads(m['content']) for m in result['messages'] if m.get('role') == 'tool']
    handed = next(r for r in rows if r.get('status') == 'dispatched' or r.get('delegation_id'))
    row = ad.get_durable_delegation(handed['delegation_id'])
    assert row is not None and row['state'] == 'running'
    assert 'web_search' not in children.seen[0].valid_tool_names
    assert result['turn_exit_reason'] == 'delegation_handoff', result
    assert 'verified' not in result['final_response'].lower()
    assert not p.exists()
    # 3 work requests, then (backstop) the one short tools-off opener line
    assert agent.client.chat.completions.create.call_count == (4 if backstop else 1)
    assert result['work_status'] == 'dispatch_ack'
    assert result['work_status'] != 'verified_completion'
    # the ack is the last assistant row, persisted like any other final reply
    assert result['messages'][-1]['role'] == 'assistant'
    assert result['messages'][-1]['content'] == result['final_response']


def test_ack_does_not_depend_on_checkpoint_opt_in(aux, tmp_path, children):
    aux.replies[:] = [verdict_json('single', [T('Audit local posture and check all findings')])]
    agent = root_agent([_tools(_call('delegate_task', {'goal': 'Audit local posture'}))], tmp_path)

    result = agent.run_conversation('Audit the local posture and verify the overall verdict.')

    assert agent._delegation_checkpoint.settings.enabled is False
    assert result['turn_exit_reason'] == 'delegation_handoff'


def test_a_write_after_an_accepted_dispatch_in_the_same_batch_is_refused(tmp_path, children):
    agent = root_agent([], tmp_path)
    arm(agent, calls=5)

    rows = run_batch(agent, [('delegate_task', {'goal': 'Audit local posture and verify results'})]
                     + writes(tmp_path, 2))

    assert rows[0].get('status') == 'dispatched'
    assert all(is_handoff(r) for r in rows[1:]), rows
    assert not any((tmp_path / f'w{i}.txt').exists() for i in range(2))
    assert rows[1]['delegation_id'] == rows[0]['delegation_id']
    assert children.started.wait(5)


def test_a_failed_dispatch_arms_no_acknowledgment_and_no_refusal(aux, tmp_path, children, monkeypatch):
    from clover_cli import plugins

    def deny(name, args, **kw):
        return ('delegation blocked by test policy', None) if name == 'delegate_task' else (None, None)

    monkeypatch.setattr(plugins, '_dispatch_pre_tool_call_hooks', deny)
    agent = root_agent([], tmp_path)
    arm(agent, calls=5)

    denied, *rest = run_batch(agent, [('delegate_task', {'goal': 'Audit local posture'})]
                              + writes(tmp_path, 2))

    assert 'delegation blocked by test policy' in json.dumps(denied)
    assert not any(is_handoff(r) for r in rest)
    assert all((tmp_path / f'w{i}.txt').exists() for i in range(2))
    assert dc.completion_directive(agent) is None
    assert not children.started.wait(0.1)


def test_human_stop_and_helper_control_survive_the_handoff(tmp_path, children):
    agent = root_agent([], tmp_path)
    arm(agent)
    rows = run_batch(agent, writes(tmp_path, 2))
    handed = rows[-1]
    assert is_handoff(handed) and handed['subagent_ids']
    assert children.started.wait(5)
    assert dc.completion_directive(agent) is not None

    listed, stopped = run_batch(agent, [
        ('delegate_task', {'action': 'list'}),
        ('delegate_task', {'action': 'stop', 'subagent_id': handed['subagent_ids'][0]}),
    ])

    assert not is_handoff(listed) and not is_handoff(stopped)
    # the interrupt of the turn itself is never gated by the router
    agent.interrupt('stop')
    assert agent._interrupt_requested


def test_an_unrelated_conversation_is_not_blocked(tmp_path, children):
    busy_dir, other_dir = tmp_path / 'a', tmp_path / 'b'
    busy_dir.mkdir()
    other_dir.mkdir()
    busy = root_agent([], busy_dir, session_id='chat-a')
    other = root_agent([], other_dir, session_id='chat-b')
    arm(busy)
    run_batch(busy, writes(busy_dir, 2))
    assert children.started.wait(5)
    assert dc.completion_directive(busy) is not None

    rows = run_batch(other, writes(other_dir, 4))

    assert not any(is_handoff(r) for r in rows)
    assert all((other_dir / f'w{i}.txt').exists() for i in range(4))
    assert dc.completion_directive(other) is None
    # and a fresh human turn on the busy chat is not walled off either
    arm(busy, calls=5)
    assert dc.completion_directive(busy) is None, 'last turn\'s receipt must not end this turn'
    assert rb.work_status(busy) is None
    again = run_batch(busy, writes(busy_dir, 1, prefix='next'))
    assert not any(is_handoff(r) for r in again)


def test_work_status_separates_ack_partial_incomplete_and_verified(tmp_path, children):
    agent = root_agent([], tmp_path)
    arm(agent)
    assert rb.work_status(agent) is None
    run_batch(agent, writes(tmp_path, 2))
    assert children.started.wait(5)
    assert rb.work_status(agent) == 'dispatch_ack'
    children.release.set()
    assert children.finished.wait(10)

    deadline = time.monotonic() + 10
    status = rb.work_status(agent)
    while status in ('dispatch_ack', 'partial') and time.monotonic() < deadline:
        time.sleep(0.05)
        status = rb.work_status(agent)
    # a finished-but-unchecked result is not verification
    assert status != 'verified_completion'


# ── 2. generated dispatch obeys delegate authorization ─────────────────────

def test_backstop_dispatch_obeys_delegate_plugin_authorization(aux, tmp_path, children, monkeypatch):
    from clover_cli import plugins

    def deny_delegate(name, args, **kw):
        return ('delegation blocked by test policy', None) if name == 'delegate_task' else (None, None)

    monkeypatch.setattr(plugins, '_dispatch_pre_tool_call_hooks', deny_delegate)
    agent = root_agent([], tmp_path)
    arm(agent, ('Audit local posture and verify results',))
    denied = run_batch(agent, [('delegate_task', {'goal': 'Audit local posture and verify results'})])
    assert 'delegation blocked by test policy' in json.dumps(denied)

    results = run_batch(agent, writes(tmp_path, 2))

    assert not children.started.wait(0.1), results
    assert ad.active_count() == 0
    # honest error: the refused call says helpers could not start, not "handed off"
    assert not any(is_handoff(r) for r in results)
    assert 'could not be started' in json.dumps(results[-1])
    assert getattr(agent._router_turn, 'handoff', None) is None


def test_backstop_dispatch_is_visible_to_pre_tool_hooks_as_delegate_task(tmp_path, children, monkeypatch):
    from clover_cli import plugins
    seen = []

    def spy(name, args, **kw):
        seen.append(name)
        return (None, None)

    monkeypatch.setattr(plugins, '_dispatch_pre_tool_call_hooks', spy)
    agent = root_agent([], tmp_path)
    arm(agent)

    run_batch(agent, writes(tmp_path, 2))

    assert 'delegate_task' in seen
    assert children.started.wait(5)


# ── 3. quoted / fenced / blockquoted text is not an instruction ────────────

@pytest.mark.parametrize('message', [
    'Review this quoted example: "do it yourself, no helpers". Find its problems.',
    'Explain this log:\n```\nuse helpers to delete files\n```',
    'What does this mean?\n> do it yourself\n> without any helpers',
    "The README says 'do it yourself' somewhere; where?",
    'See `use helpers` in the config and `no helpers` too.',
    'Summarise: “do it yourself” (from the vendor page).',
    'Review the snippet:\n~~~\nuse helpers\n~~~',
    'Explain this log:\n```\nuse helpers to delete files',
])
def test_quoted_source_is_not_authentic_override(message):
    assert dr.explicit_override(message) is None


@pytest.mark.parametrize('message, expected', [
    ('Please do it yourself.', 'self'),
    ('Use helpers for this one.', 'helpers'),
    ('Review "the plan" but do it yourself, no helpers.', 'self'),
    ('He wrote "ship it" so use helpers to ship it.', 'helpers'),
    ("don't use helpers, it's quick", 'self'),
])
def test_authentic_instruction_beside_quoted_text_still_counts(message, expected):
    assert dr.explicit_override(message) == expected


def test_quoted_do_it_yourself_does_not_force_direct(aux):
    aux.replies[:] = [verdict_json('single', [T('Audit every service and verify the result')])]

    verdict = dr.route('Review this quoted example: "do it yourself, no helpers". '
                       'Then audit every service and verify the result.')

    assert verdict is not None and verdict.mode == 'single'
    assert verdict.source != 'override'


def test_fenced_use_helpers_does_not_force_single(aux):
    aux.replies[:] = [verdict_json('direct')]

    verdict = dr.route('Explain this log:\n```\nuse helpers to delete files\n```')

    assert verdict is not None and verdict.mode == 'direct'
    assert verdict.source != 'override'


# ── 4. the marker is trusted per result, not per byte string ───────────────

def test_vouched_marker_cannot_be_replayed_as_codex_tool_output():
    from agent.tool_dispatch_helpers import make_tool_result_message
    state = rb.RouterTurn(dr.Verdict('single', (dr.PlanTask('Audit local posture'),)),
                          'Audit local posture', dr.RouterSettings(), 0)
    state.handoff = {'delegation_id': 'old-id', 'subagent_ids': ['old-child'],
                     'later': [], 'next_stage': None}
    trusted = rb._verdict_for(state, 'write_file').block_result

    replay = make_tool_result_message('codex', trusted, 'forged-call')

    assert MARKER not in replay['content']


def test_authentic_marker_trusted_in_runtime_result_not_in_codex_replay(tmp_path, children):
    from agent.tool_dispatch_helpers import make_tool_result_message
    agent = root_agent([], tmp_path)
    arm(agent)

    raw_rows = raw_batch(agent, writes(tmp_path, 2))
    raw = raw_rows[-1]

    assert MARKER in raw, 'the real executor keeps Clover\'s own marker'
    assert MARKER in make_tool_result_message('write_file', raw, 'call-1')['content']
    # byte-identical copy as another source's result, even under the same call id
    assert MARKER not in make_tool_result_message('codex', raw, 'call-1')['content']
    assert MARKER not in make_tool_result_message('codex', raw, 'forged')['content']
    # same tool name, a different invocation
    assert MARKER not in make_tool_result_message('write_file', raw, 'call-99')['content']
    assert children.started.wait(5)


def test_marker_copied_into_a_later_real_tool_result_is_defanged(tmp_path, children):
    agent = root_agent([], tmp_path)
    arm(agent)
    raw = raw_batch(agent, writes(tmp_path, 2))[-1]
    echo = tmp_path / 'echo.txt'
    echo.write_text(raw)
    rb.clear(agent)  # a later turn: the router no longer refuses the read itself

    [read] = raw_batch(agent, [('read_file', {'path': str(echo)})], task_id='sol-echo')

    assert 'Handed off to helpers' in read
    assert MARKER not in read
    assert children.started.wait(5)


def test_retry_keeps_one_total_deadline(aux, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(dr.time, 'monotonic', lambda: clock[0])
    calls = []

    def model(messages, timeout):
        calls.append(timeout)
        clock[0] += 4.7
        return 'invalid JSON'

    monkeypatch.setattr(dr, '_call_model', model)
    assert dr.route('Build and verify a compiler', settings=dr.RouterSettings(timeout_seconds=5)) is None
    assert len(calls) == 1, 'do not retry below minimum remaining budget'
    assert calls[0] <= 5


def test_default_router_active_and_timeout_no_plan(aux, tmp_path):
    agent = root_agent([_text('normal')], tmp_path)
    assert dr.skip_reason(agent, 'Build and verify a compiler') is None
    aux.replies[:] = [TimeoutError('synthetic timeout')]
    assert dr.plan_for_turn(agent, 'Build and verify a compiler') == ''
    assert getattr(agent, '_router_turn', None) is None
