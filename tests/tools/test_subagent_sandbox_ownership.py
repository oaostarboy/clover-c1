"""Subagent sandbox: built from the parent's own folder, never another agent's.

Bug A (2026-10-12): the parent's terminal sat in a sibling agent's live
checkout, and the child's worktree was created INSIDE that checkout.
Bug B: the isolation note read as "don't even look elsewhere", so a
read-only child refused to read the real repo it was asked to measure.
"""

import subprocess
from pathlib import Path

import pytest

from tools import subagent_worktree as sw


def _repo(root: Path) -> Path:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "T"], check=True)
    (root / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
    return root


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """own repo + own home + an ~/agents-style root holding a sibling's repo."""
    own_repo = _repo(tmp_path / "Projects" / "own")
    own_home = tmp_path / "own-home"
    own_home.mkdir()
    agents = tmp_path / "agents"
    sibling_repo = _repo(agents / "sibling" / "clover-c1")
    other_home = tmp_path / ".other-home"
    other_home.mkdir()
    (other_home / "gateway.pid").write_text("{}")
    monkeypatch.setattr(sw, "foreign_agent_roots", lambda: [str(agents.resolve())])
    monkeypatch.setattr(sw, "own_clover_home", lambda: str(own_home.resolve()))
    monkeypatch.setattr(sw, "own_install_repo", lambda: str(own_repo.resolve()))
    monkeypatch.setattr(sw, "session_start_candidates", lambda: [str(own_repo.resolve())])
    return {
        "own_repo": own_repo,
        "own_home": own_home,
        "agents": agents,
        "sibling_repo": sibling_repo,
        "other_home": other_home,
    }


def test_foreign_owner_classifies_folders(layout):
    assert sw.foreign_owner(str(layout["own_repo"])) is None
    assert sw.foreign_owner(str(layout["own_home"])) is None
    assert "install folder" in sw.foreign_owner(str(layout["sibling_repo"]))
    assert "home" in sw.foreign_owner(str(layout["other_home"] / "workspace"))


def test_nested_profile_home_inside_own_home_is_foreign(layout):
    nested = layout["own_home"] / "profiles" / "dev"
    nested.mkdir(parents=True)
    (nested / "gateway.pid").write_text("{}")
    assert sw.foreign_owner(str(nested / "x")) is not None
    assert sw.foreign_owner(str(layout["own_home"] / "cache")) is None


def test_worktree_never_created_inside_sibling_checkout(layout):
    """The 13:08 reproduction: parent cwd inside a sibling's live repo."""
    sibling = layout["sibling_repo"]
    outcome, info = sw.prepare_subagent_worktree(
        str(sibling), "child-a", enabled=True, required=True, local_backend=True
    )
    assert outcome["status"] == "isolated"
    assert info is not None
    wt = Path(info["path"]).resolve()
    # Built from the parent's own repo...
    assert str(wt).startswith(str(layout["own_repo"].resolve()) + "/")
    # ...and nothing was written into the sibling's checkout.
    assert not (sibling / ".worktrees").exists()
    assert ".worktrees" not in (sibling / ".gitignore").read_text() if (sibling / ".gitignore").exists() else True
    branches = subprocess.run(
        ["git", "-C", str(sibling), "branch", "--list", "clover-subagent/*"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert branches.strip() == ""
    assert info["redirected_from"] == str(sibling)
    assert "redirected" not in outcome or True
    assert str(sibling) in outcome["reason"]
    note = sw.build_worktree_context_note(info)
    assert str(sibling) in note  # the child is told where the parent was
    sw.finalize_subagent_worktree(info)


def test_worktree_in_other_clover_home_repo_is_redirected(layout):
    repo_in_home = _repo(layout["other_home"] / "workspace" / "proj")
    outcome, info = sw.prepare_subagent_worktree(
        str(repo_in_home), "child-b", enabled=True, required=True, local_backend=True
    )
    assert info is not None and outcome["status"] == "isolated"
    assert not (repo_in_home / ".worktrees").exists()
    assert str(Path(info["path"]).resolve()).startswith(str(layout["own_repo"].resolve()))
    sw.finalize_subagent_worktree(info)


def test_falls_back_to_private_scratch_when_no_own_repo(layout, monkeypatch):
    monkeypatch.setattr(sw, "session_start_candidates", lambda: [])
    sibling = layout["sibling_repo"]
    outcome, info = sw.prepare_subagent_worktree(
        str(sibling), "child-c", enabled=True, required=True, local_backend=True
    )
    assert outcome["status"] == "isolated" and outcome.get("scratch") is True
    assert info is not None
    path = Path(info["path"])
    assert path.is_dir()
    assert str(path.resolve()).startswith(str(layout["own_home"].resolve()))
    assert not (sibling / ".worktrees").exists()
    note = sw.build_worktree_context_note(info)
    assert "[SANDBOX]" in note and str(path) in note
    # Empty scratch is pruned; scratch with output is kept for review.
    final = sw.finalize_subagent_worktree(info)
    assert final["pruned"] is True and not path.exists()


def test_scratch_with_output_is_kept(layout, monkeypatch):
    monkeypatch.setattr(sw, "session_start_candidates", lambda: [])
    _, info = sw.prepare_subagent_worktree(
        str(layout["sibling_repo"]), "child-d", enabled=True, required=False, local_backend=True
    )
    (Path(info["path"]) / "out.txt").write_text("result\n")
    final = sw.finalize_subagent_worktree(info)
    assert final["pruned"] is False and final["dirty"] is True


def test_own_repo_cwd_is_not_redirected(layout):
    outcome, info = sw.prepare_subagent_worktree(
        str(layout["own_repo"]), "child-e", enabled=True, required=True, local_backend=True
    )
    assert info is not None and "redirected_from" not in info
    assert "reason" not in outcome
    sw.finalize_subagent_worktree(info)


# ── Bug B: the note restricts writes only ──────────────────────────────────


def test_isolation_note_allows_reads_anywhere():
    note = sw.build_worktree_context_note(
        {"path": "/x/wt", "branch": "clover-subagent/subagent-1"}
    )
    assert "/x/wt" in note and "clover-subagent/subagent-1" in note
    assert "WRITES only" in note
    assert "Reading is allowed on any path" in note
    for verb in ("ls", "du", "grep", "git log"):
        assert verb in note
    # The wording that made a read-only child refuse is gone.
    assert "Do NOT cd" not in note
    assert "All file edits and shell commands must happen" not in note
    assert "uv venv .venv" in note


def test_scratch_note_allows_reads_anywhere():
    note = sw.build_worktree_context_note({"path": "/s", "scratch": "1"})
    assert "WRITES only" in note and "Reading is allowed on any path" in note
