"""Tests for clover_cli.gitlock — stale git lock recovery + ancestry probe.

These cover the two failure modes that produced the false "update available"
notification and the hard ``update --check`` failure after a crashed fetch on
a shallow clone:

1. A stale ``.git/shallow.lock`` makes every later ``git fetch`` fail with
   "File exists" unless cleared.
2. On a shallow clone the update check compares tip SHAs, so local
   cherry-picks on top of the remote tip look like "update available" even
   though HEAD already contains the remote tip.

The module's safety rules are also pinned: a *young* lock or a *running git
process* must never be cleared.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from clover_cli.gitlock import (
    LOCK_NAMES,
    STALE_LOCK_MIN_AGE_SECONDS,
    clear_stale_git_locks,
    is_ancestor_of_head,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real, tiny git repo with two commits (no network)."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "a.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "first"], cwd=root, check=True)
    (root / "b.txt").write_text("two\n")
    subprocess.run(["git", "add", "b.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=root, check=True)
    return root


def _touch(path: Path, age_seconds: float) -> None:
    """Create (or truncate) a file and backdate its mtime."""
    path.touch()
    old = time.time() - age_seconds
    os.utime(path, (old, old))


@pytest.fixture
def no_git_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the process guard: pretend no git process is running.

    The removal tests exercise the sweep itself, not the guard.  Without
    this pin they are flaky on CI: the parallel per-file test runner is
    almost always running a real ``git`` subprocess somewhere, the
    ``pgrep -x git`` probe hits, and the sweep (correctly) refuses to
    remove anything — failing the assertion for reasons unrelated to the
    code under test.  The guard's own behavior is pinned separately in
    :func:`test_clear_skips_sweep_while_git_running`.
    """
    import clover_cli.gitlock as gitlock

    monkeypatch.setattr(gitlock, "_git_proc_running", lambda: False)


def test_clear_removes_stale_shallow_lock(repo: Path, no_git_running: None) -> None:
    _touch(repo / ".git" / "shallow.lock", STALE_LOCK_MIN_AGE_SECONDS + 60)
    removed = clear_stale_git_locks(repo)
    assert str(repo / ".git" / "shallow.lock") in removed
    assert not (repo / ".git" / "shallow.lock").exists()


def test_clear_removes_all_stale_lock_kinds(repo: Path, no_git_running: None) -> None:
    for name in LOCK_NAMES:
        _touch(repo / ".git" / name, STALE_LOCK_MIN_AGE_SECONDS + 60)
    removed = clear_stale_git_locks(repo)
    assert len(removed) == len(LOCK_NAMES)
    for name in LOCK_NAMES:
        assert not (repo / ".git" / name).exists()


