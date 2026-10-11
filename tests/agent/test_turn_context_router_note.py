"""The work plan rides the API copy of the current user turn only (C1.5 router).

Real ``AIAgent.run_conversation``, a real ``SessionDB`` in the scratch
``CLOVER_HOME``, a local mock provider over HTTP.  Only the auxiliary-client
boundary is faked (the router's model).  Contract:

* the plan is appended to the API copy of THIS turn's user message, beside the
  prefetch / plugin context, and nowhere else;
* the stored user text stays clean; no extra message is inserted;
* the system prompt and the tool schemas are byte-identical across turns, and a
  later turn replays the earlier turn's exact bytes (prompt cache stays warm);
* internal (helper-completion) turns and skipped turns get no plan.
"""
from __future__ import annotations

import json

import pytest

from agent.turn_context import compose_user_api_content
from tests.agent.test_delegation_router import T, aux, verdict_json  # noqa: F401
from tests.agent.test_foreground_time_limit_loop import (  # noqa: F401
    _limit_on,
    clock,
    make_agent,
    provider,
    text,
)

MARKER = '[Clover runtime] work plan:'
ASK = 'Compare the pricing pages of Notion, Linear and Asana and give me one table.'

pytestmark = pytest.mark.real_delegation_router


@pytest.fixture(autouse=True)
def _unbind_session_context():
    yield
    import gateway.session_context as sc

    for var in sc._VAR_MAP.values():
        var.set(sc._UNSET)
    sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)


PARALLEL = verdict_json('parallel', [T('Notion pricing'), T('Linear pricing'), T('Asana pricing')])


def _user_contents(request: dict) -> list:
    return [m['content'] for m in request['messages'] if m.get('role') == 'user']


# ── the composer ───────────────────────────────────────────────────────────

def test_compose_appends_the_plan_after_prefetch_and_plugin_context():
    out = compose_user_api_content('hello', 'recalled fact', 'plugin said', 'WORK PLAN BLOCK')
    assert out.startswith('hello')
    assert out.index('recalled fact') < out.index('plugin said') < out.index('WORK PLAN BLOCK')


def test_compose_with_only_a_plan_still_returns_the_composed_copy():
    assert compose_user_api_content('hello', '', '', 'WORK PLAN BLOCK') == 'hello\n\nWORK PLAN BLOCK'


def test_compose_without_a_plan_is_unchanged():
    assert compose_user_api_content('hello', '', '') is None
    assert compose_user_api_content('hello', '', 'ctx') == 'hello\n\nctx'


def test_compose_never_touches_multimodal_content():
    parts = [{'type': 'text', 'text': 'hi'}]
    assert compose_user_api_content(parts, '', '', 'WORK PLAN BLOCK') is None


# ── a real turn ────────────────────────────────────────────────────────────

def test_the_plan_is_on_the_wire_copy_and_the_stored_text_is_clean(provider, aux):
    aux.replies[:] = [PARALLEL]
    provider.queue[:] = [text('On it.')]
    agent = make_agent(provider)

    result = agent.run_conversation(ASK, conversation_history=[], task_id='rn1')

    wire_user = _user_contents(provider.captured[0])[-1]
    assert wire_user.startswith(ASK)
    assert MARKER in wire_user
    assert 'Notion pricing' in wire_user and 'delegate_task' in wire_user

    stored = [m for m in result['messages'] if m['role'] == 'user']
    assert len(stored) == 1
    assert stored[0]['content'] == ASK
    assert MARKER not in stored[0]['content']


def test_no_extra_message_is_inserted(provider, aux):
    aux.replies[:] = [PARALLEL]
    provider.queue[:] = [text('On it.')]
    agent = make_agent(provider)

    result = agent.run_conversation(ASK, conversation_history=[], task_id='rn2')

    assert [m['role'] for m in result['messages']] == ['user', 'assistant']
    roles = [m['role'] for m in provider.captured[0]['messages']]
    assert roles == ['system', 'user']


def test_the_note_is_persisted_as_the_sent_bytes_beside_clean_content(provider, aux, tmp_path):
    from clover_state import SessionDB

    aux.replies[:] = [PARALLEL]
    provider.queue[:] = [text('On it.')]
    db = SessionDB(tmp_path / 'state.db')
    agent = make_agent(provider, session_db=db, session_id='router-note-1')

    agent.run_conversation(ASK, conversation_history=[], task_id='rn3')

    rows = [r for r in db.get_messages('router-note-1') if r['role'] == 'user']
    assert len(rows) == 1
    assert rows[0]['content'] == ASK
    assert rows[0]['api_content'] == _user_contents(provider.captured[0])[-1]


