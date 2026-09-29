"""Emilio's Windows 11 forensic report (2026-09-29), updater side.

D1/D2: the fleet-restart catch-up was armed by a receipt of a run that never
pulled (a refused Telegram /update), and by the ``sys.exit(0)`` stop_reason
the command boundary writes on every exit. Every later no-op update then ran
the catch-up, which on Windows killed the gateway it had just started.

D3: ``_purge_stale_clover_modules()`` evicted ``clover_cli.update_receipt``,
losing the open receipt, so the stale receipt was never replaced.

D4/D9: the restart watcher stood down, and "Gateway started" was printed,
for gateways that never came up.

D5 (updater half): the pause wrote a planned-stop marker that raced the
socket pause, and waited only the 0 s drain budget.
"""

from __future__ import annotations

import importlib
import json

from clover_cli import main as clover_main
from clover_cli import update_cmd
from clover_constants import get_clover_home
from tests.clover_cli.test_update_fleet_restart_pending import (
    _make_up_to_date_side_effect,
    _patch_update_deps,
    _update_args,
)

OLD = "69a75bb87740dda7461825f864d678c72fce5eec"
HEAD = "c7ec2d0e9a5c87b833542f1ddb5fe54a16e239c0"

# E4: Emilio's latest.json, the receipt that keeps the catch-up armed.
EMILIO_LATEST = {
    "outcome": "refused",
    "exit_code": 2,
    "stop_reason": "sys.exit(2)",
    "pre_update": {"sha": OLD, "short_sha": "69a75bb8", "version": "1.0.0", "source": "git"},
    "post_update": {"sha": OLD, "short_sha": "69a75bb8", "version": "1.0.0", "source": "git"},
    "gateway_restart": {},
    "fleet": [],
    "steps": [{"name": "pre_update_backup", "ok": False, "detail": "disabled or failed",
               "at": "2026-09-29T14:26:15.144475+00:00"}],
    "plan": {
        "runtimes": [{"kind": "gateway", "profile": "default", "pid": 15136, "supervisor": "manual",
                      "code_sha": OLD, "code_version": "1.0.0", "restart_via": "manual", "detail": {}}],
        "install_method": "git",
    },
}


def _write_latest(receipt: dict) -> None:
    directory = get_clover_home() / "logs" / "update_receipts"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "latest.json").write_text(json.dumps(receipt), encoding="utf-8")


def test_emilios_refused_receipt_does_not_arm_the_catch_up(monkeypatch):
    monkeypatch.setattr(update_cmd, "_current_checkout_sha", lambda: HEAD)
    _write_latest(EMILIO_LATEST)
    assert not update_cmd._fleet_restart_pending_marker_path().exists()
    assert update_cmd._receipt_reports_stale_runtime() is False
    assert update_cmd._pending_fleet_restart_needed() is False


def test_no_pull_update_with_emilios_receipt_runs_no_catch_up(monkeypatch, tmp_path, capsys):
    """The exact no-op update that took his gateway down for 21 minutes."""
    _patch_update_deps(monkeypatch, tmp_path, _make_up_to_date_side_effect(HEAD))
    monkeypatch.setattr(update_cmd, "_current_checkout_sha", lambda: HEAD)
    _write_latest(EMILIO_LATEST)
    ran = []
    monkeypatch.setattr(update_cmd, "_run_pending_fleet_restart", lambda: ran.append(1) or True)

    clover_main.cmd_update(_update_args())

    assert ran == []
    assert "did not restart running gateways" not in capsys.readouterr().out


def test_clean_boundary_exit_is_not_an_unfinished_update(monkeypatch):
    monkeypatch.setattr(update_cmd, "_current_checkout_sha", lambda: HEAD)
    success = dict(EMILIO_LATEST, outcome="success", exit_code=0, stop_reason="sys.exit(0)",
                   post_update={"sha": "4" * 40})
    _write_latest(success)
    assert update_cmd._receipt_reports_stale_runtime() is False


