"""A session whose recorded cwd was deleted must recover, not wedge.

Regression: the agent removed a temp checkout while its shell sat inside it.
Every later terminal command was prefixed with ``cd <gone>`` and failed with
exit 126 (even ones that cd'd somewhere valid themselves), and delegate_task
with ``worktree_isolation_required`` refused every child with "workspace is
not a git repository" because it inherited the dead folder.
"""

import os
from types import SimpleNamespace

import pytest

from tools import terminal_tool as tt


@pytest.fixture
def clean_records():
    saved = dict(tt._session_cwd)
    tt._session_cwd.clear()
    yield
    tt._session_cwd.clear()
    tt._session_cwd.update(saved)


def test_vanished_record_falls_back_to_default(tmp_path, clean_records):
    gone = tmp_path / "deleted"
    gone.mkdir()
    tt.record_session_cwd("chat-1", str(gone))
    gone.rmdir()

    cwd = tt._resolve_command_cwd(
        workdir=None, default_cwd=str(tmp_path), session_key="chat-1", env_type="local"
    )

    assert cwd == str(tmp_path)
    # The dead record is dropped so the session doesn't re-hit it.
    assert tt.get_session_cwd("chat-1") is None


def test_existing_record_still_wins(tmp_path, clean_records):
    live = tmp_path / "live"
    live.mkdir()
    tt.record_session_cwd("chat-1", str(live))

    cwd = tt._resolve_command_cwd(
        workdir=None, default_cwd=str(tmp_path), session_key="chat-1", env_type="local"
    )

    assert cwd == str(live)
    assert tt.get_session_cwd("chat-1") == str(live)


def test_explicit_workdir_untouched(tmp_path, clean_records):
    tt.record_session_cwd("chat-1", str(tmp_path / "nope"))
    cwd = tt._resolve_command_cwd(
        workdir="/explicit", default_cwd=str(tmp_path), session_key="chat-1", env_type="local"
    )
    assert cwd == "/explicit"


def test_container_backend_record_not_checked_against_host(tmp_path, clean_records, monkeypatch):
    # A sandbox path doesn't exist on the host; that must not count as vanished.
    monkeypatch.setattr(tt, "_is_container_backend", lambda env_type: env_type == "docker")
    monkeypatch.setattr(tt, "_is_unusable_container_cwd", lambda p: False)
    tt.record_session_cwd("chat-1", "/workspace/project")
    cwd = tt._resolve_command_cwd(
        workdir=None, default_cwd="/workspace", session_key="chat-1", env_type="docker"
    )
    assert cwd == "/workspace/project"


def test_task_default_skips_vanished_record(tmp_path, clean_records):
    gone = tmp_path / "deleted"
    gone.mkdir()
    tt.record_session_cwd("task-1", str(gone))
    gone.rmdir()

    cwd = tt._resolve_task_default_cwd(
        "task-1", default_cwd=str(tmp_path), env_type="local"
    )
    assert cwd == str(tmp_path)


def test_first_command_cwd_never_returns_vanished_dir(tmp_path, clean_records):
    gone = tmp_path / "deleted"
    gone.mkdir()
    tt.record_session_cwd("chat-1", str(gone))
    gone.rmdir()

    cwd = tt.resolve_first_command_cwd(
        "chat-1", "chat-1", default_cwd=str(tmp_path), env_type="local"
    )
    assert cwd == str(tmp_path)
    assert os.path.isdir(cwd)


def test_delegate_parent_workspace_skips_vanished_cwd(tmp_path, clean_records, monkeypatch):
    from tools import delegate_tool as dt
    import tools.approval as approval

    gone = tmp_path / "deleted"
    gone.mkdir()
    tt.record_session_cwd("tg-chat", str(gone))
    gone.rmdir()

    monkeypatch.setattr(approval, "get_current_session_key", lambda default="": "tg-chat")
    monkeypatch.setattr(dt, "_resolve_fresh_gateway_chat_cwd", lambda task, key: str(tmp_path))
    parent = SimpleNamespace(_current_task_id="tg-chat", _delegate_depth=0, platform="telegram")

    _task, cwd = dt._capture_parent_workspace(parent)

    assert cwd == str(tmp_path)


def test_terminal_recovers_after_cwd_deleted_end_to_end(tmp_path, monkeypatch, clean_records):
    """Real local shell: sit in a dir, delete it, next command still runs."""
    import json

    home = tmp_path / "home"
    home.mkdir()
    doomed = tmp_path / "doomed"
    doomed.mkdir()

    monkeypatch.setattr(tt, "_active_environments", {})
    monkeypatch.setattr(tt, "_last_activity", {})
    monkeypatch.setattr(
        tt, "_get_env_config",
        lambda: {"env_type": "local", "cwd": str(home), "timeout": 60,
                 "lifetime_seconds": 3600},
    )
    monkeypatch.setattr(tt, "_check_all_guards", lambda command, env_type, **kw: {"approved": True})
    monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)

    try:
        r = json.loads(tt.terminal_tool(command=f"cd {doomed}", task_id="e2e-sess"))
        assert r["exit_code"] == 0, r
        assert tt.get_session_cwd("e2e-sess") == str(doomed)

        doomed.rmdir()

        r = json.loads(tt.terminal_tool(command="pwd", task_id="e2e-sess"))
        assert r["exit_code"] == 0, r
        assert str(home) in r["output"], r
    finally:
        for env in list(tt._active_environments.values()):
            try:
                env.cleanup()
            except Exception:
                pass
