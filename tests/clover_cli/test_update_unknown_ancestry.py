"""Chat ``/update`` must never tree-kill its own gateway, even when ancestry is unknown.

Incident (Windows 11 guest, 2026-09-30, C1 at c7ec2d0): ``/update`` sent from
Telegram ran the updater as a descendant of the gateway it was pausing. The
gateway did not drain in time, so the updater force-stopped it with
``terminate_pid(force=True)``, which is ``taskkill /T``. That killed the
updater too, and the chat got "Update process ended without reporting a
result".

62ef460b fixed the common case: a PID found in ``_own_ancestor_pids()`` is
stopped alone with ``_force_stop_single_process``. But ``_own_ancestor_pids()``
returns an EMPTY set when ``psutil.Process().parents()`` raises (AccessDenied
walking into a SYSTEM/elevated parent, or a parent that exits mid-walk). An
empty set made every PID look "not ours", so the tree kill came back.

Contract pinned here: when ancestry cannot be read, every candidate is treated
as a possible ancestor and stopped as a single process. Both force-stop sites
in ``clover update`` follow it, and a failed single kill is logged.
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from clover_cli import main as cli_main


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / ".clover"
    h.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(h))
    monkeypatch.delenv("CLOVER_UPDATE_REEXEC", raising=False)
    monkeypatch.delenv("CLOVER_GATEWAY_AUTORELAUNCH", raising=False)
    return h


@pytest.fixture()
def ancestry_unreadable(monkeypatch):
    """``psutil.Process().parents()`` raises, as it does on a SYSTEM parent."""
    import psutil

    def _denied(self):
        raise psutil.AccessDenied(os.getpid())

    monkeypatch.setattr(psutil.Process, "parents", _denied)


def test_ancestry_reports_unknown_instead_of_empty(ancestry_unreadable):
    # None means "could not read"; an empty set would mean "no ancestors".
    assert cli_main._own_ancestor_pids() is None


def _pause_harness(monkeypatch, tmp_path, *, single_kill=None):
    import gateway.status as status_mod
    import clover_cli.gateway as gateway_mod

    profile_home = tmp_path / "profiles" / "default"
    profile_home.mkdir(parents=True)
    gateway = SimpleNamespace(profile="default", path=profile_home, pid=101)
    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda **_k: [101])
    monkeypatch.setattr(gateway_mod, "find_windows_gateway_services", lambda **_k: [])
    monkeypatch.setattr(gateway_mod, "find_profile_gateway_processes", lambda **_k: [gateway])
    monkeypatch.setattr(gateway_mod, "_get_restart_drain_timeout", lambda: 0.1)
    monkeypatch.setattr(gateway_mod, "_capture_gateway_argv", lambda pid: None)
    monkeypatch.setattr(cli_main, "_venv_launcher_ancestors", lambda pids: set())
    monkeypatch.setattr(cli_main, "_wait_for_windows_update_gateway_exit",
                        lambda pids, *, timeout: {101})  # did not drain
    tree_killed, single_killed = [], []
    monkeypatch.setattr(status_mod, "terminate_pid",
                        lambda pid, force=False: tree_killed.append(pid))

    def _single(pid):
        single_killed.append(pid)
        if single_kill is not None:
            raise single_kill

    monkeypatch.setattr(cli_main, "_force_stop_single_process", _single)
    return tree_killed, single_killed


def test_pause_never_tree_kills_when_ancestry_is_unknown(
    home, monkeypatch, tmp_path, ancestry_unreadable,
):
    """Site 1: ``_pause_windows_gateways_for_update`` drain survivors."""
    tree_killed, single_killed = _pause_harness(monkeypatch, tmp_path)

    cli_main._pause_windows_gateways_for_update()

    assert tree_killed == []
    assert single_killed == [101]


def test_pause_logs_a_single_kill_that_failed(home, monkeypatch, tmp_path, caplog):
    """A survivor that could not be stopped is logged, never silently passed."""
    monkeypatch.setattr(cli_main, "_own_ancestor_pids", lambda: {101})
    _pause_harness(monkeypatch, tmp_path, single_kill=PermissionError(101))

    with caplog.at_level(logging.WARNING, logger="clover_cli.update_cmd"):
        cli_main._pause_windows_gateways_for_update()

    assert any("101" in r.getMessage() for r in caplog.records
               if r.levelno >= logging.WARNING)


def _update_args(**overrides):
    defaults = dict(gateway=False, check=False, no_backup=True, backup=False,
                    yes=True, branch=None, force=False, force_venv=False)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_venv_guard_never_tree_kills_when_ancestry_is_unknown(home, ancestry_unreadable):
    """Site 2: the venv-holder guard's leftover-gateway stop in ``_cmd_update_impl``."""

    class _PastGuard(Exception):
        pass

    class _RootSentinel:
        def __truediv__(self, _other):
            raise _PastGuard

    holders = [(101, "pythonw.exe", "pythonw.exe -m clover_cli.main gateway run")]
    tree_killed, single_killed = [], []

    with patch.object(cli_main, "_is_windows", return_value=True), patch.object(
        cli_main, "_venv_scripts_dir", return_value=None
    ), patch.object(cli_main, "_run_pre_update_backup"), patch.object(
        cli_main, "_pause_windows_gateways_for_update", return_value=None
    ), patch.object(
        cli_main, "_resume_windows_gateways_after_update"
    ), patch.object(
        cli_main, "_detect_venv_python_processes", side_effect=[holders, []]
    ), patch.object(
        cli_main, "_leftover_pausable_gateway_pids", return_value=[101]
    ), patch.object(
        cli_main, "_force_stop_single_process", side_effect=single_killed.append
    ), patch(
        "gateway.status.terminate_pid",
        side_effect=lambda pid, force=False: tree_killed.append(pid),
    ), patch.object(
        cli_main, "PROJECT_ROOT", _RootSentinel()
    ), patch("time.sleep"):
        with pytest.raises(_PastGuard):
            cli_main._cmd_update_impl(_update_args(), gateway_mode=False)

    assert tree_killed == []
    assert single_killed == [101]
