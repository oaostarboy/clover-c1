"""Adopt a copied/zipped install (no ``.git``) into git instead of refusing.

Field bug: an install made from a copy/zip has no ``.git``, so Telegram
``/update`` and ``clover update`` said "Not a git repository" forever. The
updater now matches the tree to the commit of origin/main it actually equals
and resets there, so only the user's real edits look modified.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from clover_cli import update_cmd

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="needs git",
)


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    """A 3-commit upstream repo whose branch is 'main'."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("venv/\n", encoding="utf-8")
    shas = []
    for i in (1, 2, 3):
        (repo / "app.py").write_text(f"print({i})\n", encoding="utf-8")
        (repo / f"file{i}.txt").write_text(f"v{i}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"c{i}")
        shas.append(_git(repo, "rev-parse", "HEAD"))
    return repo, shas


def _export(repo, sha, dest):
    dest.mkdir()
    tar = subprocess.run(["git", "archive", sha], cwd=repo, capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=tar, check=True)


@pytest.fixture
def install_dir(upstream, tmp_path):
    repo, shas = upstream
    dest = tmp_path / "install"
    _export(repo, shas[1], dest)  # commit 2
    (dest / "app.py").write_text("print('mine')\n", encoding="utf-8")  # user edit
    (dest / "venv").mkdir()
    (dest / "venv" / "keep.bin").write_text("do not touch", encoding="utf-8")
    return dest


def _tree_files(root: Path):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def test_adopt_picks_matching_commit_and_only_user_edit_is_modified(
    upstream, install_dir, monkeypatch
):
    repo, shas = upstream
    monkeypatch.setattr(update_cmd, "OFFICIAL_REPO_URL", str(repo))
    before = _tree_files(install_dir)

    result = update_cmd.adopt_non_git_install(install_dir)

    assert result.commit == shas[1]
    assert result.differing_files == 1
    status = _git(install_dir, "status", "-s")
    assert [l.strip() for l in status.splitlines()] == ["M app.py"]  # venv/ is ignored, nothing else dirty
    # Never deletes or rewrites anything in the working tree.
    assert _tree_files(install_dir) == before
    assert (install_dir / "venv" / "keep.bin").read_text(encoding="utf-8") == "do not touch"
    # Tracking is set so the normal update flow can pull.
    assert _git(install_dir, "rev-parse", "--abbrev-ref", "main@{upstream}") == "origin/main"


def test_adopt_failure_leaves_files_and_removes_only_its_git_dir(install_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(update_cmd, "OFFICIAL_REPO_URL", str(tmp_path / "does-not-exist"))
    before = _tree_files(install_dir)

    with pytest.raises(update_cmd.AdoptError) as exc:
        update_cmd.adopt_non_git_install(install_dir)

    assert "GitHub" in str(exc.value)
    assert not (install_dir / ".git").exists()
    assert _tree_files(install_dir) == before


def test_adopt_without_git_installed_is_actionable(install_dir):
    with patch("clover_cli.update_cmd.shutil.which", return_value=None):
        with pytest.raises(update_cmd.AdoptError) as exc:
            update_cmd.adopt_non_git_install(install_dir)
    assert "Git" in str(exc.value)
    assert not (install_dir / ".git").exists()


def test_update_check_on_non_git_dir_adopts_instead_of_refusing(
    upstream, install_dir, monkeypatch, capsys
):
    repo, shas = upstream
    monkeypatch.setattr(update_cmd, "OFFICIAL_REPO_URL", str(repo))
    monkeypatch.setattr("clover_cli.main.PROJECT_ROOT", install_dir)
    monkeypatch.setattr("clover_cli.main._run_pre_update_backup", lambda args: None)

    try:
        update_cmd._cmd_update_check()
    except SystemExit:
        pass  # later stages may exit; we only care about the guard

    out = capsys.readouterr().out
    assert "Not a git repository" not in out
    assert (install_dir / ".git").exists()


def test_cmd_update_on_non_git_dir_reaches_adoption(install_dir, monkeypatch, capsys):
    """The apply path calls the adopter (and shows its plain-words error)."""
    monkeypatch.setattr("clover_cli.main.PROJECT_ROOT", install_dir)
    monkeypatch.setattr("clover_cli.main._run_pre_update_backup", lambda args: None)
    seen = []

    def boom(root, *a, **k):
        seen.append(root)
        raise update_cmd.AdoptError("plain words about git")

    monkeypatch.setattr(update_cmd, "adopt_non_git_install", boom)
    monkeypatch.setattr(update_cmd.sys, "platform", "linux")

    with pytest.raises(SystemExit) as exc:
        update_cmd._cmd_update_impl(
            SimpleNamespace(check=False, no_backup=True, backup=False, yes=True,
                            branch=None, gateway=False),
            gateway_mode=False,
        )

    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert seen == [install_dir]
    assert "Not a git repository" not in out
    assert "plain words about git" in out
