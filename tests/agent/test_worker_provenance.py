"""Scoped local launches, fail-closed identity, atomic helper creation."""
import json
from types import SimpleNamespace

import pytest

from agent.worker_provenance import configure_oneshot_worker, local_worker_launch
from clover_constants import get_clover_home
from clover_state import SessionDB
from gateway.session_context import _SESSION_ID
from tools.environments.local import _sanitize_subprocess_env, _make_run_env


def test_launch_scope_never_inherits_or_mutates_global_identity(monkeypatch):
    monkeypatch.setenv("_CLOVER_WORKER_LAUNCH", "stale")
    monkeypatch.setenv("CLOVER_SESSION_ID", "foreign")
    token = _SESSION_ID.set("actual-stored-parent")
    try:
        for build in (lambda: _sanitize_subprocess_env({"_CLOVER_WORKER_LAUNCH": "stale"}),
                      lambda: _make_run_env({})):
            assert "_CLOVER_WORKER_LAUNCH" not in build()
            with local_worker_launch(True):
                assert json.loads(build()["_CLOVER_WORKER_LAUNCH"])["parent"] == "actual-stored-parent"
            assert "_CLOVER_WORKER_LAUNCH" not in build()
    finally:
        _SESSION_ID.reset(token)


def test_empty_context_cannot_borrow_foreign_session(monkeypatch):
    monkeypatch.setenv("CLOVER_SESSION_ID", "foreign")
    token = _SESSION_ID.set("")
    try:
        with local_worker_launch(True):
            assert "_CLOVER_WORKER_LAUNCH" not in _sanitize_subprocess_env({})
    finally:
        _SESSION_ID.reset(token)


def test_oneshot_provenance_checks_profile_and_missing_parent(monkeypatch):
    home = get_clover_home()
    db = SessionDB(db_path=home / "state.db")
    agent = SimpleNamespace(session_id="child", _session_init_model_config={})
    try:
        for payload in ({"parent": "missing", "home": str(home.resolve())},
                        {"parent": "parent", "home": str(home / "other-profile")},
                        {"parent": "child", "home": str(home.resolve())}):
            monkeypatch.setenv("_CLOVER_WORKER_LAUNCH", json.dumps(payload))
            with pytest.raises(ValueError):
                configure_oneshot_worker(agent, db)
        assert agent._session_init_model_config == {}
    finally:
        db.close()


@pytest.mark.parametrize("parent", ["missing", "child", "descendant"])
def test_atomic_creation_rejects_missing_self_and_cyclic_parent(tmp_path, parent):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        if parent == "descendant":
            db.create_session("child", "cli")
            db.create_session("descendant", "cli", parent_session_id="child")
        with pytest.raises(ValueError):
            db.create_session("child", "cli", parent_session_id=parent,
                              model_config={"_delegate_from": parent}, validate_worker_parent=True)
        row = db.get_session("child")
        if parent == "descendant":
            assert row["parent_session_id"] is None
            assert row["model_config"] is None
        else:
            assert row is None
    finally:
        db.close()
