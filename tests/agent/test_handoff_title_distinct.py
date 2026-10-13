"""Distinct, truthful hand-off titles inside one chat, and per-stage cards.

The 2026-10-12 incident: two hand-offs in one chat (13:04 and 13:06) both said
"restoring the Clover gateway", so the user thought the card had duplicated.
And a router-planned staged dispatch put the plan's raw task text in the card
titles and the WHOLE plan's goal on one task.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent import handoff_notes as hn
from agent import handoff_title as ht
from agent import router_backstop as rb
from agent.delegation_router import PlanTask, Verdict


# ── the pure helpers ───────────────────────────────────────────────────────

def test_a_title_not_used_recently_is_kept():
    assert ht.distinct_title('restoring the Clover gateway', []) == 'restoring the Clover gateway'
    assert ht.distinct_title('restoring the Clover gateway', ['fixing the login bug']) == (
        'restoring the Clover gateway')


@pytest.mark.parametrize('completion', [True, False])
def test_a_repeated_title_becomes_a_different_one_about_the_same_subject(completion):
    out = ht.distinct_title('restoring the Clover gateway', ['restoring the Clover gateway'],
                            completion=completion)
    assert ht.title_key(out) != ht.title_key('restoring the Clover gateway')
    assert 'Clover gateway' in out
    assert out.split()[0].endswith('ing')
    assert len(out) <= ht.TITLE_CHARS


def test_a_completion_helper_checks_what_the_earlier_one_did():
    assert ht.distinct_title('restoring the Clover gateway', ['restoring the Clover gateway'],
                             completion=True) == 'checking the Clover gateway'


def test_a_chain_of_hand_offs_never_repeats_any_title():
    recent = ['restoring the Clover gateway']
    seen = set(map(ht.title_key, recent))
    for _ in range(8):
        title = ht.distinct_title('restoring the Clover gateway', recent, completion=True)
        assert ht.title_key(title) not in seen, title
        seen.add(ht.title_key(title))
        recent.append(title)


def test_the_default_title_gets_a_natural_variant_too():
    out = ht.distinct_title(ht.FALLBACK_TITLE, [ht.FALLBACK_TITLE], completion=True)
    assert out != ht.FALLBACK_TITLE and out.split()[0].endswith('ing')


def test_comparison_ignores_case_and_punctuation():
    assert ht.is_recent_title('Restoring the Clover Gateway.', ['restoring the clover gateway'])


# ── the stage helper ───────────────────────────────────────────────────────

def test_a_stage_is_titled_from_its_own_task_with_its_own_goal():
    out = ht.stage_handoff(['Fix the subagent sandbox bug so subagents work in the bugs folder'])
    assert out['work'].startswith('fixing the subagent sandbox bug')
    assert len(out['work']) <= ht.TITLE_CHARS
    assert out['outcome'] and len(out['outcome']) <= ht.GOAL_CHARS
    assert out['work'] != 'Fix the subagent sandbox bug so subagents work in the bugs folder'


def test_two_stages_get_different_titles_and_goals():
    a = ht.stage_handoff(['Fix the subagent sandbox bug so subagents work in the bugs folder'])
    b = ht.stage_handoff(['Remove the old copy of the project folder the user asked about'])
    assert a['work'] != b['work'] and a['outcome'] != b['outcome']


# ── the recent-titles ledger ───────────────────────────────────────────────

def test_titles_are_remembered_per_chat_and_expire(monkeypatch):
    hn.remember_title('sess-a', 'restoring the Clover gateway')
    hn.remember_title('sess-b', 'fixing the login bug')
    assert hn.recent_titles('sess-a') == ['restoring the Clover gateway']
    assert hn.recent_titles('sess-b') == ['fixing the login bug']
    assert hn.recent_titles('sess-a', within=-1) == []  # outside the window
    assert hn.recent_titles('') == []


# ── the model path ─────────────────────────────────────────────────────────

def test_the_model_is_told_the_earlier_titles_and_a_repeat_is_dropped(monkeypatch):
    seen = {}

    def fake(kwargs, timeout):
        seen['system'] = kwargs['messages'][0]['content']
        return json.dumps({'title': 'restoring the Clover gateway', 'goal': 'gateway back up',
                           'known': ['x']})

    monkeypatch.setattr(hn, '_timed_call', fake)
    brief = hn.handoff_title('NOTES', recent_titles=['restoring the Clover gateway'],
                             completion=True)

    assert 'restoring the Clover gateway' in seen['system']
    assert 'what is different' in seen['system'] and 'must not repeat' in seen['system']
    assert brief.title == '', 'a title equal to an earlier one is dropped, not shown twice'
    assert brief.goal == 'gateway back up'


def test_a_different_model_title_is_kept(monkeypatch):
    monkeypatch.setattr(hn, '_timed_call', lambda kw, t: json.dumps(
        {'title': 'checking the gateway fix', 'goal': 'fix confirmed', 'known': []}))
    brief = hn.handoff_title('NOTES', recent_titles=['restoring the Clover gateway'])
    assert brief.title == 'checking the gateway fix'


def test_no_earlier_titles_leaves_the_prompt_unchanged(monkeypatch):
    seen = {}
    monkeypatch.setattr(hn, '_timed_call', lambda kw, t: seen.setdefault('s', kw['messages'][0]['content']) and '{}')
    hn.handoff_title('NOTES')
    assert seen['s'] == hn._TITLE_SYSTEM


# ── the backstop wiring (fallback path, no model) ──────────────────────────

def _agent(session='sess-x', completion=()):
    return SimpleNamespace(session_id=session, _router_completion_ids=tuple(completion))


def test_a_second_hand_off_in_the_chat_gets_a_different_title_without_a_model():
    agent = _agent()
    first = rb._distinct_handoff(agent, {'work': 'restoring the Clover gateway', 'outcome': 'gateway up'})
    agent2 = _agent(completion=['deleg_1'])
    second = rb._distinct_handoff(agent2, {'work': 'restoring the Clover gateway', 'outcome': 'gateway up'})
    third = rb._distinct_handoff(agent2, {'work': 'restoring the Clover gateway', 'outcome': 'gateway up'})

    assert first['work'] == 'restoring the Clover gateway'
    assert second['work'] == 'checking the Clover gateway'
    assert len({first['work'], second['work'], third['work']}) == 3
    assert second['outcome'] == 'gateway up'


def test_another_chat_is_not_affected():
    rb._distinct_handoff(_agent('one'), {'work': 'restoring the Clover gateway', 'outcome': 'g'})
    other = rb._distinct_handoff(_agent('two'), {'work': 'restoring the Clover gateway', 'outcome': 'g'})
    assert other['work'] == 'restoring the Clover gateway'


# ── staged / parallel plans dispatched by the runtime ──────────────────────

PLAN = Verdict(mode='staged', reason='1.5.4 released after bugs and cleanup', tasks=(
    PlanTask(goal='Fix the subagent sandbox bug so subagents work in the bugs folder'),
    PlanTask(goal='Remove the old copy of the project folder the user asked about', depends_on=(0,)),
    PlanTask(goal='Release 1.5.4 once the fixes are merged', depends_on=(0, 1)),
))


def test_a_runtime_dispatched_stage_gets_a_short_title_and_its_own_goal():
    handoff = rb._runtime_handoff(PLAN, 'fix everything and release', stage_tasks=[PLAN.tasks[0]])

    assert handoff['work'].startswith('fixing the subagent sandbox bug')
    assert len(handoff['work']) <= ht.TITLE_CHARS
    assert '1.5.4 released' not in handoff['outcome'], 'never the whole plan\'s goal on one task'
    assert 'released after' not in handoff['outcome']


def test_each_stage_task_has_a_short_title_not_the_raw_plan_text():
    titles = [rb._task_title_arg(PLAN, i)['title'] for i in range(3)]

    assert len(set(titles)) == 3
    for task, title in zip(PLAN.tasks, titles):
        assert title != task.goal and len(title) <= ht.TITLE_CHARS


def test_single_task_dispatches_keep_their_own_card_wording_but_still_carry_a_task_title():
    single = Verdict(mode='single', reason='', tasks=(PlanTask(goal='Compare three GPUs'),))
    assert rb._task_title_arg(single, 0) == {'title': 'comparing three GPUs'}
    assert rb._plan_stage_handoff(single, [single.tasks[0]]) is None


def test_a_dispatch_that_carries_the_whole_plan_is_not_a_stage():
    parallel = Verdict(mode='parallel', reason='', tasks=PLAN.tasks[:2])
    assert rb._plan_stage_handoff(parallel, list(parallel.tasks)) is None
