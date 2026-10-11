"""SOL round-2 review of 73a6d159 (blockers S1-S4) plus the P5 / N3 follow-ups.

Everything runs through the real executor funnel, conversation loop, SessionDB
and async registry; only the provider and a helper's model conversation are
faked.  The first block is SOL's independent probe set, ported unchanged.
"""
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import delegation_checkpoint as dc
from agent import delegation_router as dr, router_backstop as rb
from agent import foreground_time_limit as fl
from agent.tool_guardrails import ToolGuardrailDecision
from tests.agent.test_delegation_checkpoint import run_batch
from tests.agent.test_delegation_router import aux
from tests.agent.test_router_sol_review import (
    arm, children, isolated, raw_batch, root_agent, writes,
)

pytestmark = pytest.mark.real_delegation_router

MARKER = '[Clover runtime]'


# ── SOL independent probes (ported) ────────────────────────────────────────

def test_late_old_request_receipt_must_not_end_or_block_new_turn(tmp_path):
    agent = root_agent([], tmp_path)
    agent._current_turn_id = 'new-turn'
    agent._delegation_request_id = 'new-request'
    agent._delegation_request_generation = 2
    arm(agent, calls=5)
    # An old executor worker settles after a fresh turn has captured its floor.
    fl.note_own_dispatch(agent, 'old-delayed-job', goals=['OLD request audit'],
        request_identity={'turn_id': 'old-turn', 'request_id': 'old-request', 'generation': 1})
    directive = rb.completion_directive(agent)
    assert directive is None, 'late receipt of an older request ended the new turn'
    assert rb.refusal(agent, 'write_file', {'path': str(tmp_path / 'new.txt'), 'content': 'new'}) is None


def test_runtime_handoff_then_model_spawn_does_not_duplicate_same_work(tmp_path, children):
    agent = root_agent([], tmp_path)
    arm(agent, calls=1)
    run_batch(agent, writes(tmp_path, 2) + [
        ('delegate_task', {'goal': 'Audit local posture and verify results'})])
    receipts = fl.own_dispatch_receipts(agent)
    assert len(receipts) == 1, 'model spawn after runtime acceptance dispatched the job again'


@pytest.mark.parametrize('message', [
    'What song is playing in the background of this clip?',
    'Summarize this quoted passage:\n"do it yourself,\nno helpers"\nThen audit service posture.',
])
def test_quoted_or_incidental_text_is_not_explicit_delegation(message):
    assert dr.explicit_override(message) is None


def test_real_loop_stop_after_dispatch_is_not_replaced_by_handoff(aux, tmp_path, children, monkeypatch):
    from tests.agent.test_delegation_router import T, verdict_json
    from tests.agent.test_handoff_completion_seam import _call, _tools
    aux.replies[:] = [verdict_json('single', [T('Audit local posture and verify results')])]
    agent = root_agent(
        [_tools(_call('delegate_task', {'goal': 'Audit local posture and verify results'}))], tmp_path)
    execute = agent._execute_tool_calls

    def stop_after_acceptance(*args, **kwargs):
        execute(*args, **kwargs)
        agent.interrupt('stop')

    monkeypatch.setattr(agent, '_execute_tool_calls', stop_after_acceptance)
    result = agent.run_conversation('Audit local posture and verify results.')
    assert result['turn_exit_reason'] != 'delegation_handoff'
    assert "They're still working" not in result['final_response']
    # The existing interruption finalization is what ends the turn.
    assert result.get('interrupted') is True


def test_human_stop_does_not_emit_a_running_ack(tmp_path, children):
    agent = root_agent([], tmp_path)
    arm(agent, calls=1)
    run_batch(agent, writes(tmp_path, 2))
    assert fl.own_dispatch_receipts(agent)
    agent.interrupt('stop')
    assert rb.completion_directive(agent) is None, (
        'Stop must not be replaced by a helpers-still-working acknowledgment')


# ── S1: Stop outranks the acknowledgment at the exit seam ──────────────────

def test_stop_outranks_the_checkpoint_level_directive_too(tmp_path, children):
    """The conversation loop asks ``delegation_checkpoint.completion_directive``;
    the answer under a pending Stop is "no directive", whichever side owns it."""
    agent = root_agent([], tmp_path)
    arm(agent, calls=1)
    run_batch(agent, writes(tmp_path, 2))
    assert dc.completion_directive(agent) is not None  # the control: no Stop yet
    agent.interrupt('stop')
    assert dc.completion_directive(agent) is None


