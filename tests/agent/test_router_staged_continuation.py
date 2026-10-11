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

from agent import delegation_router as dr, router_backstop as rb
from agent import foreground_time_limit as fl
from tests.agent.test_delegation_checkpoint import run_batch
from tests.agent.test_delegation_router import T, aux  # noqa: F401  (aux fixture)
from tests.agent.test_handoff_completion_seam import _text
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