def test_live_gateway_on_head_beats_any_receipt(monkeypatch):
    import gateway.status as status

    monkeypatch.setattr(update_cmd, "_current_checkout_sha", lambda: HEAD)
    interrupted = dict(EMILIO_LATEST, outcome="failed", exit_code=1,
                       stop_reason="KeyboardInterrupt: ", post_update={})
    _write_latest(interrupted)
    assert update_cmd._receipt_reports_stale_runtime() is True  # genuinely owed
    monkeypatch.setattr(status, "get_running_pid", lambda *a, **k: 4242)
    monkeypatch.setattr(status, "read_runtime_status", lambda *a, **k: {"pid": 4242, "code_sha": HEAD})
    assert update_cmd._receipt_reports_stale_runtime() is False


def test_open_receipt_survives_the_stale_module_purge():
    """E9, as a unit test."""
    r1 = importlib.import_module("clover_cli.update_receipt")
    sentinel = object()
    saved = r1._current
    r1._current = sentinel
    try:
        update_cmd._purge_stale_clover_modules()
        r2 = importlib.import_module("clover_cli.update_receipt")
        assert "clover_cli.update_receipt" in update_cmd._STALE_PURGE_PROTECTED
        assert r2 is r1
        assert r2._current is sentinel
    finally:
        r1._current = saved


def test_a_lost_receipt_is_reported_loudly(monkeypatch, capsys):
    from clover_cli import update_receipt

    monkeypatch.setattr(update_receipt, "_began_in_process", True)
    monkeypatch.setattr(update_receipt, "_written_in_process", False)
    assert update_receipt.warn_if_receipt_lost() is True
    assert "did not record its receipt" in capsys.readouterr().err
    monkeypatch.setattr(update_receipt, "_written_in_process", True)
    assert update_receipt.warn_if_receipt_lost() is False


def test_restart_watcher_stays_armed_until_a_gateway_is_confirmed(monkeypatch):
    """D4: a no-op update must not stand the watcher down over a dead gateway."""
    import gateway.status as status
    from clover_cli import update_restart_watcher as urw

    beacon = urw.write_beacon(["python", "-m", "clover_cli.main", "gateway", "run"])
    monkeypatch.setattr(status, "get_running_pid", lambda *a, **k: None)
    assert update_cmd._clear_restart_beacon_if_gateway_up() is False
    assert beacon.exists(), "watcher stood down while no gateway was running"

    monkeypatch.setattr(status, "get_running_pid", lambda *a, **k: 4242)
    assert update_cmd._clear_restart_beacon_if_gateway_up() is True
    assert not beacon.exists()


def test_windows_ready_check_needs_a_claimed_gateway_not_a_process_match(monkeypatch):
    """D4/D9: "Gateway started" was printed for gateways that never ran."""
    import clover_cli.gateway as gw
    import gateway.status as status
    from clover_cli import gateway_windows

    monkeypatch.setattr(gw, "find_gateway_pids", lambda *a, **k: [15744, 24024])  # spawned, not up
    monkeypatch.setattr(status, "get_running_pid", lambda *a, **k: None)
    assert gateway_windows._wait_for_gateway_ready(timeout_s=0) == []
    monkeypatch.setattr(status, "get_running_pid", lambda *a, **k: 24024)
    assert gateway_windows._wait_for_gateway_ready(timeout_s=0) == [24024]


def test_handoff_parent_never_cold_starts_a_gateway(monkeypatch):
    """D9: the clover.exe parent cold-started a gateway that the hand-off
    child then force-stopped; the child owns the only (re)start."""
    token = {"resume_needed": True, "profiles": {}, "unmapped_pids": [], "unmapped": [],
             "cold_start_if_installed": True}
    started = []
    monkeypatch.setattr(clover_main, "_cold_start_windows_gateway_after_update",
                        lambda: started.append(1) or True)
    assert update_cmd._hand_off_windows_gateway_resume(token) is True
    update_cmd._resume_windows_gateways_after_update(token)  # the parent's atexit resume
    assert started == []