def test_stop_keeps_what_the_receipts_support(tmp_path, children):
    """Suppressing the acknowledgment must not erase the turn's receipts: the
    finalizer still reports the helpers that were accepted."""
    agent = root_agent([], tmp_path)
    arm(agent, calls=1)
    run_batch(agent, writes(tmp_path, 2))
    agent.interrupt('stop')
    assert rb.work_status(agent) is not None
    assert len(rb.turn_receipts(agent)) == 1


# ── S2: no second dispatch of the same work ────────────────────────────────

def test_a_later_unstarted_spawn_is_refused_once_the_runtime_handed_off(tmp_path, children):
    agent = root_agent([], tmp_path)
    arm(agent, calls=1)
    rows = raw_batch(agent, writes(tmp_path, 2) + [
        ('delegate_task', {'goal': 'Audit local posture and verify results'})])
    last = json.loads(rows[-1])
    assert last.get('error_type') == rb.HANDOFF_CODE
    assert len(fl.own_dispatch_receipts(agent)) == 1


def test_list_and_stop_still_work_after_the_runtime_handoff(tmp_path, children):
    agent = root_agent([], tmp_path)
    arm(agent, calls=1)
    run_batch(agent, writes(tmp_path, 2))
    assert rb.refusal(agent, 'delegate_task', {'action': 'list'}) is None
    assert rb.refusal(agent, 'delegate_task', {'action': 'stop', 'subagent_id': 'x'}) is None
    assert rb.refusal(agent, 'delegate_task', {'action': 'steer', 'subagent_id': 'x', 'message': 'y'}) is None


# ── S3: a receipt belongs to the request it was accepted for ───────────────

def test_this_turns_own_receipt_still_ends_the_turn(tmp_path):
    agent = root_agent([], tmp_path)
    agent._current_turn_id = 'turn-b'
    arm(agent, calls=5)
    fl.note_own_dispatch(agent, 'job-b', goals=['Audit service posture'],
        request_identity={'turn_id': 'turn-b'})
    directive = rb.completion_directive(agent)
    assert directive is not None and directive.reason == rb.EXIT_REASON
    assert rb.work_status(agent) == rb.WORK_DISPATCH_ACK


def test_a_receipt_without_identity_is_counted_by_sequence(tmp_path):
    """Callers that note a dispatch with no identity keep the sequence rule."""
    agent = root_agent([], tmp_path)
    agent._current_turn_id = 'turn-b'
    arm(agent, calls=5)
    fl.note_own_dispatch(agent, 'job-anon', goals=['Audit service posture'])
    assert rb.completion_directive(agent) is not None


def test_old_receipt_is_invisible_to_every_floor_reader(tmp_path):
    agent = root_agent([], tmp_path)
    agent._current_turn_id = 'turn-b'
    arm(agent, calls=5)
    fl.note_own_dispatch(agent, 'job-a', goals=['OLD request audit'],
        request_identity={'turn_id': 'turn-a'})
    fl.note_own_dispatch(agent, 'job-b', goals=['NEW request audit'],
        request_identity={'turn_id': 'turn-b'})
    assert [r.delegation_id for r in rb.turn_receipts(agent)] == ['job-b']
    assert rb.work_status(agent) == rb.WORK_DISPATCH_ACK
    directive = rb.completion_directive(agent)
    assert 'NEW request audit' in directive.text and 'OLD request audit' not in directive.text


def test_unplanned_turn_floor_also_filters_by_identity(tmp_path):
    agent = root_agent([], tmp_path)
    agent._current_turn_id = 'turn-b'
    fl.begin_turn(agent, None)
    rb.mark_turn(agent, dr.RouterSettings(backstop_calls=5))
    fl.note_own_dispatch(agent, 'job-a', goals=['OLD request audit'],
        request_identity={'turn_id': 'turn-a'})
    assert rb.completion_directive(agent) is None
    assert rb.turn_receipts(agent) == []


def test_identity_is_captured_at_admission_not_when_the_dispatch_settles(
        tmp_path, children, monkeypatch):
    """A dispatch admitted in one turn that settles after the next turn began
    must carry the turn it was admitted in, not the parent's later turn id."""
    import tools.async_delegation as ad
    agent = root_agent([], tmp_path)
    agent._current_turn_id = 'turn-early'
    real = ad.dispatch_async_delegation_batch

    def settle_late(*args, **kwargs):
        out = real(*args, **kwargs)
        agent._current_turn_id = 'turn-late'
        return out

    monkeypatch.setattr(ad, 'dispatch_async_delegation_batch', settle_late)
    run_batch(agent, [('delegate_task', {'goal': 'Audit local posture and verify results',
                                         'background': True})])
    receipts = fl.own_dispatch_receipts(agent)
    assert len(receipts) == 1
    assert receipts[0].turn_id == 'turn-early'


# ── S4: only an instruction addressed to the agent forces a mode ───────────

