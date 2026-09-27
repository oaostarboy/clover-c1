"""Post-update SAFE doctor repairs must self-heal without blocking `clover update`.

A successful `clover update` is the moment abandoned git locks, an unseeded
`skills/` dir, or a missing core runtime dep are most likely to have been
left behind — but until now a user only discovered them by separately
running `clover doctor`. `_run_post_update_safe_repairs` runs the same
SAFE-tier checks right after a successful update and reports one short line
per repair performed or problem found. Each repair is independent: a failure
in one must never raise out of the function or block the update.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import patch

from clover_cli import update_cmd


def _patch_m(monkeypatch, tmp_path):
    monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(PROJECT_ROOT=tmp_path))


def test_no_repairs_needed_prints_nothing(monkeypatch, tmp_path, capsys):
    _patch_m(monkeypatch, tmp_path)
    with patch("clover_cli.gitlock.clear_stale_git_locks", return_value=[]), \
         patch("clover_cli.gitlock.clear_stale_tmp_packs", return_value=[]), \
         patch("tools.skills_sync.bundled_skills_dir_is_unseeded", return_value=False), \
         patch("tools.skills_sync.sync_skills") as sync_skills:
        update_cmd._run_post_update_safe_repairs()
    sync_skills.assert_not_called()
    assert capsys.readouterr().out == ""


def test_stale_git_locks_cleared_prints_one_line(monkeypatch, tmp_path, capsys):
    _patch_m(monkeypatch, tmp_path)
    with patch(
        "clover_cli.gitlock.clear_stale_git_locks",
        return_value=[str(tmp_path / ".git" / "shallow.lock")],
    ), patch("clover_cli.gitlock.clear_stale_tmp_packs", return_value=[]), \
         patch("tools.skills_sync.bundled_skills_dir_is_unseeded", return_value=False):
        update_cmd._run_post_update_safe_repairs()
    out = capsys.readouterr().out
    assert "Cleared 1 stale git lock(s)" in out


def test_git_lock_repair_failure_is_caught_and_reported(monkeypatch, tmp_path, capsys):
    _patch_m(monkeypatch, tmp_path)
    with patch(
        "clover_cli.gitlock.clear_stale_git_locks", side_effect=OSError("boom")
    ), patch("tools.skills_sync.bundled_skills_dir_is_unseeded", return_value=False):
        update_cmd._run_post_update_safe_repairs()  # must not raise
    out = capsys.readouterr().out
    assert "Post-update git lock repair failed" in out
    assert "boom" in out


def test_unseeded_skills_dir_is_reseeded(monkeypatch, tmp_path, capsys):
    _patch_m(monkeypatch, tmp_path)
    with patch("clover_cli.gitlock.clear_stale_git_locks", return_value=[]), \
         patch("clover_cli.gitlock.clear_stale_tmp_packs", return_value=[]), \
         patch(
             "tools.skills_sync.bundled_skills_dir_is_unseeded",
             side_effect=[True, False],
         ) as unseeded, \
         patch("tools.skills_sync.sync_skills") as sync_skills:
        update_cmd._run_post_update_safe_repairs()
    sync_skills.assert_called_once_with(quiet=True)
    assert unseeded.call_count == 2
    assert "Reseeded bundled skills" in capsys.readouterr().out


def test_skills_reseed_that_still_fails_reports_it(monkeypatch, tmp_path, capsys):
    _patch_m(monkeypatch, tmp_path)
    with patch("clover_cli.gitlock.clear_stale_git_locks", return_value=[]), \
         patch("clover_cli.gitlock.clear_stale_tmp_packs", return_value=[]), \
         patch("tools.skills_sync.bundled_skills_dir_is_unseeded", return_value=True), \
         patch("tools.skills_sync.sync_skills"):
        update_cmd._run_post_update_safe_repairs()
    out = capsys.readouterr().out
    assert "still empty after reseed attempt" in out


def test_skills_reseed_failure_is_caught_and_reported(monkeypatch, tmp_path, capsys):
    _patch_m(monkeypatch, tmp_path)
    with patch("clover_cli.gitlock.clear_stale_git_locks", return_value=[]), \
         patch("clover_cli.gitlock.clear_stale_tmp_packs", return_value=[]), \
         patch(
             "tools.skills_sync.bundled_skills_dir_is_unseeded",
             side_effect=RuntimeError("kaboom"),
         ):
        update_cmd._run_post_update_safe_repairs()  # must not raise
    out = capsys.readouterr().out
    assert "Post-update skills reseed failed" in out
    assert "kaboom" in out


def test_missing_core_runtime_module_is_reported(monkeypatch, tmp_path, capsys):
    _patch_m(monkeypatch, tmp_path)
    doctor = importlib.import_module("clover_cli.doctor")  # the module update_cmd imports from

    monkeypatch.setattr(
        doctor, "_CORE_RUNTIME_MODULES", [("definitely_not_a_real_module_xyz", "Fakepkg")]
    )
    with patch("clover_cli.gitlock.clear_stale_git_locks", return_value=[]), \
         patch("clover_cli.gitlock.clear_stale_tmp_packs", return_value=[]), \
         patch("tools.skills_sync.bundled_skills_dir_is_unseeded", return_value=False):
        update_cmd._run_post_update_safe_repairs()
    out = capsys.readouterr().out
    assert "Core runtime dependencies missing after update: Fakepkg" in out


def test_healthy_core_runtime_modules_print_nothing(monkeypatch, tmp_path, capsys):
    _patch_m(monkeypatch, tmp_path)
    doctor = importlib.import_module("clover_cli.doctor")  # the module update_cmd imports from

    monkeypatch.setattr(doctor, "_CORE_RUNTIME_MODULES", [("os", "os")])
    with patch("clover_cli.gitlock.clear_stale_git_locks", return_value=[]), \
         patch("clover_cli.gitlock.clear_stale_tmp_packs", return_value=[]), \
         patch("tools.skills_sync.bundled_skills_dir_is_unseeded", return_value=False):
        update_cmd._run_post_update_safe_repairs()
    assert capsys.readouterr().out == ""


def test_core_runtime_probe_failure_is_caught_and_reported(monkeypatch, tmp_path, capsys):
    _patch_m(monkeypatch, tmp_path)
    doctor = importlib.import_module("clover_cli.doctor")  # the module update_cmd imports from

    # A malformed entry (not a 2-tuple) makes the unpacking loop itself raise
    # — exercising the except path around the probe, not just ImportError.
    monkeypatch.setattr(doctor, "_CORE_RUNTIME_MODULES", ["not-a-pair"])
    with patch("clover_cli.gitlock.clear_stale_git_locks", return_value=[]), \
         patch("clover_cli.gitlock.clear_stale_tmp_packs", return_value=[]), \
         patch("tools.skills_sync.bundled_skills_dir_is_unseeded", return_value=False):
        update_cmd._run_post_update_safe_repairs()  # must not raise
    out = capsys.readouterr().out
    assert "Post-update core runtime import probe failed" in out


def test_all_three_repairs_run_independently(monkeypatch, tmp_path, capsys):
    """One repair failing must not prevent the other two from running."""
    _patch_m(monkeypatch, tmp_path)
    doctor = importlib.import_module("clover_cli.doctor")  # the module update_cmd imports from

    monkeypatch.setattr(doctor, "_CORE_RUNTIME_MODULES", [("os", "os")])
    with patch(
        "clover_cli.gitlock.clear_stale_git_locks", side_effect=OSError("locks boom")
    ), patch(
        "tools.skills_sync.bundled_skills_dir_is_unseeded",
        side_effect=[True, False],
    ), patch("tools.skills_sync.sync_skills") as sync_skills:
        update_cmd._run_post_update_safe_repairs()
    sync_skills.assert_called_once_with(quiet=True)
    out = capsys.readouterr().out
    assert "Post-update git lock repair failed" in out
    assert "Reseeded bundled skills" in out


def test_never_raises_even_when_every_repair_fails(monkeypatch, tmp_path):
    """The whole point of the hook: a repair failure must never propagate."""
    _patch_m(monkeypatch, tmp_path)
    with patch(
        "clover_cli.gitlock.clear_stale_git_locks", side_effect=OSError("a")
    ), patch(
        "tools.skills_sync.bundled_skills_dir_is_unseeded", side_effect=RuntimeError("b")
    ):
        update_cmd._run_post_update_safe_repairs()  # would raise pre-fix