def test_clear_skips_sweep_while_git_running(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A running git process must block the sweep even for stale locks."""
    import clover_cli.gitlock as gitlock

    monkeypatch.setattr(gitlock, "_git_proc_running", lambda: True)
    _touch(repo / ".git" / "shallow.lock", STALE_LOCK_MIN_AGE_SECONDS + 60)
    removed = clear_stale_git_locks(repo)
    assert removed == []
    assert (repo / ".git" / "shallow.lock").exists()


def test_clear_keeps_young_lock(repo: Path, no_git_running: None) -> None:
    _touch(repo / ".git" / "shallow.lock", 1)  # 1 second old — presumably live
    removed = clear_stale_git_locks(repo)
    assert removed == []
    assert (repo / ".git" / "shallow.lock").exists()


def test_clear_noop_on_non_repo(tmp_path: Path) -> None:
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    assert clear_stale_git_locks(bare) == []


def test_clear_noop_with_no_locks(repo: Path) -> None:
    assert clear_stale_git_locks(repo) == []


def test_is_ancestor_true_for_first_commit(repo: Path) -> None:
    first = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert is_ancestor_of_head(repo, first) is True


def test_is_ancestor_true_for_head_itself(repo: Path) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert is_ancestor_of_head(repo, head) is True


def test_is_ancestor_false_for_unknown_rev(repo: Path) -> None:
    assert is_ancestor_of_head(repo, "deadbeef" * 5) is False


def test_is_ancestor_false_for_nonexistent_repo(tmp_path: Path) -> None:
    assert is_ancestor_of_head(tmp_path / "missing", "HEAD") is False


# ---- a killed git's index.lock goes at once; a live one's stays ----


def _status_blocked_on_a_fifo(repo: Path) -> subprocess.Popen:
    """A real ``git status`` that takes ``.git/index.lock`` and then blocks: its
    untracked scan opens a FIFO ``.gitignore`` nobody writes."""
    (repo / "junk").mkdir()
    os.mkfifo(repo / "junk" / ".gitignore")
    (repo / "junk" / "x").touch()
    (repo / "a.txt").touch()  # stat-dirty: status refreshes (and so locks) the index
    proc = subprocess.Popen(
        ["git", "status", "--porcelain"], cwd=repo,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while not (repo / ".git" / "index.lock").exists():
        assert proc.poll() is None and time.monotonic() < deadline, "git status never took index.lock"
        time.sleep(0.05)
    return proc


def _age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="ownership proof via /proc (Linux)")
def test_killed_gits_index_lock_goes_at_once_and_a_live_ones_stays(repo: Path) -> None:
    from clover_cli.gitlock import release_dead_index_lock

    lock = repo / ".git" / "index.lock"
    proc = _status_blocked_on_a_fifo(repo)
    try:
        assert release_dead_index_lock(repo) is False
        assert lock.exists()
    finally:
        proc.kill()
        proc.wait()

    assert lock.exists(), "premise: a SIGKILLed status strands index.lock"
    # Fresh mtime: the 10-minute age sweep keeps it, and so does the dead-owner release (C13-ASTRA-07).
    assert clear_stale_git_locks(repo) == []
    assert release_dead_index_lock(repo) is False and lock.exists()
    _age(lock, 2 * STALE_LOCK_MIN_AGE_SECONDS)
    assert release_dead_index_lock(repo) is True
    assert not lock.exists()


def test_release_dead_index_lock_without_a_lock_is_a_noop(repo: Path) -> None:
    from clover_cli.gitlock import release_dead_index_lock

    assert release_dead_index_lock(repo) is False
    assert release_dead_index_lock(repo / "missing") is False


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="ownership proof via /proc (Linux)")
def test_lock_replaced_between_owner_scan_and_unlink_is_not_deleted(repo: Path, monkeypatch) -> None:
    """The proof covers the file examined, not whatever the path names by the time we unlink (C13-ASTRA-07)."""
    import clover_cli._git_lock_owner as owner
    from clover_cli.gitlock import release_dead_index_lock

    lock = repo / ".git" / "index.lock"
    _touch(lock, 2 * STALE_LOCK_MIN_AGE_SECONDS)
    real_scan = owner._held_open
    children: list[subprocess.Popen] = []

    def scan_then_concurrent_recovery_and_git(path, root, **kwargs):
        verdict = real_scan(path, root, **kwargs)
        if children:  # only the first scan is interleaved
            return verdict
        assert verdict is False, verdict
        # A concurrent updater releases the same dead lock after our scan, then a
        # real live git takes a NEW lock before our unlink.
        with monkeypatch.context() as concurrent:
            concurrent.setattr(owner, "_held_open", real_scan)
            assert release_dead_index_lock(repo) is True
        children.append(_status_blocked_on_a_fifo(repo))
        assert real_scan(path, root, **kwargs), "premise: replacement lock has a live holder"
        return verdict

    monkeypatch.setattr(owner, "_held_open", scan_then_concurrent_recovery_and_git)
    try:
        removed = release_dead_index_lock(repo)
        assert children and children[0].poll() is None
        assert not removed and lock.exists(), ("deleted live replacement index.lock", removed)
        assert not list(lock.parent.glob("index.lock.clover-dead-*")), "quarantine file left behind"
    finally:
        for child in children:
            child.kill()
            child.wait(timeout=5)


def test_unlink_if_same_file_refuses_a_swapped_in_lock(tmp_path: Path) -> None:
    from clover_cli._git_lock_owner import _lock_identity, _unlink_if_same_file

    lock = tmp_path / "index.lock"
    lock.write_text("old")
    examined = _lock_identity(lock)
    assert _unlink_if_same_file(lock, examined) is True and not lock.exists()
    lock.write_text("new, longer")  # a different file under the same name
    assert _unlink_if_same_file(lock, examined) is None
    assert lock.read_text() == "new, longer"
    assert _unlink_if_same_file(tmp_path / "gone.lock", examined) is None


def test_young_lock_is_never_unlinked_even_when_it_is_the_examined_file(tmp_path: Path) -> None:
    """Age is part of the proof: a fresh file may be a live git's, and nothing can undo a wrong delete."""
    from clover_cli._git_lock_owner import _lock_identity, _unlink_if_same_file

    lock = tmp_path / "index.lock"
    lock.write_text("x")
    assert _unlink_if_same_file(lock, _lock_identity(lock), STALE_LOCK_MIN_AGE_SECONDS) is None
    assert lock.exists()
    _age(lock, 2 * STALE_LOCK_MIN_AGE_SECONDS)
    assert _unlink_if_same_file(lock, _lock_identity(lock), STALE_LOCK_MIN_AGE_SECONDS) is True
    assert not lock.exists()


def test_removing_a_dead_lock_never_renames_or_links(repo: Path, monkeypatch) -> None:
    """The unlink is the only filesystem operation: a move-aside has no safe undo (C13-ASTRA-07)."""
    import clover_cli._git_lock_owner as owner
    from clover_cli.gitlock import release_dead_index_lock

    lock = repo / ".git" / "index.lock"
    _touch(lock, 2 * STALE_LOCK_MIN_AGE_SECONDS)
    monkeypatch.setattr(owner, "_held_open", lambda *a, **k: False)
    calls: list[str] = []
    for name in ("rename", "replace", "link", "symlink"):
        monkeypatch.setattr(owner.os, name, lambda *a, _n=name, **k: calls.append(_n))
    assert release_dead_index_lock(repo) is True
    assert not lock.exists() and calls == []


@pytest.mark.linux_only
@pytest.mark.parametrize("restoration", ["another_git", "link_fails"])
def test_cleanup_never_moves_the_lock_aside(repo: Path, monkeypatch, restoration: str) -> None:
    """C13-ASTRA-07: rename-to-quarantine + link-back lost a live git's lock whenever the link-back
    failed (EEXIST because a second git took the vacant name, EOPNOTSUPP, ...) and the quarantine was
    then unlinked. The cleanup now never renames or links: a replacement it did not examine survives,
    and no pathname is ever left vacant for a second git to take."""
    import errno
    import clover_cli._git_lock_owner as owner
    from clover_cli.gitlock import release_dead_index_lock

    lock = repo / ".git" / "index.lock"
    _touch(lock, 2 * STALE_LOCK_MIN_AGE_SECONDS)
    real_scan = owner._held_open
    children: list[subprocess.Popen] = []
    moved: list[tuple] = []

    def scan_then_replace(path, root, **kwargs):
        verdict = real_scan(path, root, **kwargs)
        if not children:
            assert verdict is False, verdict
            lock.unlink()  # the dead lock is released by someone else ...
            children.append(_status_blocked_on_a_fifo(repo))  # ... and a live git takes a new one
        return verdict

    def forbidden(name):
        def hook(*args, **kwargs):
            moved.append((name, args))
            if name == "link" and restoration == "link_fails":
                raise OSError(errno.EOPNOTSUPP, "hard links unavailable")
            return None
        return hook

    monkeypatch.setattr(owner, "_held_open", scan_then_replace)
    monkeypatch.setattr(owner.os, "rename", forbidden("rename"))
    monkeypatch.setattr(owner.os, "link", forbidden("link"))
    monkeypatch.setattr(owner.os, "replace", forbidden("replace"))
    try:
        removed = release_dead_index_lock(repo)
        assert children and children[0].poll() is None
        assert removed is False
        assert lock.exists(), "deleted the live git's replacement index.lock"
        assert moved == [], ("cleanup moved the lock aside", moved)
        assert not list(lock.parent.glob("index.lock.clover-dead-*"))
    finally:
        for child in children:
            child.kill()
            child.wait(timeout=5)
