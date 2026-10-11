"""Staged plans carry on under shipped defaults (``delegation.checkpoint`` off).

The router sizes a request as stages; stage 1 is dispatched on the human turn.
The later stages ride the finished stage's durable row and the *runtime* starts
each one when the stage before it was delivered and finished cleanly -- through
the same authorized dispatch a model's own ``delegate_task`` takes.  Real
conversation loop, executor funnel, SessionDB, async registry and durable rows;
only the provider and a helper's model conversation are faked.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from agent import delegation_router as dr, router_backstop as rb, work_lifecycle as wl
from agent import foreground_time_limit as fl
from tests.agent.test_delegation_checkpoint import run_batch
from tests.agent.test_delegation_router import T, aux  # noqa: F401  (aux fixture)
from tests.agent.test_handoff_completion_seam import _call, _text, _tools
from tests.agent.test_router_sol_review import root_agent, writes
import tools.async_delegation as ad

pytestmark = pytest.mark.real_delegation_router

STAGE_ONE = 'Write the feature flag module with unit tests'
STAGE_TWO = 'Review the actual feature diff and its tests'
STAGE_THREE = 'Write the release notes for the reviewed change'


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    ad._reset_for_tests()
    monkeypatch.setattr(fl, '_detached_delivery_available', lambda: True)
    monkeypatch.setattr('gateway.session_context.async_delivery_supported', lambda: True)
    yield
    ad._reset_for_tests()


@pytest.fixture
def helpers(monkeypatch):
    """Real child construction/pool/runner; the child's conversation waits for
    ``gate`` (a helper does not finish in a millisecond), then returns the next
    scripted outcome (``ok`` or ``fail``)."""
    import run_agent
    real = run_agent.AIAgent.run_conversation
    state = SimpleNamespace(outcomes=[], goals=[], lock=threading.Lock(), gate=threading.Event())

    def conversation(self, user_message=None, *args, **kwargs):
        if getattr(self, '_delegate_depth', 0) or getattr(self, '_subagent_id', None):
            with state.lock:
                state.goals.append({'user': str(user_message),
                                    'system': getattr(self, 'ephemeral_system_prompt', '') or ''})
            state.gate.wait(20)
            with state.lock:
                kind = state.outcomes.pop(0) if state.outcomes else 'ok'
            if kind == 'fail':
                return {'final_response': '', 'messages': [], 'api_calls': 1,
                        'completed': False, 'error': 'helper could not finish',
                        'input_tokens': 0, 'output_tokens': 0}
            return {'final_response': f'RESULT-OF[{str(user_message)[:48]}]', 'messages': [],
                    'api_calls': 1, 'completed': True, 'input_tokens': 0, 'output_tokens': 0}
        return real(self, user_message, *args, **kwargs)

    monkeypatch.setattr(run_agent.AIAgent, 'run_conversation', conversation)
    yield state
    state.gate.set()


def _wait(predicate, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f'timed out waiting for {what}')


def _staged(*goals_and_deps):
    return dr.parse_verdict({'mode': 'staged', 'tasks': [T(g, d) for g, d in goals_and_deps]})


def _dispatch_stage_one(agent, tmp_path, plan, request='Build and review the feature flag'):
    """The human turn: the root works, the backstop hands stage 1 to helpers."""
    fl.begin_turn(agent, None)
    rb.begin(agent, plan, request, dr.RouterSettings(backstop_calls=1))
    rows = run_batch(agent, writes(tmp_path, 2))
    assert rows[-1].get('delegation_id'), rows
    return rows[-1]['delegation_id']


def _finish_and_deliver(helpers, delegation_id):
    """The stage finished and the gateway claimed its completion for a turn."""
    helpers.gate.set()
    _wait(lambda: (ad.get_durable_delegation(delegation_id) or {}).get('state')
          not in (None, '', 'running', 'finalizing', 'stalling'),
          f'{delegation_id} to finish')
    assert ad.claim_completion_delivery(delegation_id, f'claim-{delegation_id}')


def _completion_turn(agent, delegation_id, reply='Reporting.'):
    agent.client.chat.completions.create.side_effect = [_text(reply)]
    return agent.run_conversation(
        f'[helper {delegation_id} finished]', persist_user_display_kind='internal_notification',
        completion_delegation_ids=[delegation_id],
    )


def _new_dispatch_ids(before):
    return [d for d in _all_ids() if d not in before]


def _all_ids():
    return [d['delegation_id'] for d in ad.list_async_delegations()]


# ── the durable follow-through exists under the default config ─────────────

def test_staged_dispatch_has_executable_followthrough_under_default_config(
        aux, tmp_path, helpers):
    agent = root_agent([], tmp_path)
    plan = dr.parse_verdict({'mode': 'staged', 'tasks': [
        T('Write feature flag module with tests'), T('Review actual feature diff and tests', [0])]})
    delegation_id = _dispatch_stage_one(agent, tmp_path, plan, 'Build and review feature flag')
    row = ad.get_durable_delegation(delegation_id)
    assert row is not None
    stored = ad.get_continuation_plan(delegation_id)
    assert stored and stored['kind'] == rb.PLAN_KIND and stored['consumed'] == 0
    assert [stage['tasks'] for stage in stored['stages']] == [[1]], stored
    assert stored['tasks'][1]['goal'] == 'Review actual feature diff and tests'


# ── dependency order ───────────────────────────────────────────────────────

def test_three_stages_run_in_dependency_order_one_stage_in_flight(aux, tmp_path, helpers):
    agent = root_agent([], tmp_path)
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)), (STAGE_THREE, (1,)))
    first = _dispatch_stage_one(agent, tmp_path, plan)
    _wait(lambda: len(helpers.goals) == 1, 'stage one helper')

    # Stage 1 is delivered, not stage 2 yet: nothing else may be running.
    assert [g['user'] for g in helpers.goals][0].startswith(STAGE_ONE[:20])
    assert len(_all_ids()) == 1

    _finish_and_deliver(helpers, first)
    before = set(_all_ids())
    result = _completion_turn(agent, first, 'Stage one is done; stage two is running.')
    second = _new_dispatch_ids(before)
    assert len(second) == 1, 'exactly one next stage was started'
    _wait(lambda: len(helpers.goals) == 2, 'stage two helper')
    assert STAGE_TWO[:20] in helpers.goals[1]['user']
    # The next stage's helpers get what the stage before returned.
    assert 'RESULT-OF[' in helpers.goals[1]['system'] + helpers.goals[1]['user']
    assert result.get('final_response')

    # Stage 3 waits for stage 2's own delivery.
    plan_two = ad.get_continuation_plan(second[0])
    assert plan_two and [s['tasks'] for s in plan_two['stages']] == [[2]]
    _finish_and_deliver(helpers, second[0])
    before = set(_all_ids())
    _completion_turn(agent, second[0])
    third = _new_dispatch_ids(before)
    assert len(third) == 1
    _wait(lambda: len(helpers.goals) == 3, 'stage three helper')
    assert STAGE_THREE[:20] in helpers.goals[2]['user']
    assert ad.get_continuation_plan(third[0]) is None, 'the last stage binds nothing'


def test_a_stage_still_running_starts_nothing(aux, tmp_path, helpers):
    agent = root_agent([], tmp_path)
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)))
    first = _dispatch_stage_one(agent, tmp_path, plan)
    # An early completion turn for a row that is not terminal yet (an
    # "each"-delivery child, a duplicate wake-up) must not start stage 2.
    _wait(lambda: len(helpers.goals) == 1, 'stage one helper')
    assert (ad.get_durable_delegation(first) or {}).get('state') == 'running'
    assert rb.continue_staged(agent, [first], dr.RouterSettings()) == ''
    assert len(helpers.goals) == 1
    assert ad.get_continuation_plan(first)['consumed'] == 0


# ── a failed child stops the chain, honestly ───────────────────────────────

def test_failed_stage_stops_the_chain_and_the_root_is_told_plainly(aux, tmp_path, helpers):
    helpers.outcomes[:] = ['fail']
    agent = root_agent([], tmp_path)
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)), (STAGE_THREE, (1,)))
    first = _dispatch_stage_one(agent, tmp_path, plan)
    _finish_and_deliver(helpers, first)
    before = set(_all_ids())

    _completion_turn(agent, first, 'The first stage did not complete.')

    assert _new_dispatch_ids(before) == [], 'a failed stage starts nothing after it'
    assert len(helpers.goals) == 1
    stored = ad.get_continuation_plan(first)
    assert stored['cancelled'] is True and stored['stages'] == []
    sent = agent.client.chat.completions.create.call_args.kwargs['messages']
    blob = ' '.join(str(m.get('content')) for m in sent if isinstance(m, dict))
    assert 'did not finish cleanly' in blob and 'NOT started' in blob
    # And a later delivery of the same row cannot revive it.
    again = rb.continue_staged(agent, [first], dr.RouterSettings())
    assert again == ''
    assert len(helpers.goals) == 1


def test_one_failed_child_in_a_wide_stage_stops_the_chain(aux, tmp_path, helpers):
    helpers.outcomes[:] = ['ok', 'fail']
    agent = root_agent([], tmp_path)
    plan = _staged(('Collect the data for lane A', ()), ('Collect the data for lane B', ()),
                   ('Merge both lanes into one report', (0, 1)))
    first = _dispatch_stage_one(agent, tmp_path, plan)
    _wait(lambda: len(helpers.goals) == 2, 'both lane helpers')
    _finish_and_deliver(helpers, first)
    before = set(_all_ids())

    _completion_turn(agent, first)

    assert _new_dispatch_ids(before) == []
    assert len(helpers.goals) == 2, 'the merge stage never ran on a partial input'


# ── exactly once across restart ────────────────────────────────────────────

def test_restart_and_replay_start_the_next_stage_exactly_once(aux, tmp_path, helpers):
    agent = root_agent([], tmp_path, session_id='octavia')
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)), (STAGE_THREE, (1,)))
    first = _dispatch_stage_one(agent, tmp_path, plan)
    _finish_and_deliver(helpers, first)
    before = set(_all_ids())

    # The gateway restarts: a fresh root instance of the same conversation gets
    # the replayed completion -- twice (a crash after the dispatch, a replay).
    for n in range(3):
        revived = root_agent([], tmp_path, session_id='octavia')
        _completion_turn(revived, first)

    started = _new_dispatch_ids(before)
    assert len(started) == 1, f'stage two started {len(started)} times: {started}'
    _wait(lambda: len(helpers.goals) == 2, 'stage two helper')
    time.sleep(0.2)
    assert len(helpers.goals) == 2


def test_another_conversation_cannot_continue_the_chain(aux, tmp_path, helpers):
    agent = root_agent([], tmp_path, session_id='owner')
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)))
    first = _dispatch_stage_one(agent, tmp_path, plan)
    _finish_and_deliver(helpers, first)
    before = set(_all_ids())

    stranger = root_agent([], tmp_path, session_id='someone-else')
    _completion_turn(stranger, first)

    assert _new_dispatch_ids(before) == []
    assert ad.get_continuation_plan(first)['consumed'] == 0


# ── the root reports; it never does or re-dispatches the stage ─────────────

def test_continuation_turn_refuses_the_roots_own_spawn_and_work(aux, tmp_path, helpers):
    agent = root_agent([], tmp_path)
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)), (STAGE_THREE, (1,)))
    first = _dispatch_stage_one(agent, tmp_path, plan)
    _finish_and_deliver(helpers, first)
    agent._router_completion_ids = (first,)
    fl.begin_turn(agent, 'internal_notification')
    note = dr.plan_for_turn(agent, 'helper finished', [], 'internal_notification')
    assert 'stage 2 is now running on helpers' in note, note

    rows = run_batch(agent, [
        ('delegate_task', {'goal': 'Review the actual feature diff and its tests'}),
        writes(tmp_path, 1, 'late')[0],
    ])
    assert all(r.get('error_type') == rb.HANDOFF_CODE for r in rows), rows
    directive = rb.completion_directive(agent)
    assert directive is not None and directive.reason == rb.EXIT_REASON


def test_root_dispatching_stage_one_itself_still_carries_the_rest(aux, tmp_path, helpers):
    agent = root_agent([], tmp_path)
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)))
    fl.begin_turn(agent, None)
    rb.begin(agent, plan, 'Build and review', dr.RouterSettings(backstop_calls=5))
    rows = run_batch(agent, [('delegate_task', {'tasks': [{'goal': STAGE_ONE}]})])
    delegation_id = rows[0]['delegation_id']

    assert rb.completion_directive(agent) is not None
    stored = ad.get_continuation_plan(delegation_id)
    assert stored and [s['tasks'] for s in stored['stages']] == [[1]]


def test_a_root_that_merged_the_stages_gets_no_runtime_chain(aux, tmp_path, helpers):
    agent = root_agent([], tmp_path)
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)))
    fl.begin_turn(agent, None)
    rb.begin(agent, plan, 'Build and review', dr.RouterSettings(backstop_calls=5))
    rows = run_batch(agent, [('delegate_task', {'tasks': [{'goal': STAGE_ONE}, {'goal': STAGE_TWO}]})])
    delegation_id = rows[0]['delegation_id']

    assert rb.completion_directive(agent) is not None
    assert ad.get_continuation_plan(delegation_id) is None


# ── a completion claimed before the continuation binds keeps its tail ──────
#
# The gateway claims a finished helper's completion before the turn that
# integrates it runs, so a claim is not proof the root saw anything.  The
# claims below use the real claim API on real rows.

def _stage_two_helpers(helpers):
    return sum(STAGE_TWO[:20] in g['user'] for g in helpers.goals)


def test_fast_claim_before_parent_ack_keeps_the_staged_tail(aux, tmp_path, helpers):
    agent = root_agent([], tmp_path)
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)), (STAGE_THREE, (1,)))
    fl.begin_turn(agent, None)
    rb.begin(agent, plan, 'Build, review, release notes', dr.RouterSettings(backstop_calls=5))
    rows = run_batch(agent, [('delegate_task', {'tasks': [{'goal': STAGE_ONE}]})])
    first = rows[0]['delegation_id']

    # The helper finishes and the gateway claims its completion while the
    # parent's turn is still acknowledging the dispatch.
    _finish_and_deliver(helpers, first)
    assert rb.completion_directive(agent) is not None
    stored = ad.get_continuation_plan(first)
    assert stored and [s['tasks'] for s in stored['stages']] == [[1], [2]], stored

    before = set(_all_ids())
    _completion_turn(agent, first)
    assert len(_new_dispatch_ids(before)) == 1, 'the claimed stage lost its next stage'
    _wait(lambda: _stage_two_helpers(helpers) == 1, 'stage two helper')


def test_fast_claim_of_the_next_stage_keeps_the_third(aux, tmp_path, helpers, monkeypatch):
    from agent import tool_executor

    agent = root_agent([], tmp_path)
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)), (STAGE_THREE, (1,)))
    first = _dispatch_stage_one(agent, tmp_path, plan)
    _finish_and_deliver(helpers, first)

    real = tool_executor.dispatch_generated_tool

    def claim_before_bind(*args, **kwargs):
        raw = real(*args, **kwargs)
        data = rb._decode(raw)
        if data.get('status') == 'dispatched':
            _finish_and_deliver(helpers, data['delegation_id'])
        return raw

    monkeypatch.setattr(tool_executor, 'dispatch_generated_tool', claim_before_bind)
    before = set(_all_ids())
    agent._router_completion_ids = (first,)
    fl.begin_turn(agent, 'internal_notification')
    note = dr.plan_for_turn(agent, 'helper finished', [], 'internal_notification')
    second = _new_dispatch_ids(before)
    assert len(second) == 1
    assert 'last stage' not in note, note
    assert [s['tasks'] for s in ad.get_continuation_plan(second[0])['stages']] == [[2]]

    monkeypatch.setattr(tool_executor, 'dispatch_generated_tool', real)
    before = set(_all_ids())
    _completion_turn(agent, second[0])
    assert len(_new_dispatch_ids(before)) == 1, 'the claimed second stage lost its third'


def test_a_tail_that_cannot_attach_is_never_called_the_last_stage(
        aux, tmp_path, helpers, monkeypatch):
    agent = root_agent([], tmp_path)
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)), (STAGE_THREE, (1,)))
    first = _dispatch_stage_one(agent, tmp_path, plan)
    _finish_and_deliver(helpers, first)
    monkeypatch.setattr(rb, '_bind_plan', lambda *a, **k: False)
    before = set(_all_ids())

    agent._router_completion_ids = (first,)
    fl.begin_turn(agent, 'internal_notification')
    note = dr.plan_for_turn(agent, 'helper finished', [], 'internal_notification')

    assert len(_new_dispatch_ids(before)) == 1, 'stage two itself still starts'
    assert 'last stage' not in note, note
    assert 'stage 3' in note.lower() or STAGE_THREE[:20] in note, note
    assert 'not' in note.lower() and 'start' in note.lower(), note


def test_a_claimed_row_takes_one_continuation_once(aux, tmp_path, helpers):
    agent = root_agent([], tmp_path)
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)), (STAGE_THREE, (1,)))
    fl.begin_turn(agent, None)
    rb.begin(agent, plan, 'Build, review, release notes', dr.RouterSettings(backstop_calls=5))
    rows = run_batch(agent, [('delegate_task', {'tasks': [{'goal': 'unrelated lane'}]})])
    first = rows[0]['delegation_id']
    _finish_and_deliver(helpers, first)

    def make(stages):
        return {'version': 1, 'kind': rb.PLAN_KIND, 'delegation_id': first,
                'request_id': f'router:{first}', 'generation': 0, 'receipt_ids': [first],
                'accepted_at': time.time(), 'expires_at': time.time() + 600,
                'consumed': 0, 'session_id': '', 'stages': stages, 'tasks': [],
                'digests': []}

    assert ad.bind_continuation_plan(first, make([{'stage': 1, 'tasks': [1]}]))
    # No overwrite, and a spent plan is not replaced by a fresh one.
    assert not ad.bind_continuation_plan(first, make([{'stage': 2, 'tasks': [2]}]))
    assert ad.get_continuation_plan(first)['stages'] == [{'stage': 1, 'tasks': [1]}]
    assert ad.revoke_continuation_plan(first)
    assert not ad.bind_continuation_plan(first, make([{'stage': 2, 'tasks': [2]}]))


def test_a_delivered_row_takes_no_late_continuation(aux, tmp_path, helpers):
    agent = root_agent([], tmp_path)
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)))
    fl.begin_turn(agent, None)
    rb.begin(agent, plan, 'Build and review', dr.RouterSettings(backstop_calls=5))
    rows = run_batch(agent, [('delegate_task', {'tasks': [{'goal': 'unrelated lane'}]})])
    first = rows[0]['delegation_id']
    _finish_and_deliver(helpers, first)
    assert ad.complete_completion_delivery(first, f'claim-{first}')

    late = {'version': 1, 'kind': rb.PLAN_KIND, 'delegation_id': first, 'consumed': 0,
            'stages': [{'stage': 1, 'tasks': [1]}]}
    assert not ad.bind_continuation_plan(first, late), \
        'nothing would ever consume a plan bound after the completion was delivered'


def test_claimed_fast_stage_replay_starts_stage_two_exactly_once(aux, tmp_path, helpers):
    agent = root_agent([], tmp_path, session_id='octavia')
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)), (STAGE_THREE, (1,)))
    fl.begin_turn(agent, None)
    rb.begin(agent, plan, 'Build, review, release notes', dr.RouterSettings(backstop_calls=5))
    rows = run_batch(agent, [('delegate_task', {'tasks': [{'goal': STAGE_ONE}]})])
    first = rows[0]['delegation_id']
    _finish_and_deliver(helpers, first)
    assert rb.completion_directive(agent) is not None
    before = set(_all_ids())

    for _ in range(3):
        _completion_turn(root_agent([], tmp_path, session_id='octavia'), first)

    assert len(_new_dispatch_ids(before)) == 1
    _wait(lambda: _stage_two_helpers(helpers) == 1, 'stage two helper')
    time.sleep(0.2)
    assert _stage_two_helpers(helpers) == 1


# ── the root dispatching the stages itself: stage two runs once ────────────

def test_root_spawning_both_stages_itself_runs_stage_two_once(aux, tmp_path, helpers):
    agent = root_agent([], tmp_path)
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)))
    fl.begin_turn(agent, None)
    rb.begin(agent, plan, 'Build and review', dr.RouterSettings(backstop_calls=5))
    rows = run_batch(agent, [
        ('delegate_task', {'tasks': [{'goal': STAGE_ONE}]}),
        ('delegate_task', {'tasks': [{'goal': STAGE_TWO}]}),
    ])
    first, second = rows[0].get('delegation_id'), rows[1].get('delegation_id')
    assert first and second, rows
    assert rb.completion_directive(agent) is not None
    assert ad.get_continuation_plan(first) is None, 'two own dispatches bind no runtime chain'

    _finish_and_deliver(helpers, first)
    before = set(_all_ids())
    agent._router_completion_ids = (first,)
    fl.begin_turn(agent, 'internal_notification')
    dr.plan_for_turn(agent, 'helper finished', [], 'internal_notification')
    assert _new_dispatch_ids(before) == []
    _wait(lambda: _stage_two_helpers(helpers) >= 1, 'the root\'s own stage two helper')
    time.sleep(0.3)
    assert _stage_two_helpers(helpers) == 1


def test_a_bound_chain_refuses_the_roots_later_spawn_of_the_same_stage(
        aux, tmp_path, helpers):
    agent = root_agent([], tmp_path)
    plan = _staged((STAGE_ONE, ()), (STAGE_TWO, (0,)))
    fl.begin_turn(agent, None)
    rb.begin(agent, plan, 'Build and review', dr.RouterSettings(backstop_calls=5))
    rows = run_batch(agent, [
        ('delegate_task', {'tasks': [{'goal': STAGE_ONE}]}),
        writes(tmp_path, 1, 'between')[0],
        ('delegate_task', {'tasks': [{'goal': STAGE_TWO}]}),
    ])
    first = rows[0]['delegation_id']
    assert rows[2].get('error_type') == rb.HANDOFF_CODE, rows
    assert [s['tasks'] for s in ad.get_continuation_plan(first)['stages']] == [[1]]

    _finish_and_deliver(helpers, first)
    before = set(_all_ids())
    _completion_turn(agent, first)
    assert len(_new_dispatch_ids(before)) == 1
    _wait(lambda: _stage_two_helpers(helpers) == 1, 'stage two helper')
    time.sleep(0.3)
    assert _stage_two_helpers(helpers) == 1


# ── the lifecycle hand-back never replaces a running continuation ──────────

def _stage_one_delivered(agent, tmp_path, helpers, *stages):
    first = _dispatch_stage_one(agent, tmp_path, _staged(*stages))
    _finish_and_deliver(helpers, first)
    helpers.gate.clear()  # stage two (and anything else started) stays running
    return first


def test_lifecycle_leaves_the_staged_continuation_turn_as_the_router_armed_it(
        aux, tmp_path, helpers):
    agent = root_agent([], tmp_path)
    first = _stage_one_delivered(
        agent, tmp_path, helpers,
        (STAGE_ONE, ()), (STAGE_TWO, (0,)), (STAGE_THREE, (1,)))
    agent._router_completion_ids = (first,)
    fl.begin_turn(agent, 'internal_notification')
    note = dr.plan_for_turn(agent, 'helper finished', [], 'internal_notification')
    assert 'stage 2 is now running on helpers' in note, note
    armed = rb._state(agent)
    floor, receipts = armed.receipt_floor, [r.delegation_id for r in rb.turn_receipts(agent)]
    assert armed.no_spawn and armed.handoff and rb.turn_is_armed(agent)
    assert len(receipts) == 1

    wl.begin_turn(agent, 'helper finished', 'internal_notification', [first])

    assert rb._state(agent) is armed
    assert (armed.receipt_floor, armed.no_spawn) == (floor, True)
    assert [r.delegation_id for r in rb.turn_receipts(agent)] == receipts
    directive = rb.completion_directive(agent)
    assert directive is not None and directive.reason == rb.EXIT_REASON


def test_a_running_next_stage_gets_no_integration_helper_beside_it(aux, tmp_path, helpers):
    agent = root_agent([], tmp_path)
    first = _stage_one_delivered(
        agent, tmp_path, helpers,
        (STAGE_ONE, ()), (STAGE_TWO, (0,)), (STAGE_THREE, (1,)))
    agent._router_completion_ids = (first,)
    fl.begin_turn(agent, 'internal_notification')
    dr.plan_for_turn(agent, 'helper finished', [], 'internal_notification')
    wl.begin_turn(agent, 'helper finished', 'internal_notification', [first])
    before = set(_all_ids())

    rows = run_batch(agent, writes(tmp_path, 4, 'late'))

    assert _new_dispatch_ids(before) == [], rows
    assert all(r.get('error_type') == rb.HANDOFF_CODE for r in rows), rows


def test_completion_entry_does_not_verify_while_the_next_stage_runs(aux, tmp_path, helpers):
    agent = root_agent([], tmp_path)
    first = _stage_one_delivered(agent, tmp_path, helpers, (STAGE_ONE, ()), (STAGE_TWO, (0,)))
    before = set(_all_ids())

    result = _completion_turn(agent, first, 'Everything is verified.')

    started = _new_dispatch_ids(before)
    assert len(started) == 1
    assert ad.get_durable_delegation(started[0])['state'] == 'running'
    assert result.get('work_status') == 'dispatch_ack', result  # the new stage is accepted, running


def test_root_work_on_a_staged_completion_turn_is_refused_not_handed_back(
        aux, tmp_path, helpers):
    """Real loop: three foreground writes after stage 2 started.  The turn ends
    at the stage-two acknowledgment; no write runs and no "final report"
    helper starts beside the review stage."""
    agent = root_agent([], tmp_path)
    first = _stage_one_delivered(agent, tmp_path, helpers, (STAGE_ONE, ()), (STAGE_TWO, (0,)))
    before = set(_all_ids())
    agent.client.chat.completions.create.side_effect = [
        _tools(_call('write_file', {'path': str(tmp_path / f'k{i}.txt'), 'content': 'x'}))
        for i in range(3)
    ] + [_text('All stages are done and verified.')] + [_text('extra')] * 3

    agent.run_conversation(
        f'[helper {first} finished]', persist_user_display_kind='internal_notification',
        completion_delegation_ids=[first],
    )

    assert len(_new_dispatch_ids(before)) == 1  # stage two only
    assert [i for i in range(3) if (tmp_path / f'k{i}.txt').exists()] == []
    assert not any(wl.HANDBACK_GOAL[:30] in g['user'] for g in helpers.goals)
    assert rb._state(agent).no_spawn is True
