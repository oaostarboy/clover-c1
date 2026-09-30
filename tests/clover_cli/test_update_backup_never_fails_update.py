"""The pre-update backup can never fail or refuse an update.

Windows 11 report (2026-09-29, 10:26): ``clover update --gateway --force``
exited 2 and the user's agent reported "pre-update backup step failed". The
real refusal was elsewhere; the receipt blamed the backup because a missing
snapshot (disabled by the shipped example config, or nothing captured) was
recorded as a FAILED step. A locked/in-use file must be skipped per file,
never sink the snapshot or the update.
"""

from __future__ import annotations

import json
import shutil

from clover_cli import backup
from clover_cli import main as clover_main
from clover_cli import update_cmd
from clover_constants import get_clover_home
from tests.clover_cli.test_update_fleet_restart_pending import (
    _make_up_to_date_side_effect,
    _patch_update_deps,
    _update_args,
)


def _latest_receipt() -> dict:
    path = get_clover_home() / "logs" / "update_receipts" / "latest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_missing_snapshot_is_a_skip_not_a_failed_step(monkeypatch, tmp_path):
    _patch_update_deps(monkeypatch, tmp_path, _make_up_to_date_side_effect())
    monkeypatch.setattr(clover_main, "_resolve_pre_update_backup_mode", lambda args: "off")

    clover_main.cmd_update(_update_args())

    receipt = _latest_receipt()
    failed = [s for s in receipt.get("steps", []) if s["name"] == "pre_update_backup" and not s["ok"]]
    assert failed == [], f"backup recorded as a failed step: {failed}"
    assert any(s["name"] == "pre_update_backup" for s in receipt.get("skips", []))
    assert receipt["outcome"] == "success"


def test_locked_file_is_skipped_and_the_snapshot_still_captured(monkeypatch):
    home = get_clover_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    (home / ".env").write_text("A=1\n", encoding="utf-8")
    real_copy2 = shutil.copy2

    def copy2(src, dst, *a, **k):
        if str(src).endswith(".env"):
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real_copy2(src, dst, *a, **k)

    monkeypatch.setattr(backup.shutil, "copy2", copy2)
    snap_id = backup.create_quick_snapshot(label="pre-update", clover_home=home)
    assert snap_id is not None
    snap = backup._quick_snapshot_root(home) / snap_id
    assert (snap / "config.yaml").is_file()
    assert not (snap / ".env").exists()


def test_backup_helper_crash_never_stops_the_update(monkeypatch):
    def boom(**k):
        raise PermissionError(32, "locked")

    monkeypatch.setattr(backup, "create_quick_snapshot", boom)
    monkeypatch.setattr(update_cmd, "_resolve_pre_update_backup_mode", lambda args: "quick")
    assert update_cmd._run_pre_update_backup(_update_args()) is None
