"""delegate_task under the per-message foreground time limit (C1.4 P1, 5b-5c).

Real ``delegate_task`` entry, real root ``AIAgent``, real async registry in a
scratch ``CLOVER_HOME`` and a fake monotonic clock.  Only child construction
and (for the dispatch tests) the child runner are stubbed.

Contract under test:

* On an async-delivery surface every ``delegate_task`` of a limited turn runs
  as a background helper (whole turn, not only after expiry), the result text
  names the mode, and a helper that cannot start is an ERROR, never inline.
* On a surface with no async delivery (API server) the call runs inline with a
  per-call child timeout equal to the episode's remaining time (never the 30 s
  config floor, never "unlimited"); every queued batch child re-checks the
  remainder when it starts; after 100% it is refused.
"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import tools.async_delegation as ad
import tools.delegate_tool as dt
from agent import foreground_time_limit as fl


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(fl, '_now', c)
    return c


@pytest.fixture(autouse=True)
def _clean_registry():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


@pytest.fixture(autouse=True)
def _limit_config():
    from clover_constants import get_clover_home

    from clover_cli.config_defaults import DEFAULT_CONFIG

    (get_clover_home() / 'config.yaml').write_text(yaml.safe_dump({
        '_config_version': DEFAULT_CONFIG['_config_version'],
        'delegation': {'foreground_time_limit_seconds': 300},
    }))


@pytest.fixture(autouse=True)
def _unbind_session_context():
    yield
    import gateway.session_context as sc

    for var in sc._VAR_MAP.values():
        var.set(sc._UNSET)
    sc._SESSION_ASYNC_DELIVERY.set(sc._UNSET)


_CREDS = {
    'model': 'm', 'provider': None, 'base_url': None, 'api_key': None,
    'api_mode': None, 'command': None, 'args': None,
}


def _root(platform: str = 'telegram'):
    from run_agent import AIAgent

    agent = AIAgent(
        provider='custom', api_mode='chat_completions',
        base_url='http://127.0.0.1:1/v1', api_key='local-test-credential',
        model='chosen-orchestrator', model_pinned=True,
        enabled_toolsets=['todo', 'delegation', 'file', 'terminal'],
        max_iterations=6, quiet_mode=True, tool_progress_mode='off',
        skip_context_files=True, load_soul_identity=False,
        skip_memory=True, skip_background_review=True, platform=platform,
    )
    fl.begin_turn(agent)
    return agent


class _FakeChild:
    def __init__(self, index: int):
        self._delegate_role = 'leaf'
        self._subagent_id = f'sa-fg-{index}'
        self.session_id = f'child-session-{index}'
        self.closed = 0

    def close(self):
        self.closed += 1

    def get_activity_summary(self):
        return {'api_call_count': 0, 'current_tool': None,
                'last_activity_ts': time.time()}


class Harness:
    def __init__(self) -> None:
        self.built: list[_FakeChild] = []
        self.ran_goals: list[str] = []


@pytest.fixture
def harness(monkeypatch) -> Harness:
    """Stub child construction + runner (dispatch-level tests)."""
    h = Harness()

    def _build(**kw):
        child = _FakeChild(kw['task_index'])
        h.built.append(child)
        kw['parent_agent']._active_children.append(child)
        return child

    def _run(task_index, goal, child, parent_agent, **kw):
        h.ran_goals.append(goal)
        return {'task_index': task_index, 'status': 'completed',
                'summary': f'done: {goal}', 'api_calls': 1,
                'duration_seconds': 0.0, 'model': 'm', 'exit_reason': 'completed'}

    monkeypatch.setattr(dt, '_build_child_agent', _build)
    monkeypatch.setattr(dt, '_run_single_child', _run)
    monkeypatch.setattr(dt, '_resolve_delegation_credentials', lambda *a, **k: _CREDS)
    monkeypatch.setattr(dt, '_get_max_async_children', lambda: 1)
    return h


def _delegate(root, **kw) -> dict:
    kw.setdefault('goal', 'Build the reusable explainer and verify every output.')
    return json.loads(dt.delegate_task(parent_agent=root, **kw))


def _run_batch(agent, calls, task_id='fg-limit-delegate'):
    """Drive the real tool executor funnel (admission + dispatch)."""
    tool_calls = [
        SimpleNamespace(id=f'call-{i}', function=SimpleNamespace(
            name=n, arguments=json.dumps(a)))
        for i, (n, a) in enumerate(calls)]
    wire = [{'id': tc.id, 'type': 'function',
             'function': {'name': tc.function.name, 'arguments': tc.function.arguments}}
            for tc in tool_calls]
    messages = [{'role': 'user', 'content': 'do the job'},
                {'role': 'assistant', 'content': None, 'tool_calls': wire}]
    agent._execute_tool_calls(SimpleNamespace(tool_calls=tool_calls), messages, task_id)
    out = []
    for message in messages[2:]:
        try:
            out.append(json.loads(message['content']))
        except (TypeError, ValueError):
            out.append(message['content'])
    return out


def _no_async_surface() -> None:
    """The API-server shape: a finite session that cannot take a late result."""
    from gateway.session_context import declare_stateless_channel

    declare_stateless_channel()


# ── async-delivery surfaces: the whole turn is background ──────────────────

def test_every_delegate_call_in_a_limited_turn_is_background(clock, harness):
    root = _root()
    result = _delegate(root)  # no background flag: the limit applies it

    assert result['status'] == 'dispatched', result
    assert result['mode'] == 'background'


def test_forced_background_result_text_names_the_mode(clock, harness):
    root = _root()
    result = _delegate(root, background=False)

    text = json.dumps(result).lower()
    assert result['status'] == 'dispatched'
    assert 'time limit' in text
    assert 'background' in result['note'].lower()
    assert result.get('foreground_time_limit') is True


def test_unlimited_turn_keeps_a_synchronous_call_synchronous(clock, harness, monkeypatch):
    from clover_constants import get_clover_home

    from clover_cli.config_defaults import DEFAULT_CONFIG

    (get_clover_home() / 'config.yaml').write_text(yaml.safe_dump({
        '_config_version': DEFAULT_CONFIG['_config_version'],
        'delegation': {'foreground_time_limit_seconds': 0},
    }))
    root = _root()
    result = _delegate(root)

    assert 'results' in result and result.get('mode') != 'background'
    assert harness.ran_goals


def test_subagent_roots_are_not_forced_into_background(clock, harness, monkeypatch):
    monkeypatch.setattr(dt, '_get_max_spawn_depth', lambda: 3)
    root = _root()
    root._delegate_depth = 1  # a child, not a conversational root
    fl.begin_turn(root)

    result = _delegate(root)

    assert 'results' in result


def test_helper_that_cannot_start_is_an_error_not_an_inline_run(clock, harness, monkeypatch):
    import tools.async_delegation as async_mod

    root = _root()
    monkeypatch.setattr(async_mod, 'has_async_capacity', lambda *_a, **_k: False)

    result = _delegate(root)

    assert result['status'] == 'rejected'
    assert result['started'] is False
    assert not harness.ran_goals, 'work ran inline although the helper never started'
    assert not harness.built, 'children were built for a rejected dispatch'


def test_dispatch_refusal_after_children_are_built_is_an_error_too(clock, harness, monkeypatch):
    root = _root()
    monkeypatch.setattr(
        ad, 'dispatch_async_delegation_batch',
        lambda **kw: {'status': 'rejected', 'reason': 'schedule', 'error': 'pool shut down'})

    result = _delegate(root)

    assert result['status'] == 'rejected'
    assert result['reason'] == 'schedule'
    assert not harness.ran_goals
    assert all(c.closed for c in harness.built)


def test_no_async_surface_has_no_limit_and_inline_is_the_base_behaviour(clock, harness, monkeypatch):
    """Fix round 2: the API server (no async delivery, no wake id) is simply
    not limited -- no deadline, no bounded child, the base inline fallback with
    the base note. The live dispatcher passes background=True for a root."""
    _no_async_surface()
    root = _root(platform='api_server')
    assert fl.remaining_seconds(root) is None, 'the limit must be off on this surface'
    clock.advance(10_000)
    seen = {}

    def _run(task_index, goal, child, parent_agent, **kw):
        seen.update(kw)
        return {'task_index': task_index, 'status': 'completed', 'summary': 'ok',
                'api_calls': 1, 'duration_seconds': 0.0, 'model': 'm',
                'exit_reason': 'completed'}

    monkeypatch.setattr(dt, '_run_single_child', _run)

    result = json.loads(root._dispatch_delegate_task({'goal': 'Summarise the whole report'}))

    assert 'results' in result, result
    assert result.get('status') != 'rejected'
    assert 'SYNCHRONOUSLY' in result['note']
    assert 'time limit' not in result['note'].lower()
    assert 'foreground_wait' not in seen


@pytest.mark.parametrize('surface', ['async', 'wake_id'])
@pytest.mark.parametrize('scenario', ['accepted', 'capacity', 'schedule_rejected',
                                      'construction_failure'])
def test_limit_active_means_no_child_runs_inline(clock, harness, monkeypatch, surface, scenario):
    """With the limit active a helper can always be delivered, so the real
    dispatcher never runs a child in the caller's thread: it is either a
    detached helper or an error the model sees."""
    import gateway.session_context as sc
    import tools.async_delegation as async_mod

    platform = 'telegram'
    if surface == 'wake_id':
        monkeypatch.setenv('CLOVER_SESSION_ID', 'raw-sid-11')
        sc.set_session_vars(platform='api_server', chat_id='raw-sid-11',
                            session_key='raw-sid-11', session_id='raw-sid-11',
                            async_delivery=False)
        platform = 'api_server'
    root = _root(platform=platform)
    assert fl.remaining_seconds(root) is not None, 'the limit must be active here'
    caller = threading.current_thread()
    inline_runs: list[str] = []

    def _run(task_index, goal, child, parent_agent, **kw):
        if threading.current_thread() is caller:
            inline_runs.append(goal)
        return {'task_index': task_index, 'status': 'completed', 'summary': 'ok',
                'api_calls': 1, 'duration_seconds': 0.0, 'model': 'm',
                'exit_reason': 'completed'}

    monkeypatch.setattr(dt, '_run_single_child', _run)
    if scenario == 'capacity':
        monkeypatch.setattr(async_mod, 'has_async_capacity', lambda *_a, **_k: False)
    elif scenario == 'schedule_rejected':
        monkeypatch.setattr(
            async_mod, 'dispatch_async_delegation_batch',
            lambda **kw: {'status': 'rejected', 'reason': 'schedule', 'error': 'pool shut down'})
    elif scenario == 'construction_failure':
        def _boom(**kw):
            raise ValueError('pinned delegation.command is not on PATH')
        monkeypatch.setattr(dt, '_build_child_agent', _boom)

    result = json.loads(root._dispatch_delegate_task(
        {'goal': 'Summarise the whole report in detail', 'background': False}))

    assert not inline_runs, 'a child ran inline under an active limit'
    assert 'results' not in result, result
    expected = 'dispatched' if scenario == 'accepted' else 'rejected'
    assert result['status'] == expected, result
    assert result['mode'] == 'background'


def test_checkpoint_keeps_its_strict_rejection_on_a_surface_without_delivery(clock, harness):
    """Unchanged behaviour: with the checkpoint on, an undeliverable background
    request is rejected (the limit only changes the limit-only case)."""
    import yaml
    from clover_constants import get_clover_home

    from clover_cli.config_defaults import DEFAULT_CONFIG

    (get_clover_home() / 'config.yaml').write_text(yaml.safe_dump({
        '_config_version': DEFAULT_CONFIG['_config_version'],
        'delegation': {'foreground_time_limit_seconds': 300,
                       'checkpoint': {'enabled': True}},
    }))
    _no_async_surface()
    root = _root(platform='api_server')

    result = _delegate(root, background=True)

    assert result['status'] == 'rejected'
    assert result['reason'] == 'delivery_unsupported'
    assert not harness.ran_goals


def test_session_with_a_wake_id_still_dispatches_in_the_background(clock, harness, monkeypatch):
    """The API server can wake a session by raw id (gateway/wake.py): the limit
    must not turn that real background path into an inline wait."""
    import gateway.session_context as sc

    monkeypatch.setenv('CLOVER_SESSION_ID', 'raw-sid-7')
    sc.set_session_vars(platform='api_server', chat_id='raw-sid-7',
                        session_key='raw-sid-7', session_id='raw-sid-7',
                        async_delivery=False)
    root = _root(platform='api_server')

    result = _delegate(root, background=True)

    assert result['status'] == 'dispatched', result
    assert result['mode'] == 'background'


def test_limit_off_keeps_the_historical_inline_fallback(clock, harness, monkeypatch):
    from clover_constants import get_clover_home

    from clover_cli.config_defaults import DEFAULT_CONFIG

    (get_clover_home() / 'config.yaml').write_text(yaml.safe_dump({
        '_config_version': DEFAULT_CONFIG['_config_version'],
        'delegation': {'foreground_time_limit_seconds': 0},
    }))
    _no_async_surface()
    root = _root(platform='api_server')

    result = _delegate(root, background=True)

    assert 'results' in result  # ran synchronously, as before
    assert harness.ran_goals


def test_control_actions_are_never_forced_or_refused(clock, harness):
    root = _root()
    clock.advance(10_000)

    result = _delegate(root, action='list', goal=None)

    assert 'error' not in result or 'time limit' not in json.dumps(result).lower()


# ── no-async surfaces: inline, bounded by the episode remainder ────────────

class _BlockingChild:
    """A real-shaped child whose conversation blocks until interrupted."""

    def __init__(self, index: int = 0, hang_seconds: float = 30.0):
        self._subagent_id = f'sa-blk-{index}'
        self._delegate_depth = 1
        self._delegate_role = 'leaf'
        self.session_id = f'child-blk-{index}'
        self.model = 'm'
        self.provider = 'custom'
        self.api_mode = 'chat_completions'
        self.base_url = 'http://127.0.0.1:1/v1'
        self.max_iterations = 5
        self.quiet_mode = True
        self.platform = 'subagent'
        self.valid_tool_names = {'terminal'}
        self.tools = []
        self._api_call_count = 1
        self._stop = threading.Event()
        self._hang_seconds = hang_seconds
        self.started = threading.Event()
        self.interrupts = 0

    def get_activity_summary(self):
        return {'api_call_count': self._api_call_count, 'max_iterations': 5,
                'current_tool': None, 'seconds_since_activity': 0}

    def run_conversation(self, user_message, task_id=None, stream_callback=None,
                         conversation_history=None):
        self.started.set()
        self._stop.wait(self._hang_seconds)
        return {'final_response': '', 'completed': False,
                'api_calls': self._api_call_count, 'interrupted': self._stop.is_set()}

    def interrupt(self, *_a, **_k):
        self.interrupts += 1
        self._stop.set()

    def close(self):
        pass


@pytest.fixture
def fast_grace(monkeypatch):
    monkeypatch.setattr(dt, '_STALE_RESULT_GRACE_SECONDS', 0.05)


def _run_inline(root, child, **kw):
    """The base inline runner (no episode bound exists any more)."""
    return dt._run_single_child(
        task_index=kw.pop('task_index', 0), goal='inline goal', child=child,
        parent_agent=root, **kw)


def test_background_delegate_is_allowed_after_the_limit(clock, harness):
    root = _root()
    clock.advance(301)

    result = _delegate(root, background=True)

    assert result['status'] == 'dispatched'


def test_forced_background_result_tells_the_model_not_to_mention_the_limit(clock, harness):
    root = _root()
    result = _delegate(root, background=False)

    assert result['status'] == 'dispatched'
    assert 'do not mention' in result['note'].lower()
