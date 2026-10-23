"""Helpers must start when there is no git repo to isolate.

After a gateway restart the parent chat can have no usable cwd record and the
process sits in ``~/.clover`` (not a repo). With ``worktree_isolation_required``
on, every helper was refused with "workspace is not a git repository". Isolation
protects a checkout; with no checkout there is nothing to protect. Real repos
keep their worktree, and a real failure INSIDE a repo still refuses.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import delegate_tool, subagent_worktree as sw


def _repo(root: Path) -> Path:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "T"], check=True)
    (root / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
    return root


def _run_child(parent_cwd, *, required=True, previews=None):
    """Run a real ``_run_single_child`` with a stub child; return (result, goals)."""
    goals = []

    def progress(event, **kwargs):
        if event == "subagent.progress" and previews is not None:
            previews.append(kwargs.get("preview", ""))

    child = SimpleNamespace(
        _subagent_id="helper-start-child", _delegate_saved_tool_names=[],
        _credential_pool=None, session_id="child-session",
        _parent_session_id="parent-session", _delegate_depth=1,
        _parent_subagent_id=None, tool_progress_callback=progress,
        run_conversation=lambda **kw: goals.append(kw["user_message"]) or {
            "final_response": "done", "messages": [], "api_calls": 1,
            "completed": True, "input_tokens": 0, "output_tokens": 0,
        },
    )
    parent = SimpleNamespace(session_id="parent-session", _current_task_id=None)
    with patch.object(delegate_tool, "_get_worktree_isolation", return_value=True), \
         patch.object(delegate_tool, "_get_worktree_isolation_required", return_value=required), \
         patch.object(delegate_tool, "_capture_parent_workspace",
                      return_value=("parent-session", parent_cwd)):
        result = delegate_tool._run_single_child(0, "do work", child, parent)
    return result, goals


@pytest.fixture
def no_start_cwd(monkeypatch):
    """No gateway start folder and no TERMINAL_CWD hint."""
    from tools import terminal_tool

    monkeypatch.setattr(terminal_tool, "_gateway_start_cwd", None)
    monkeypatch.delenv("TERMINAL_CWD", raising=False)


def test_unknown_parent_cwd_in_non_git_process_folder_starts_shared(tmp_path, monkeypatch, no_start_cwd):
    """The post-restart case: parent has no cwd record, process folder is not a repo."""
    plain = tmp_path / "home-like"
    plain.mkdir()
    monkeypatch.chdir(plain)
    previews = []
    result, goals = _run_child(None, required=True, previews=previews)
    assert goals, result
    assert result["status"] in ("completed", "success"), result
    assert result["workspace_isolation"]["status"] == "shared"
    assert "nothing to isolate" in goals[0]
    assert any("nothing to isolate" in p for p in previews), previews


def test_known_non_git_parent_cwd_starts_shared_and_says_so(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    previews = []
    result, goals = _run_child(str(plain), required=True, previews=previews)
    assert goals, result
    assert result["workspace_isolation"]["status"] == "shared"
    assert any("not a git repository" in p and "nothing to isolate" in p for p in previews), previews
    assert "nothing to isolate" in goals[0]


def test_unknown_parent_cwd_inside_a_repo_is_still_refused(tmp_path, monkeypatch, no_start_cwd):
    """Unknown cwd + process sits in a repo: we cannot prove nothing needs isolating."""
    repo = _repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    result, goals = _run_child(None, required=True)
    assert not goals
    assert result["status"] == "error"
    assert result["workspace_isolation"]["status"] == "blocked"


def test_git_repo_with_required_isolation_still_creates_worktree(tmp_path):
    repo = _repo(tmp_path / "repo")
    result, goals = _run_child(str(repo), required=True)
    assert goals, result
    assert result["workspace_isolation"]["status"] == "isolated"
    assert "isolated git worktree" in goals[0]
    assert str(repo / ".worktrees") in goals[0] or ".worktrees" in goals[0] or "worktree" in goals[0]


def test_failed_worktree_inside_real_repo_still_refuses_with_clear_message(tmp_path):
    repo = _repo(tmp_path / "repo")
    with patch.object(sw, "create_subagent_worktree", return_value=None):
        result, goals = _run_child(str(repo), required=True)
    assert not goals, "a helper whose required isolation failed must not start"
    assert result["status"] == "error"
    assert result["workspace_isolation"]["status"] == "blocked"
    err = result["error"]
    assert "child was not started" in err
    assert "git worktree creation failed" in err
    assert "delegation.worktree_isolation_required" in err
    assert "clover config set delegation.worktree_isolation_required false" in err


def test_failed_worktree_inside_real_repo_optional_runs_shared(tmp_path):
    repo = _repo(tmp_path / "repo")
    with patch.object(sw, "create_subagent_worktree", return_value=None):
        result, goals = _run_child(str(repo), required=False)
    assert goals, result
    assert result["workspace_isolation"]["status"] == "shared"


def test_isolation_keys_are_recognized_config_keys():
    from clover_cli.config import DEFAULT_CONFIG, _validate_config_key

    for key in ("delegation.worktree_isolation", "delegation.worktree_isolation_required"):
        assert _validate_config_key(key) == (True, None), key
        assert isinstance(DEFAULT_CONFIG["delegation"][key.split(".")[1]], bool)


def test_config_defaults_match_runtime_defaults():
    """Declared defaults must equal what delegate_tool assumes when the key is unset."""
    from clover_cli.config import DEFAULT_CONFIG

    with patch.object(delegate_tool, "_load_config", return_value={}):
        assert DEFAULT_CONFIG["delegation"]["worktree_isolation"] is delegate_tool._get_worktree_isolation()
        assert (
            DEFAULT_CONFIG["delegation"]["worktree_isolation_required"]
            is delegate_tool._get_worktree_isolation_required()
        )