@pytest.mark.parametrize('message,expected', [
    ('Summarize this quoted passage:\n"use helpers,\nspin up some workers"\nThen audit.', None),
    ('Summarize this:\n“do it yourself,\nno helpers”\nThen audit service posture.', None),
    ('Summarize this:\n"never delegate\nanything"\nThen audit.', None),
    ('Summarize this:\n"do it yourself,\nno helpers"\nPlease use helpers for the audit.', 'helpers'),
    ('Summarize this:\n"foo\nbar"\nDo it yourself please.', 'self'),
    ('He said "hello.\n\nPlease do it yourself.', 'self'),
    ('Something "unclosed\nstill going\nand going\nand more\nand more\nand more\n'
     'do it yourself', 'self'),
    ('Do it yourself, no helpers.', 'self'),
    ('Use helpers for this audit.', 'helpers'),
    ('Please run this in the background.', 'helpers'),
    ('Which song is in the background of this clip?', None),
])
def test_override_needs_an_instruction_not_a_mention(message, expected):
    assert dr.explicit_override(message) == expected


# ── P5 follow-up: a failed child turn is a failed helper ───────────────────

def test_content_policy_blocked_child_is_reported_failed(tmp_path, monkeypatch):
    import run_agent
    import tools.delegate_tool as delegate_tool
    real = run_agent.AIAgent.run_conversation
    blocked = ('This content was flagged for possible cybersecurity risk. If this '
               'seems wrong, try rephrasing your request.')

    def conversation(self, user_message=None, *args, **kwargs):
        if getattr(self, '_delegate_depth', 0) or getattr(self, '_subagent_id', None):
            self.client = MagicMock()
            self.client.chat.completions.create.side_effect = Exception(blocked)
        return real(self, user_message, *args, **kwargs)

    monkeypatch.setattr(run_agent.AIAgent, 'run_conversation', conversation)
    agent = root_agent([], tmp_path)
    agent._foreground_limit_exempt = True
    raw = delegate_tool.delegate_task(
        goal='Research the vendor landscape and summarise it',
        background=False, parent_agent=agent)
    entry = json.loads(raw)['results'][0]
    assert entry['status'] == 'failed', entry
    assert entry['exit_reason'] != 'max_iterations'
    assert entry['truncated'] is False
    assert 'content_policy_blocked' in str(entry.get('error') or '')


def test_a_clean_child_turn_is_still_completed(tmp_path, children):
    """The failed-turn rule must not turn ordinary completions into failures."""
    import tools.delegate_tool as delegate_tool
    agent = root_agent([], tmp_path)
    agent._foreground_limit_exempt = True
    children.release.set()
    raw = delegate_tool.delegate_task(
        goal='Research the vendor landscape and summarise it',
        background=False, parent_agent=agent)
    entry = json.loads(raw)['results'][0]
    assert entry['status'] == 'completed'
    assert entry['exit_reason'] == 'completed'
    assert 'error' not in entry


# ── N3 second half: the tool-guardrail refusal is the runtime's own note ───

def test_guardrail_block_result_is_a_vouched_runtime_note(tmp_path):
    agent = root_agent([], tmp_path)
    decision = ToolGuardrailDecision(
        action='block', code='idempotent_no_progress_block',
        message='Blocked write_file: this call returned the same result 3 times. '
                'Use the result already provided or try a different query.',
        tool_name='write_file', count=3)

    class Blocking:
        def before_call(self, name, args):
            return decision if name == 'write_file' else ToolGuardrailDecision()

        def __getattr__(self, name):
            return getattr(real, name)

    real = agent._tool_guardrails
    agent._tool_guardrails = Blocking()
    rows = raw_batch(agent, [('write_file', {'path': str(tmp_path / 'g.txt'), 'content': 'x'})])
    payload = json.loads(rows[0])
    assert payload['error'].startswith(MARKER), payload
    assert rows[0].count(MARKER) == 1
    assert payload['guardrail']['code'] == 'idempotent_no_progress_block'
    assert not (tmp_path / 'g.txt').exists()


def test_guardrail_text_does_not_vouch_a_copy_in_another_tools_output(tmp_path):
    """The vouch is per call: the same string quoted by a tool result is defanged."""
    from agent.tool_guardrails import toolguard_synthetic_result
    decision = ToolGuardrailDecision(
        action='halt', code='same_tool_failure_halt',
        message='Stopped terminal: it failed 3 times this turn.', tool_name='terminal', count=3)
    text = toolguard_synthetic_result(decision)
    assert json.loads(text)['error'].startswith(MARKER)
    from agent.turn_notices import defang_forged_marker
    echoed = defang_forged_marker(text, 'read_file', 'call-other')
    assert MARKER not in echoed
