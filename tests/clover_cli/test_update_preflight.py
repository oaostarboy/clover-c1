"""Behavior contracts for the read-only update pre-check (clover_cli.update_preflight).

Every check is exercised against REAL temporary git repositories wherever the
condition can be built for real (not-a-git-copy, empty/broken repo, missing
origin, read-only folder, wrong branch dirty/clean/unmerged, dirty tree with
untracked files, shallow clone with unrelated history, network/branch probe
against a local origin). Host-specific conditions that can't be built safely
(the Windows process scans, free disk space) are covered through their pure
classification seams.

The read-only promise itself is a contract: a full pre-check run over a dirty,
wrong-branch checkout must leave every file (including everything in .git)
byte- and mtime-identical, and must never delete a stale update marker.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from clover_cli import update_preflight as pf

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="needs git",
)


# ---------------------------------------------------------------------------
# Real git fixtures
# ---------------------------------------------------------------------------


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(repo: Path, name: str, content: str, msg: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def upstream(tmp_path):
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "package.json").write_text("{}\n", encoding="utf-8")
    (repo / "pkg" / "package-lock.json").write_text("{}\n", encoding="utf-8")
    for i in (1, 2, 3):
        _commit(repo, "app.py", f"print({i})\n", f"c{i}")
    return repo


@pytest.fixture
def checkout(upstream, tmp_path):
    """A full clone of *upstream* on main, origin set, origin/main present."""
    dest = tmp_path / "install"
    _git(tmp_path, "clone", "-q", upstream.as_uri(), str(dest))
    return dest


@pytest.fixture
def clean_config(monkeypatch):
    """No updates.* overrides: the shipped defaults."""
    monkeypatch.setattr("clover_cli.config.load_config", lambda: {})


def _with_updates_config(monkeypatch, **updates):
    monkeypatch.setattr("clover_cli.config.load_config", lambda: {"updates": dict(updates)})


def _codes(checks, status=None):
    return [c.code for c in checks if status is None or c.status == status]


def _assert_plain(check):
    """Every non-ok verdict is one readable sentence with a fix for blocks."""
    assert check.reason and check.reason[0].isupper(), check
    assert check.reason.rstrip().endswith("."), check
    assert "Traceback" not in check.reason
    if check.status == pf.BLOCK:
        assert check.fix, f"block without a fix: {check}"


# ---------------------------------------------------------------------------
# not a git checkout / git missing / broken repo / no origin
# ---------------------------------------------------------------------------


def test_plain_file_copy_warns_that_it_will_be_adopted(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "app.py").write_text("print(1)\n", encoding="utf-8")

    checks = pf.check_git_checkout(copy, tmp_path / "home")

    assert _codes(checks) == ["not_git_checkout"]
    assert checks[0].status == pf.WARN
    _assert_plain(checks[0])


@pytest.mark.linux_only
def test_plain_file_copy_without_git_is_blocked_with_install_hint(tmp_path, monkeypatch):
    copy = tmp_path / "copy"
    copy.mkdir()
    monkeypatch.setattr(pf.shutil, "which", lambda name: None)

    checks = pf.check_git_checkout(copy, tmp_path / "home")

    assert _codes(checks, pf.BLOCK) == ["git_missing"]
    _assert_plain(checks[0])
    assert "install git" in checks[0].fix.lower()


def test_git_repo_without_commits_is_blocked_and_points_at_reinstall(tmp_path):
    repo = tmp_path / "broken"
    repo.mkdir()
    _git(repo, "init", "-q")
    home = tmp_path / "home"

    checks = pf.check_git_checkout(repo, home)

    assert _codes(checks, pf.BLOCK) == ["git_broken"]
    _assert_plain(checks[0])
    assert str(home) in checks[0].fix, "the fix must say the user's data is kept"


def test_healthy_checkout_passes_git_and_origin(checkout, tmp_path):
    assert _codes(pf.check_git_checkout(checkout, tmp_path), pf.OK) == ["git_checkout"]
    assert _codes(pf.check_origin_remote(checkout), pf.OK) == ["origin_remote"]


def test_missing_origin_remote_is_blocked_with_exact_command(checkout):
    _git(checkout, "remote", "remove", "origin")

    checks = pf.check_origin_remote(checkout)

    assert _codes(checks, pf.BLOCK) == ["no_origin_remote"]
    _assert_plain(checks[0])
    assert "remote add origin" in checks[0].fix


# ---------------------------------------------------------------------------
# read-only install folder
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="POSIX permission bits; root ignores them")
def test_read_only_install_dir_is_blocked(checkout):
    objects = checkout / ".git" / "objects"
    original = objects.stat().st_mode
    objects.chmod(0o555)
    try:
        checks = pf.check_install_writable(checkout)
    finally:
        objects.chmod(original)

    assert _codes(checks, pf.BLOCK) == ["install_read_only"]
    _assert_plain(checks[0])
    assert "chown" in checks[0].fix


def test_writable_install_dir_passes(checkout):
    assert _codes(pf.check_install_writable(checkout)) == ["install_writable"]


# ---------------------------------------------------------------------------
# disk space
# ---------------------------------------------------------------------------


class _Usage:
    def __init__(self, free_mb):
        self.free = free_mb * 1024 * 1024


@pytest.mark.parametrize(
    "free_mb, expected_code, expected_status",
    [
        (100, "low_disk_space", pf.BLOCK),
        (900, "disk_space_low", pf.WARN),
        (50_000, "disk_space", pf.OK),
    ],
)
def test_disk_space_thresholds_come_from_settings(tmp_path, monkeypatch, free_mb, expected_code, expected_status):
    monkeypatch.setattr(pf.shutil, "disk_usage", lambda _p: _Usage(free_mb))
    settings = pf.preflight_settings({"updates": {"preflight": {"min_free_disk_mb": 500, "warn_free_disk_mb": 1500}}})

    checks = pf.check_disk_space(tmp_path, settings)

    assert [(c.code, c.status) for c in checks] == [(expected_code, expected_status)]
    if expected_status != pf.OK:
        _assert_plain(checks[0])


def test_disk_threshold_zero_disables_the_check(tmp_path, monkeypatch):
    monkeypatch.setattr(pf.shutil, "disk_usage", lambda _p: _Usage(1))
    settings = pf.preflight_settings({"updates": {"preflight": {"min_free_disk_mb": 0, "warn_free_disk_mb": 0}}})
    assert _codes(pf.check_disk_space(tmp_path, settings), pf.BLOCK) == []


# ---------------------------------------------------------------------------
# wrong branch (the updater's parked-branch guard) and local changes
# ---------------------------------------------------------------------------


def test_wrong_branch_with_unsaved_changes_is_blocked(checkout, clean_config):
    """Clover itself, 2026-10-07: parked on a feature branch with edits."""
    _git(checkout, "checkout", "-q", "-b", "fix/something")
    (checkout / "app.py").write_text("print('edited')\n", encoding="utf-8")
    (checkout / "notes.txt").write_text("untracked\n", encoding="utf-8")

    checks = pf.check_branch_and_changes(checkout, "main")

    assert _codes(checks, pf.BLOCK) == ["parked_branch_dirty"]
    _assert_plain(checks[0])
    assert "fix/something" in checks[0].reason
    assert "2 unsaved change(s)" in checks[0].reason
    assert "git" in checks[0].fix and "checkout main" in checks[0].fix


def test_wrong_branch_block_predicts_the_real_updater_guard(checkout, clean_config):
    """The pre-check's verdict IS the updater's own guard verdict."""
    from clover_cli import update_cmd

    _git(checkout, "checkout", "-q", "-b", "fix/something")
    (checkout / "app.py").write_text("print('edited')\n", encoding="utf-8")

    safe, reason = update_cmd._assess_parked_branch_switch(["git"], checkout, "fix/something", "main")
    checks = pf.check_branch_and_changes(checkout, "main")

    assert (safe, reason) == (False, "dirty")
    assert _codes(checks, pf.BLOCK) == ["parked_branch_dirty"]


def test_wrong_branch_fully_merged_and_clean_is_ok(checkout, clean_config):
    _git(checkout, "checkout", "-q", "-b", "old-branch")

    checks = pf.check_branch_and_changes(checkout, "main")

    assert _codes(checks, pf.BLOCK) == []
    assert _codes(checks, pf.WARN) == []
    assert "parked_branch_merged" in _codes(checks, pf.OK)


def test_wrong_branch_with_unmerged_commits_warns_commits_are_kept(checkout, clean_config):
    _git(checkout, "checkout", "-q", "-b", "my-work")
    _commit(checkout, "mine.txt", "mine\n", "my commit")

    checks = pf.check_branch_and_changes(checkout, "main")

    assert _codes(checks, pf.WARN) == ["parked_branch_unmerged"]
    _assert_plain(checks[0])
    assert "1 commit(s)" in checks[0].reason


def test_wrong_branch_with_auto_switch_disabled_is_blocked(checkout, monkeypatch):
    _with_updates_config(monkeypatch, auto_switch_parked_branch=False)
    _git(checkout, "checkout", "-q", "-b", "old-branch")

    checks = pf.check_branch_and_changes(checkout, "main")

    assert _codes(checks, pf.BLOCK) == ["parked_branch_locked"]
    _assert_plain(checks[0])
    assert "auto_switch_parked_branch" in checks[0].reason


def test_dirty_tree_on_main_with_untracked_files_warns_not_blocks(checkout, clean_config):
    (checkout / "app.py").write_text("print('hand edit')\n", encoding="utf-8")
    (checkout / "scratch.txt").write_text("untracked\n", encoding="utf-8")

    checks = pf.check_branch_and_changes(checkout, "main")

    assert _codes(checks, pf.BLOCK) == []
    assert _codes(checks, pf.WARN) == ["local_changes"]
    assert "2 changed file(s)" in checks[-1].reason
    _assert_plain(checks[-1])


def test_dirty_tree_note_says_when_config_discards_changes(checkout, monkeypatch):
    _with_updates_config(monkeypatch, non_interactive_local_changes="discard")
    (checkout / "app.py").write_text("print('hand edit')\n", encoding="utf-8")

    checks = pf.check_branch_and_changes(checkout, "main", gateway_mode=True)

    assert _codes(checks, pf.WARN) == ["local_changes"]
    assert "throw them away" in checks[-1].reason


def test_npm_lockfile_churn_alone_is_not_a_local_change(checkout, clean_config):
    """The updater discards package-lock.json churn before looking at the tree."""
    (checkout / "pkg" / "package-lock.json").write_text('{"churn": 1}\n', encoding="utf-8")

    checks = pf.check_branch_and_changes(checkout, "main")

    assert _codes(checks, pf.WARN) == []
    assert "clean_tree" in _codes(checks, pf.OK)


def test_npm_lockfile_churn_on_parked_branch_is_not_dirty(checkout, clean_config):
    _git(checkout, "checkout", "-q", "-b", "old-branch")
    (checkout / "pkg" / "package-lock.json").write_text('{"churn": 1}\n', encoding="utf-8")

    checks = pf.check_branch_and_changes(checkout, "main")

    assert _codes(checks, pf.BLOCK) == []


# ---------------------------------------------------------------------------
# shallow clones
# ---------------------------------------------------------------------------


@pytest.fixture
def shallow(upstream, tmp_path):
    dest = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", "--branch", "main", upstream.as_uri(), str(dest))
    return dest


def test_shallow_clone_on_main_is_ok(shallow, clean_config):
    checks = pf.check_shallow(shallow, "main")
    assert [(c.code, c.status) for c in checks] == [("shallow_clone", pf.OK)]


def test_shallow_clone_with_unrelated_history_blocks_in_place_merge(upstream, shallow, monkeypatch):
    """A depth-1 install whose history no longer connects to origin/main.

    RED proof first: the exact command the updater runs on this path
    (`git merge --no-edit origin/main` on a custom branch with
    update_in_place) really fails. Then the pre-check must refuse up front.
    """
    _with_updates_config(monkeypatch, parked_branch_strategy="update_in_place")
    _git(shallow, "checkout", "-q", "-b", "custom")
    _commit(shallow, "local.txt", "local\n", "local patch")
    # Upstream rewrites history (force-push), then the install fetches shallowly.
    _git(upstream, "reset", "-q", "--hard", "HEAD~2")
    _commit(upstream, "app.py", "print('rewritten')\n", "rewritten")
    _git(shallow, "fetch", "-q", "--depth", "1", "origin", "+refs/heads/main:refs/remotes/origin/main")

    probe = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "merge", "--no-commit", "--no-ff", "origin/main"],
        cwd=shallow, capture_output=True, text=True,
    )
    assert probe.returncode != 0 and "unrelated histories" in probe.stderr
    subprocess.run(["git", "merge", "--abort"], cwd=shallow, capture_output=True)

    checks = pf.check_shallow(shallow, "main")

    assert _codes(checks, pf.BLOCK) == ["shallow_unrelated_history"]
    _assert_plain(checks[0])
    assert "fetch --unshallow origin" in checks[0].fix


def test_shallow_custom_branch_with_switch_strategy_is_ok(upstream, shallow, clean_config):
    _git(shallow, "checkout", "-q", "-b", "custom")
    assert _codes(pf.check_shallow(shallow, "main"), pf.BLOCK) == []


def test_full_clone_reports_nothing_for_shallow(checkout):
    assert pf.check_shallow(checkout, "main") == []


# ---------------------------------------------------------------------------
# trial (canary) build through PYTHONPATH
# ---------------------------------------------------------------------------


def _clover_tree(path: Path) -> Path:
    (path / "clover_cli").mkdir(parents=True)
    (path / "clover_cli" / "__init__.py").write_text("", encoding="utf-8")
    return path


def test_running_from_a_trial_tree_instead_of_the_install_is_blocked(tmp_path):
    """Clover itself: a canary worktree on PYTHONPATH, editable install elsewhere."""
    trial = _clover_tree(tmp_path / "canary")
    real = _clover_tree(tmp_path / "install")

    checks = pf.check_trial_build(trial, env_pythonpath=str(trial), gateway_pythonpath="", installed_root=real)

    assert _codes(checks, pf.BLOCK) == ["trial_build_active"]
    _assert_plain(checks[0])
    assert str(trial) in checks[0].reason and str(real) in checks[0].reason


def test_gateway_importing_another_tree_is_blocked(tmp_path):
    trial = _clover_tree(tmp_path / "canary")
    real = _clover_tree(tmp_path / "install")

    checks = pf.check_trial_build(real, env_pythonpath="", gateway_pythonpath=str(trial), installed_root=real)

    assert _codes(checks, pf.BLOCK) == ["trial_build_active"]


def test_install_on_its_own_pythonpath_is_not_a_trial(tmp_path):
    """A Windows gateway puts its own install + venv site-packages on PYTHONPATH."""
    real = _clover_tree(tmp_path / "install")
    site = _clover_tree(tmp_path / "install" / "venv" / "Lib" / "site-packages")
    pythonpath = os.pathsep.join([str(real), str(site)])

    assert pf.check_trial_build(real, env_pythonpath=pythonpath, gateway_pythonpath=pythonpath, installed_root=real) == []
    assert pf.check_trial_build(real, env_pythonpath=pythonpath, gateway_pythonpath="", installed_root=None) == []


def test_non_clover_pythonpath_entries_are_ignored(tmp_path):
    other = tmp_path / "libs"
    other.mkdir()
    real = _clover_tree(tmp_path / "install")
    assert pf.check_trial_build(real, env_pythonpath=str(other), gateway_pythonpath=str(other), installed_root=real) == []


def test_editable_dir_from_direct_url_round_trips_a_real_path(tmp_path):
    import json

    target = tmp_path / "some dir" / "install"
    text = json.dumps({"url": target.as_uri(), "dir_info": {"editable": True}})
    assert pf._same_path(pf.editable_dir_from_direct_url(text), target)
    assert pf.editable_dir_from_direct_url(json.dumps({"url": target.as_uri(), "dir_info": {}})) is None
    assert pf.editable_dir_from_direct_url("not json") is None


# ---------------------------------------------------------------------------
# another update already running (shared update marker)
# ---------------------------------------------------------------------------


@pytest.fixture
def live_pid():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


def test_live_update_marker_blocks(tmp_path, live_pid):
    from clover_cli.update_lock import MARKER_NAME

    (tmp_path / MARKER_NAME).write_text(f"{live_pid}\n{int(time.time()) - 90}\n", encoding="utf-8")

    checks = pf.check_update_in_progress(tmp_path)

    assert _codes(checks, pf.BLOCK) == ["update_in_progress"]
    _assert_plain(checks[0])
    assert "1 minute(s) ago" in checks[0].reason


def test_stale_update_marker_is_ok_and_left_in_place(tmp_path):
    from clover_cli.update_lock import MARKER_NAME

    marker = tmp_path / MARKER_NAME
    marker.write_text("999999999\n0\n", encoding="utf-8")

    checks = pf.check_update_in_progress(tmp_path)

    assert _codes(checks) == ["no_update_running"]
    assert marker.exists(), "the pre-check must never delete anything"


def test_no_marker_is_ok(tmp_path):
    assert _codes(pf.check_update_in_progress(tmp_path)) == ["no_update_running"]


# ---------------------------------------------------------------------------
# network / branch on origin (local file:// origin, no real network)
# ---------------------------------------------------------------------------


def test_reachable_origin_is_ok(checkout):
    assert _codes(pf.check_network(checkout, "main", pf.preflight_settings({}))) == ["network"]


def test_branch_missing_on_origin_is_blocked(checkout):
    checks = pf.check_network(checkout, "no-such-branch", pf.preflight_settings({}))
    assert _codes(checks, pf.BLOCK) == ["branch_not_on_origin"]
    _assert_plain(checks[0])


def test_unreachable_origin_is_blocked_with_the_wire_error(checkout, tmp_path):
    gone = tmp_path / "gone.git"
    _git(checkout, "remote", "set-url", "origin", gone.as_uri())

    checks = pf.check_network(checkout, "main", pf.preflight_settings({}))

    assert _codes(checks, pf.BLOCK) == ["network_unreachable"]
    _assert_plain(checks[0])


def test_network_check_can_be_turned_off(checkout, tmp_path):
    _git(checkout, "remote", "set-url", "origin", (tmp_path / "gone.git").as_uri())
    settings = pf.preflight_settings({"updates": {"preflight": {"check_network": False}}})
    assert pf.check_network(checkout, "main", settings) == []


def test_network_timeout_is_only_a_note(checkout, monkeypatch):
    monkeypatch.setattr(pf, "_git", lambda *a, **k: None)
    checks = pf.check_network(checkout, "main", pf.preflight_settings({}))
    assert [(c.code, c.status) for c in checks] == [("network_slow", pf.WARN)]


# ---------------------------------------------------------------------------
# Windows lock classification (pure seam; the scan itself is Windows-only)
# ---------------------------------------------------------------------------


def _is_gw(cmdline: str) -> bool:
    return "gateway run" in cmdline


def test_venv_holders_gateways_and_their_children_are_not_blockers():
    holders = [
        (10, "pythonw.exe", "pythonw.exe -m clover_cli.main gateway run"),
        (11, "python.exe", "python.exe -m some_mcp_server"),  # child of the gateway
        (12, "python.exe", "python.exe -m clover_cli.main serve"),
    ]
    remaining = pf.classify_venv_holders(
        holders,
        is_pausable_gateway=_is_gw,
        ancestors_of=lambda pid: {10} if pid == 11 else {1},
    )
    assert [h[0] for h in remaining] == [12]


def test_venv_holders_the_calling_gateway_counts_as_a_gateway():
    holders = [(21, "python.exe", "python.exe tool.py")]
    remaining = pf.classify_venv_holders(
        holders, is_pausable_gateway=_is_gw, ancestors_of=lambda pid: {20}, extra_gateway_pids=[20],
    )
    assert remaining == []


def test_venv_holders_reap_rungs_remove_what_the_updater_would_stop():
    holders = [(30, "python.exe", "serve"), (31, "python.exe", "dashboard"), (32, "python.exe", "repl")]
    remaining = pf.classify_venv_holders(
        holders,
        is_pausable_gateway=_is_gw,
        reap_rungs=(lambda hs: [30], lambda hs: None, lambda hs: [{"pid": 31}], lambda hs: 1 / 0),
    )
    assert [h[0] for h in remaining] == [32]


@pytest.mark.linux_only
def test_windows_lock_check_is_silent_off_windows():
    assert pf.check_windows_locks() == []


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def test_settings_defaults_and_overrides():
    defaults = pf.preflight_settings({})
    assert defaults["enabled"] is True and defaults["check_network"] is True
    assert pf.preflight_settings({"updates": {"preflight": False}})["enabled"] is False
    custom = pf.preflight_settings(
        {"updates": {"preflight": {"enabled": "false", "min_free_disk_mb": "250", "network_timeout_seconds": "bad"}}}
    )
    assert custom["enabled"] is False
    assert custom["min_free_disk_mb"] == 250
    assert custom["network_timeout_seconds"] == defaults["network_timeout_seconds"]


def test_settings_survive_a_broken_config_loader(monkeypatch):
    def boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("clover_cli.config.load_config", boom)
    assert pf.preflight_settings()["enabled"] is True


# ---------------------------------------------------------------------------
# The runner: read-only, never raises, ordering
# ---------------------------------------------------------------------------


def _snapshot(root: Path) -> dict[str, tuple[str, int]]:
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            st = path.stat()
            out[str(path.relative_to(root))] = (hashlib.sha256(path.read_bytes()).hexdigest(), st.st_mtime_ns)
        elif path.is_dir():
            out[str(path.relative_to(root)) + "/"] = ("dir", 0)
    return out


@pytest.mark.real_update_preflight
def test_full_preflight_run_changes_nothing_in_the_install(checkout, tmp_path, clean_config):
    """The pre-check's core promise, on a dirty wrong-branch install."""
    _git(checkout, "checkout", "-q", "-b", "fix/something")
    (checkout / "app.py").write_text("print('edited')\n", encoding="utf-8")
    (checkout / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    time.sleep(0.05)
    before = _snapshot(checkout)

    report = pf.run_update_preflight(project_root=checkout, clover_home=home, branch="main")

    assert report.blocked and report.codes(pf.BLOCK) == ["parked_branch_dirty"]
    assert _snapshot(checkout) == before
    assert list(home.iterdir()) == []


@pytest.mark.real_update_preflight
def test_healthy_install_passes_with_no_blocks_or_warnings(checkout, tmp_path, clean_config, monkeypatch):
    monkeypatch.setattr(pf.shutil, "disk_usage", lambda _p: _Usage(50_000))
    report = pf.run_update_preflight(project_root=checkout, clover_home=tmp_path, branch="main")
    assert not report.blocked, report.to_dict()
    assert report.warnings == [], report.to_dict()
    assert {"git_checkout", "origin_remote", "install_writable", "disk_space", "network"} <= set(report.codes(pf.OK))


@pytest.mark.real_update_preflight
def test_a_crashing_check_never_stops_the_update(checkout, tmp_path, clean_config, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(pf, "check_disk_space", boom)
    report = pf.run_update_preflight(project_root=checkout, clover_home=tmp_path, branch="main")
    assert "disk_unchecked" in report.codes(pf.OK)
    assert not report.blocked


@pytest.mark.real_update_preflight
def test_broken_repo_stops_further_git_checks(tmp_path, clean_config):
    repo = tmp_path / "broken"
    repo.mkdir()
    _git(repo, "init", "-q")
    report = pf.run_update_preflight(project_root=repo, clover_home=tmp_path, branch="main")
    assert report.codes(pf.BLOCK) == ["git_broken"]
    assert "origin_remote" not in report.codes() and "network" not in report.codes()


@pytest.mark.real_update_preflight
def test_plain_copy_install_end_to_end_is_not_blocked(tmp_path, clean_config, monkeypatch):
    """A plain copy is adoptable; the pre-check must not refuse it."""
    monkeypatch.setattr(pf.shutil, "disk_usage", lambda _p: _Usage(50_000))
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "app.py").write_text("print(1)\n", encoding="utf-8")
    report = pf.run_update_preflight(project_root=copy, clover_home=tmp_path, branch="main")
    assert not report.blocked
    assert report.codes(pf.WARN) == ["not_git_checkout"]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _report(*checks):
    return pf.PreflightReport(checks=list(checks))


_BLOCK = pf.PreflightCheck("parked_branch_dirty", pf.BLOCK, "Reason sentence.", "Run: fix it")
_WARN = pf.PreflightCheck("local_changes", pf.WARN, "Note sentence.")
_OK = pf.PreflightCheck("git_checkout", pf.OK, "Fine.")


def test_cli_block_message_has_reason_fix_code_and_skip_hint():
    text = pf.format_cli_block(_report(_OK, _WARN, _BLOCK))
    assert text.startswith("✗ Update not started")
    assert "Nothing was changed" in text
    assert "Reason sentence." in text and "Fix: Run: fix it" in text and "parked_branch_dirty" in text
    assert "--skip-preflight" in text
    assert "Fine." not in text and "Note sentence." not in text


def test_chat_block_is_one_message_with_every_block():
    second = pf.PreflightCheck("low_disk_space", pf.BLOCK, "Disk sentence.", "Free space.")
    text = pf.format_chat_block(_report(_BLOCK, second, _WARN))
    assert text.startswith("⚠️ Update not started — nothing was changed.")
    assert "Reason sentence." in text and "Disk sentence." in text
    assert "Note sentence." not in text


def test_reports_list_every_check():
    for render in (pf.format_cli_report, pf.format_chat_report):
        text = render(_report(_OK, _WARN, _BLOCK))
        assert "Fine." in text and "Note sentence." in text and "Reason sentence." in text


def test_chat_check_request_parsing():
    assert pf.chat_check_requested("check")
    assert pf.chat_check_requested(" --check ")
    assert not pf.chat_check_requested("")
    assert not pf.chat_check_requested("now please")
