"""One mode-choice contract: direct, single helper, parallel helpers, staged (C1.5 A1).

Real ``AIAgent`` roots, the real prompt builder and conversation loop, a local
mock provider over HTTP and a fake monotonic clock.  Contract:

* A root agent with ``delegate_task`` on a surface that can deliver a detached
  helper gets ONE guidance block that names the four ways to work, lets the
  user's explicit instruction win, and tells the model to keep internal limits
  private.  The old plan-first and hand-off paragraphs are gone.
* A root that cannot get a result back later (cron, one-shot / batch runs,
  kanban workers, the stateless API server) gets a short delivery-neutral
  plan-and-verify note instead: no "end your turn", no "don't poll".
* Subagents, delegated-child contexts and agents without ``delegate_task`` get
  neither.  Every surface gets exactly one of {mode block, fallback, nothing}.
* The system prompt AND the tool schemas the provider receives are
  byte-identical across an ordinary turn, a helper-completion turn and a
  rebuilt agent for the same session.
"""
from __future__ import annotations

import json
import re

import pytest

from tests.agent.test_foreground_time_limit_loop import (  # noqa: F401
    _limit_on,
    _write_limit,
    clock,
    make_agent,
    provider,
    text,
)

HEADING = '# Choosing how to work'
FALLBACK_HEADING = '# Plan first'
OLD_HEADINGS = ('# Plan-first delegation', '# Keep the chat free')


@pytest.fixture(autouse=True)
def _unbind_session_context():
    yield
    import gateway.session_context as sc

    for var in sc._VAR_MAP.values():
        var.set(sc._UNSET)
    sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)


def _prompt(agent) -> str:
    return agent._build_system_prompt()


def _block() -> str:
    from agent.prompt_builder import DELEGATION_MODE_GUIDANCE

    return DELEGATION_MODE_GUIDANCE


# ── the block itself ───────────────────────────────────────────────────────

def test_block_is_short_and_has_no_numbers():
    block = _block()
    assert block.startswith(HEADING)
    assert len(block.split()) <= 180
    assert not re.search(r'\d', block)


def test_block_tells_the_model_what_big_means_and_what_to_do_on_rejection():
    lowered = _block().lower()
    assert 'dozen' in lowered  # an anchor for "many items", not just a feeling
    assert 'accepted dispatch' in lowered
    assert 'rejected' in lowered and 'still yours' in lowered


def test_block_names_the_four_modes_and_the_delegation_tool():
    lowered = _block().lower()
    for phrase in ('direct', 'single helper', 'parallel helpers', 'staged', 'delegate_task'):
        assert phrase in lowered, phrase


def test_block_lets_the_users_explicit_instruction_win_both_ways():
    lowered = _block().lower()
    assert 'do it yourself' in lowered
    assert 'use helpers' in lowered
    assert 'faster to start' in lowered  # never pick direct for that reason


def test_block_carries_the_worker_handoff_and_verification_rules():
    lowered = _block().lower()
    for phrase in ('scope', 'inputs', 'acceptance', 'ownership', 'poll', 'verify'):
        assert phrase in lowered, phrase
    # staged work moves on only after the previous stage's artifacts check out
    assert 'artifact' in lowered


def test_block_keeps_internal_limits_private_but_blockers_honest():
    lowered = _block().lower()
    assert 'never mention' in lowered
    assert 'blocker' in lowered


# ── who gets it ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('platform', ['telegram', 'discord', 'slack', 'cli', 'tui'])
def test_root_agent_with_delegate_task_gets_the_block_once(provider, platform):
    agent = make_agent(provider, platform=platform)
    assert 'delegate_task' in agent.valid_tool_names
    prompt = _prompt(agent)
    assert prompt.count(HEADING) == 1
    assert _block() in prompt


def test_the_old_paragraphs_are_gone(provider):
    prompt = _prompt(make_agent(provider))
    for heading in OLD_HEADINGS:
        assert heading not in prompt
    import agent.prompt_builder as pb

    assert not hasattr(pb, 'PLAN_FIRST_DELEGATION_GUIDANCE')
    assert not hasattr(pb, 'FOREGROUND_HANDOFF_GUIDANCE')


def test_block_sits_in_the_stable_part_of_the_prompt(provider):
    parts = make_agent(provider)._build_system_prompt_parts()
    assert HEADING in parts['stable']


def test_the_block_is_the_only_delegation_guidance_difference(provider):
    on = make_agent(provider)._build_system_prompt_parts()['stable']
    off = make_agent(
        provider, enabled_toolsets=['todo', 'file', 'terminal'],
    )._build_system_prompt_parts()['stable']
    assert _block() in on
    assert _block() not in off


def _which(prompt: str) -> set:
    """Which delegation guidance did this prompt get?"""
    got = set()
    if HEADING in prompt:
        got.add('mode')
    if FALLBACK_HEADING in prompt:
        got.add('fallback')
    return got


def test_a_subagent_gets_neither(provider):
    agent = make_agent(provider, platform='subagent')
    agent._delegate_depth = 1
    agent._subagent_id = 'sa-1'
    assert _which(_prompt(agent)) == set()


def test_a_delegated_child_context_gets_neither(provider):
    from agent.delegation_context import delegated_child_context

    agent = make_agent(provider)
    with delegated_child_context():
        assert _which(_prompt(agent)) == set()


