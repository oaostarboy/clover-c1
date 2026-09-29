"""Regression test for the single-branch-clone fetch bug (real upgrade failure).

`clover update --branch <feature>` on a fresh install fails with:

    Branch '<feature>' does not exist locally or on origin.
    fatal: 'origin/<feature>' is not a commit ...

Cause: the installer clones with `git clone --depth 1 --branch main` (implicitly
`--single-branch`), which scopes `remote.origin.fetch` to only `main`. The
update code then runs `git fetch origin <feature>`, which only updates
FETCH_HEAD — never `refs/remotes/origin/<feature>` — because that branch isn't
covered by the configured fetch refspec. Every later reference to
`origin/<feature>` (checkout -B, rev-parse, rev-list) then fails.

These tests use a real bare "origin" repo and a real
`git clone --depth 1 --branch main` shallow, single-branch clone of it over a
`file://` URL (so git can't silently optimize the shallow-ness away as it
sometimes does for local-path clones).
"""

import subprocess
from pathlib import Path

import pytest

import clover_cli.update_cmd as update_cmd

GIT = ["git"]


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        GIT + list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _ref_exists(cwd: Path, ref: str) -> bool:
    result = _git(cwd, "rev-parse", "--verify", "--quiet", ref, check=False)
    return result.returncode == 0


@pytest.fixture
def origin_and_clone(tmp_path):
    """A bare 'origin' repo with main + a feature branch, plus a real
    `--depth 1 --branch main` single-branch shallow clone of it."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "f.txt").write_text("one\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "init")
    _git(seed, "branch", "-M", "main")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-q", "origin", "main")

    _git(seed, "checkout", "-qb", "fix/restart-and-reasoning")
    (seed / "f.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(seed, "commit", "-qam", "feature work")
    _git(seed, "push", "-q", "origin", "fix/restart-and-reasoning")

    clone = tmp_path / "clone"
    _git(
        tmp_path,
        "clone",
        "--depth",
        "1",
        "--branch",
        "main",
        f"file://{bare}",
        str(clone),
    )
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "Test")
    return clone


def test_clone_is_shallow_and_single_branch(origin_and_clone):
    """Sanity-check the repro setup matches a real installer checkout."""
    is_shallow = _git(
        origin_and_clone, "rev-parse", "--is-shallow-repository"
    ).stdout.strip()
    assert is_shallow == "true"
    fetch_cfg = _git(
        origin_and_clone, "config", "--get-all", "remote.origin.fetch"
    ).stdout
    assert "main" in fetch_cfg
    assert "restart-and-reasoning" not in fetch_cfg


def test_plain_fetch_of_other_branch_does_not_write_tracking_ref(origin_and_clone):
    """Documents the underlying git behavior the bug depends on.

    A plain `git fetch origin <branch>` for a branch outside the configured
    single-branch scope succeeds (updates FETCH_HEAD) but never creates
    `refs/remotes/origin/<branch>`.
    """
    fetch = _git(
        origin_and_clone, "fetch", "origin", "fix/restart-and-reasoning", check=False
    )
    assert fetch.returncode == 0
    assert not _ref_exists(origin_and_clone, "origin/fix/restart-and-reasoning")


def test_fetch_branch_tracking_writes_origin_ref(origin_and_clone):
    """FAIL-BEFORE-FIX: the helper must fetch AND populate the tracking ref
    the rest of the update code (checkout -B, rev-parse) depends on."""
    result = update_cmd._fetch_branch_tracking(
        GIT, origin_and_clone, "origin", "fix/restart-and-reasoning"
    )
    assert result.returncode == 0
    assert _ref_exists(origin_and_clone, "origin/fix/restart-and-reasoning")


def test_fetch_branch_tracking_enables_checkout_switch(origin_and_clone):
    """End-to-end: reproduces the exact downstream failure from the bug report
    (`checkout -B <branch> origin/<branch>` after the scoped fetch)."""
    result = update_cmd._fetch_branch_tracking(
        GIT, origin_and_clone, "origin", "fix/restart-and-reasoning"
    )
    assert result.returncode == 0

    verify = _git(
        origin_and_clone,
        "rev-parse",
        "--verify",
        "--quiet",
        "origin/fix/restart-and-reasoning",
        check=False,
    )
    assert verify.returncode == 0, "origin/<branch> must resolve after the fetch"

    checkout = _git(
        origin_and_clone,
        "checkout",
        "-B",
        "fix/restart-and-reasoning",
        "origin/fix/restart-and-reasoning",
        check=False,
    )
    assert checkout.returncode == 0, checkout.stderr


def test_fetch_branch_tracking_preserves_shallow_depth(origin_and_clone):
    """Shallow clones must stay shallow when depth_args is passed through."""
    result = update_cmd._fetch_branch_tracking(
        GIT,
        origin_and_clone,
        "origin",
        "fix/restart-and-reasoning",
        depth_args=["--depth", "1"],
    )
    assert result.returncode == 0
    assert _ref_exists(origin_and_clone, "origin/fix/restart-and-reasoning")
    is_shallow = _git(
        origin_and_clone, "rev-parse", "--is-shallow-repository"
    ).stdout.strip()
    assert is_shallow == "true"


def test_fetch_branch_tracking_scopes_to_one_branch(origin_and_clone):
    """The fetch must not pull the whole ref space (thousands of branches in
    the real repo) — only the requested branch's tracking ref is created."""
    update_cmd._fetch_branch_tracking(
        GIT, origin_and_clone, "origin", "fix/restart-and-reasoning"
    )
    branches = _git(origin_and_clone, "branch", "-r").stdout
    lines = {line.strip() for line in branches.splitlines() if line.strip()}
    assert lines <= {"origin/main", "origin/fix/restart-and-reasoning"}
