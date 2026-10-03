"""A background process started by a delegated child is stamped with the
conversation that owns the chat route, not the child's own session id.

Otherwise its completion resolves to the child session and (before typed
route ownership) moved the parent's route onto it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def terminal(monkeypatch, tmp_path):
    import tools.terminal_tool as tt
    from tools import process_registry as pr

    spawned = SimpleNamespace(
        id="proc_stamp_test",
        pid=4242,
        notify_on_complete=False,
        watcher_platform="",
        watcher_chat_id="",
        watcher_user_id="",
        watcher_user_name="",
        watcher_thread_id="",
        watcher_message_id="",
        watcher_interval=0,
        parent_session_id="",
    )
    config = {
        "env_type": "local", "docker_image": "", "singularity_image": "",
        "modal_image": "", "daytona_image": "", "cwd": str(tmp_path), "timeout": 30,
    }
    monkeypatch.setattr(tt, "_get_env_config", lambda: config)
    monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(tt, "_check_all_guards", lambda *_a, **_k: {"approved": True})
    monkeypatch.setattr(pr.process_registry, "spawn_local", lambda **_kw: spawned)
    monkeypatch.setitem(tt._active_environments, "default", SimpleNamespace(env={}))
    monkeypatch.setitem(tt._last_activity, "default", 0.0)

    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(
        platform="telegram", chat_id="100", chat_type="dm",
        session_key="agent:main:telegram:dm:100", user_id="u1",
    )
    try:
        yield tt, spawned
    finally:
        clear_session_vars(tokens)
        tt._active_environments.pop("default", None)
        tt._last_activity.pop("default", None)


def _run_background(tt):
    out = json.loads(
        tt.terminal_tool(command="pytest tests/", background=True, notify_on_complete=True)
    )
    assert out.get("notify_on_complete") is True, out
    return out


def test_parent_turn_stamps_its_own_session(terminal):
    from gateway.session_context import scoped_current_session_id

    tt, spawned = terminal
    with scoped_current_session_id("root_sess"):
        _run_background(tt)

    assert spawned.parent_session_id == "root_sess"


def test_delegated_child_stamps_the_route_owner_not_the_child(terminal):
    from agent.delegation_context import delegated_child_context
    from gateway.session_context import scoped_current_session_id

    tt, spawned = terminal
    with scoped_current_session_id("root_sess"):
        with delegated_child_context("child_sess"):
            _run_background(tt)

    assert spawned.parent_session_id == "root_sess"


def test_nested_delegated_child_still_stamps_the_root(terminal):
    from agent.delegation_context import delegated_child_context
    from gateway.session_context import scoped_current_session_id

    tt, spawned = terminal
    with scoped_current_session_id("root_sess"):
        with delegated_child_context("orchestrator_sess"):
            with delegated_child_context("worker_sess"):
                _run_background(tt)

    assert spawned.parent_session_id == "root_sess"