def test_system_prompt_and_tools_are_byte_stable_and_the_next_turn_replays_the_note(provider, aux):
    aux.replies[:] = [PARALLEL, verdict_json('direct', reason='tiny')]
    provider.queue[:] = [text('On it.'), text('Sure, 4.')]
    agent = make_agent(provider)

    first = agent.run_conversation(ASK, conversation_history=[], task_id='rn4a')
    agent.run_conversation('What is 2+2?', conversation_history=first['messages'], task_id='rn4b')

    req1, req2 = provider.captured[0], provider.captured[1]
    assert req1['messages'][0] == req2['messages'][0], 'system prompt bytes changed between turns'
    assert json.dumps(req1.get('tools'), sort_keys=True) == json.dumps(req2.get('tools'), sort_keys=True)
    # turn 2 replays turn 1's user message exactly as sent (cached prefix intact)
    assert _user_contents(req2)[0] == _user_contents(req1)[0]
    assert MARKER in _user_contents(req2)[0]
    # the new turn's own plan is on the new turn's message only
    assert MARKER in _user_contents(req2)[1]
    assert _user_contents(req2)[1].startswith('What is 2+2?')


def test_a_direct_verdict_carries_a_short_direct_plan(provider, aux):
    aux.replies[:] = [verdict_json('direct', reason='one lookup')]
    provider.queue[:] = [text('Done.')]
    agent = make_agent(provider)

    agent.run_conversation('What is the capital of Peru?', conversation_history=[], task_id='rn5')

    wire_user = _user_contents(provider.captured[0])[-1]
    assert f'{MARKER} direct' in wire_user


def test_a_router_failure_leaves_the_turn_exactly_as_before(provider, aux):
    aux.replies[:] = [RuntimeError('aux provider down')]
    provider.queue[:] = [text('Fine.')]
    agent = make_agent(provider)

    result = agent.run_conversation(ASK, conversation_history=[], task_id='rn6')

    assert _user_contents(provider.captured[0])[-1] == ASK
    assert result['final_response'] == 'Fine.'


def test_a_pure_acknowledgement_makes_no_router_call_and_no_plan(provider, aux):
    provider.queue[:] = [text("You're welcome.")]
    agent = make_agent(provider)

    agent.run_conversation('thanks!', conversation_history=[], task_id='rn7')

    assert aux.router_calls == []
    assert _user_contents(provider.captured[0])[-1] == 'thanks!'


def test_an_internal_notification_turn_is_not_routed(provider, aux):
    provider.queue[:] = [text('Checked.')]
    agent = make_agent(provider)

    agent.run_conversation(
        '[ASYNC DELEGATION COMPLETE — d-1] helper finished',
        conversation_history=[], task_id='rn8',
        persist_user_display_kind='internal_notification',
    )

    assert aux.router_calls == []
    assert MARKER not in _user_contents(provider.captured[0])[-1]


def test_a_surface_without_a_foreground_limit_is_not_routed(provider, aux):
    provider.queue[:] = [text('Ok.')]
    agent = make_agent(provider, platform='cron')

    agent.run_conversation(ASK, conversation_history=[], task_id='rn9')

    assert aux.router_calls == []
    assert _user_contents(provider.captured[0])[-1] == ASK


def test_an_agent_without_delegate_task_is_not_routed(provider, aux):
    provider.queue[:] = [text('Ok.')]
    agent = make_agent(provider, enabled_toolsets=['todo', 'file', 'terminal'])
    assert 'delegate_task' not in agent.valid_tool_names

    agent.run_conversation(ASK, conversation_history=[], task_id='rn10')

    assert aux.router_calls == []


def test_the_router_can_be_turned_off_in_config(provider, aux):
    import yaml
    from clover_constants import get_clover_home

    from clover_cli.config_defaults import DEFAULT_CONFIG

    (get_clover_home() / 'config.yaml').write_text(yaml.safe_dump({
        '_config_version': DEFAULT_CONFIG['_config_version'],
        'delegation': {'foreground_time_limit_seconds': 300, 'router': {'enabled': False}},
    }))
    provider.queue[:] = [text('Ok.')]
    agent = make_agent(provider)

    agent.run_conversation(ASK, conversation_history=[], task_id='rn11')

    assert aux.router_calls == []
    assert _user_contents(provider.captured[0])[-1] == ASK
