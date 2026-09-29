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
