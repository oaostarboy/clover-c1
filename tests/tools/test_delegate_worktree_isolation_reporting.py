import subprocess
from pathlib import Path

from tools import subagent_worktree as sw


def _repo(root: Path) -> Path:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
    return root


def test_worktree_setup_reports_real_isolated_child_and_retains_dirty_work(tmp_path):
    repo = _repo(tmp_path / "repo")
    outcome, info = sw.prepare_subagent_worktree(
        str(repo), "child-one", enabled=True, required=True, local_backend=True
    )
    assert outcome["status"] == "isolated"
    assert outcome["required"] is True
    assert info is not None
    assert Path(info["path"]).is_dir()
    assert info["branch"] == "clover-subagent/subagent-child-one"
    assert info["path"] != str(repo)
    context = sw.build_worktree_context_note(info)
    assert info["path"] in context and info["branch"] in context
    assert "isolated git worktree" in context
    assert subprocess.run(["git", "-C", info["path"], "rev-parse", "--abbrev-ref", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip() == info["branch"]
    artifact = Path(info["path"]) / "child-output.txt"
    artifact.write_text("uncommitted child output\n")
    final = sw.finalize_subagent_worktree(info)
    assert final["dirty"] is True and final["pruned"] is False
    assert artifact.read_text() == "uncommitted child output\n"


def test_optional_setup_fallback_is_truthfully_shared(tmp_path):
    non_repo = tmp_path / "plain"
    non_repo.mkdir()
    outcome, info = sw.prepare_subagent_worktree(
        str(non_repo), "child-two", enabled=True, required=False, local_backend=True
    )
    assert info is None
    assert outcome["status"] == "shared"
    assert outcome["required"] is False
    assert "git" in outcome["reason"].lower()


def test_unsupported_backend_is_truthfully_shared_or_blocked(tmp_path):
    repo = _repo(tmp_path / "repo")
    outcome, info = sw.prepare_subagent_worktree(
        str(repo), "child-three", enabled=True, required=False, local_backend=False
    )
    assert info is None and outcome["status"] == "shared"
    assert "backend" in outcome["reason"].lower()
    blocked, missing = sw.prepare_subagent_worktree(
        str(repo), "child-four", enabled=True, required=True, local_backend=False
    )
    assert missing is None and blocked["status"] == "blocked"
    assert blocked["required"] is True


def test_disabled_isolation_reports_shared_not_isolated(tmp_path):
    outcome, info = sw.prepare_subagent_worktree(
        str(tmp_path), "child-five", enabled=False, required=False, local_backend=True
    )
    assert info is None and outcome["status"] == "shared"
    assert outcome["reason"] == "worktree isolation is disabled"
    note = sw.build_shared_workspace_context_note(outcome)
    assert "shared workspace, NOT an isolated worktree" in note
    assert "worktree isolation is disabled" in note


def test_required_setting_is_opt_in_and_uses_delegation_config():
    from unittest.mock import patch
    from tools import delegate_tool

    with patch.object(delegate_tool, "_load_config", return_value={"worktree_isolation": True}):
        assert delegate_tool._get_worktree_isolation() is True
        assert delegate_tool._get_worktree_isolation_required() is False
    with patch.object(delegate_tool, "_load_config", return_value={
        "worktree_isolation": True, "worktree_isolation_required": True
    }):
        assert delegate_tool._get_worktree_isolation_required() is True

    with patch.object(delegate_tool, "_load_config", return_value={
        "worktree_isolation": False, "worktree_isolation_required": True
    }):
        assert delegate_tool._get_worktree_isolation() is False
        assert delegate_tool._get_worktree_isolation_required() is True


def test_required_isolation_blocks_real_child_runner_before_conversation(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import patch

    from tools import delegate_tool

    child = SimpleNamespace(
        _subagent_id="required-isolation-child",
        _delegate_saved_tool_names=[],
        _credential_pool=None,
        session_id="child-session",
        _parent_session_id="parent-session",
        _delegate_depth=1,
        _parent_subagent_id=None,
        tool_progress_callback=None,
        run_conversation=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("blocked child conversation must not run")
        ),
    )
    parent = SimpleNamespace(session_id="parent-session", _current_task_id=None)
    with patch.object(delegate_tool, "_get_worktree_isolation", return_value=True), \
         patch.object(delegate_tool, "_get_worktree_isolation_required", return_value=True), \
         patch.object(delegate_tool, "_resolve_workspace_hint", return_value=str(tmp_path)):
        result = delegate_tool._run_single_child(0, "do work", child, parent)

    assert result["status"] == "error"
    assert "child was not started" in result["error"].lower()
    assert result["workspace_isolation"]["status"] == "blocked"