def test_a_review_fork_gets_neither(provider):
    agent = make_agent(provider)
    agent._is_background_review_fork = True
    assert _which(_prompt(agent)) == set()


def test_without_delegate_task_there_is_neither(provider):
    agent = make_agent(provider, enabled_toolsets=['todo', 'file', 'terminal'])
    assert 'delegate_task' not in agent.valid_tool_names
    assert _which(_prompt(agent)) == set()


def test_cron_gets_only_the_fallback(provider):
    from gateway.session_context import declare_stateless_channel

    declare_stateless_channel()
    agent = make_agent(provider, platform='cron')
    assert _which(_prompt(agent)) == {'fallback'}


@pytest.mark.parametrize('attr,value', [
    ('_delegation_checkpoint_exempt', 'oneshot'),
    ('_delegation_checkpoint_exempt', 'batch_runner'),
    ('_foreground_limit_exempt', 'single_query'),
    ('_foreground_limit_exempt', 'background'),
])
def test_oneshot_and_other_noninteractive_runs_get_only_the_fallback(provider, attr, value):
    agent = make_agent(provider, platform='cli')
    setattr(agent, attr, value)
    assert _which(_prompt(agent)) == {'fallback'}


def test_api_server_gets_only_the_fallback(provider):
    from gateway.session_context import declare_stateless_channel

    declare_stateless_channel()
    agent = make_agent(provider, platform='api_server')
    assert _which(_prompt(agent)) == {'fallback'}


def test_kanban_worker_gets_only_the_fallback(provider, monkeypatch):
    monkeypatch.setenv('CLOVER_KANBAN_TASK', 't-1')
    agent = make_agent(provider, platform='cli')
    assert _which(_prompt(agent)) == {'fallback'}


def test_fallback_is_delivery_neutral_but_keeps_plan_and_verify():
    from agent.prompt_builder import DELEGATION_PLAN_FALLBACK_GUIDANCE as fallback

    lowered = fallback.lower()
    assert fallback.startswith(FALLBACK_HEADING)
    assert len(fallback.split()) <= 60
    for phrase in ('plan', 'verify', 'quick', 'explicit'):
        assert phrase in lowered, phrase
    for phrase in ('end your turn', 'poll', 'tell the user'):
        assert phrase not in lowered, phrase


def test_the_fallback_prompt_is_byte_stable_across_turns(clock, provider):
    from gateway.session_context import declare_stateless_channel

    declare_stateless_channel()
    agent = make_agent(provider, platform='cron', session_id='fallback-stable-1')
    provider.queue[:] = [text('one'), text('two')]
    first = agent.run_conversation('job', conversation_history=[], task_id='fb-1')
    agent.run_conversation('again', conversation_history=first['messages'], task_id='fb-2')

    systems = [_system_content(r) for r in provider.captured]
    assert len(systems) == 2
    assert FALLBACK_HEADING in systems[0] and HEADING not in systems[0]
    assert systems[0] == systems[1]


@pytest.mark.parametrize('value', [0, None, False, 'off'])
def test_choosing_a_mode_does_not_depend_on_the_time_limit(provider, value):
    # The limit is a backstop; mode choice is guidance for every root that can
    # hand work to a helper, with the limit on or off.
    _write_limit(value)
    agent = make_agent(provider)
    assert HEADING in _prompt(agent)


# ── cache safety ───────────────────────────────────────────────────────────

def _system_content(request: dict) -> str:
    first = (request.get('messages') or [{}])[0]
    assert first.get('role') == 'system'
    content = first.get('content')
    if isinstance(content, list):
        content = ''.join(p.get('text', '') for p in content if isinstance(p, dict))
    return content


def _tools_bytes(request: dict) -> str:
    # Order matters to a prompt cache, so no sort_keys.
    return json.dumps(request.get('tools'), ensure_ascii=False)


def test_prompt_and_tool_schemas_are_byte_identical_across_turns_and_rebuilds(
        clock, provider):
    session_id = 'mode-stable-1'
    agent = make_agent(provider, session_id=session_id)
    provider.queue[:] = [text('first answer'), text('integrated'), text('after rebuild')]

    first = agent.run_conversation('big job', conversation_history=[], task_id='ms-1')
    # A helper finishes later; the completion re-enters as an internal turn.
    clock.advance(400)
    second = agent.run_conversation(
        'helper finished', conversation_history=first['messages'], task_id='ms-2',
        persist_user_display_kind='internal_notification')
    # Config changes between turns must not reach the cached prefix.
    _write_limit(0)
    rebuilt = make_agent(provider, session_id=session_id)
    third = rebuilt.run_conversation(
        'one more', conversation_history=second['messages'], task_id='ms-3')

    assert len(provider.captured) == 3
    systems = [_system_content(r) for r in provider.captured]
    tools = [_tools_bytes(r) for r in provider.captured]
    assert HEADING in systems[0]
    assert all(s == systems[0] for s in systems), 'system prompt changed mid-conversation'
    assert tools[0] != 'null'
    assert all(t == tools[0] for t in tools), 'tool schemas changed mid-conversation'
    assert third['final_response'] == 'after rebuild'
