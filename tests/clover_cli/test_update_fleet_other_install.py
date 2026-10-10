"""Fleet version check must not fail an update over ANOTHER install's gateway.

A profile home can be served by a gateway running from a different checkout /
venv (e.g. a dev test gateway under ``~/.clover/profiles/dev`` launched from a
separate worktree venv). The restart phase already skips it as not owned
(``_own_install_gateway_pids``: same-venv rule). The fleet check used to read
its older ``code_sha`` as STALE and exit 1. It is now an ``other_install`` row:
shown, never counted as a failure. A same-install stale gateway still fails.
"""

import json
import os
import sys
from pathlib import Path

import pytest

import clover_cli.update_receipt as ur
from clover_cli import update_cmd

pytestmark = pytest.mark.linux_only


def _venv(tmp_path, name):
    env = tmp_path / name
    bin_dir = env / "bin"
    bin_dir.mkdir(parents=True)
    (env / "pyvenv.cfg").write_text("home = /usr\n")
    (bin_dir / "python").write_text("interpreter fixture\n")
    return bin_dir / "python"


@pytest.fixture
def fleet_env(monkeypatch, tmp_path):
    """One live gateway (this test process's pid) serving the default home,
    whose /proc cmdline is redirected to a file the test controls."""
    home = tmp_path / ".clover"
    home.mkdir()
    monkeypatch.setattr(
        "clover_cli.build_info.get_code_identity",
        lambda refresh=False: {"sha": "HEADSHA", "version": "1.3.0"},
    )
    monkeypatch.setattr("clover_cli.profiles._get_default_clover_home", lambda: home)
    monkeypatch.setattr(
        "clover_cli.profiles._get_profiles_root", lambda: tmp_path / "no-profiles"
    )
    monkeypatch.setattr("gateway.control_socket.identify_gateway", lambda h, **k: None)
    own_python = _venv(tmp_path, "own-venv")
    monkeypatch.setattr(update_cmd.sys, "executable", str(own_python))
    cmdline = tmp_path / "cmdline"
    monkeypatch.setattr(update_cmd, "_gateway_pid_cmdline_path", lambda pid: cmdline)

    def run_gateway_from(python, code_sha):
        cmdline.write_bytes(
            ("\0".join([str(python), "-m", "clover_cli.main", "gateway", "run"]) + "\0").encode()
        )
        (home / "gateway_state.json").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "gateway_state": "running",
                    "code_sha": code_sha,
                    "kind": "clover-gateway",
                }
            ),
            encoding="utf-8",
        )

    return {"tmp": tmp_path, "own": own_python, "run": run_gateway_from}


def test_other_install_gateway_is_shown_not_failed(fleet_env, capsys):
    other_python = _venv(fleet_env["tmp"], "dev-venv")
    fleet_env["run"](other_python, "OLDSHA")

    fleet = ur.collect_fleet_versions(pre_restart_pids=[])
    assert [row["state"] for row in fleet] == ["other_install"]
    assert ur.print_fleet_version_matrix(fleet) is False
    out = capsys.readouterr().out
    assert "other install" in out
    assert "STALE" not in out


def test_same_install_stale_gateway_still_fails(fleet_env, capsys):
    fleet_env["run"](fleet_env["own"], "OLDSHA")

    fleet = ur.collect_fleet_versions(pre_restart_pids=[])
    assert [row["state"] for row in fleet] == ["stale"]
    assert ur.print_fleet_version_matrix(fleet) is True
    assert "STALE" in capsys.readouterr().out


def test_unreadable_ownership_keeps_stale_verdict(fleet_env, monkeypatch):
    """Unknown ownership is not proof of another install: stay fail-closed."""
    fleet_env["run"](fleet_env["own"], "OLDSHA")
    monkeypatch.setattr(
        update_cmd, "_gateway_pid_cmdline_path", lambda pid: fleet_env["tmp"] / "gone"
    )
    assert [row["state"] for row in ur.collect_fleet_versions()] == ["stale"]


def test_other_install_row_does_not_arm_the_catchup_restart(monkeypatch):
    """A receipt whose only skew is another install's gateway owes no restart."""
    receipt = {
        "outcome": "success",
        "pre_update": {"sha": "A"},
        "post_update": {"sha": "HEADSHA"},
        "fleet": [
            {"profile": "default", "pid": 1, "code_sha": "HEADSHA", "state": "current"},
            {"profile": "dev", "pid": 2, "code_sha": "OLDSHA", "state": "other_install"},
        ],
    }
    monkeypatch.setattr(ur, "read_latest_receipt", lambda: receipt)
    monkeypatch.setattr(update_cmd, "_live_gateway_serves", lambda sha: False)
    assert update_cmd._receipt_reports_stale_runtime("HEADSHA") is False


def test_plan_runtime_from_other_install_is_not_unaccounted(monkeypatch, tmp_path):
    from clover_cli.update_inventory import (
        RuntimeRecord,
        UpdatePlan,
        match_runtime_outcomes,
        report_unaccounted_runtimes,
    )

    own = _venv(tmp_path, "own-venv")
    other = _venv(tmp_path, "dev-venv")
    monkeypatch.setattr(update_cmd.sys, "executable", str(own))
    files = {}
    for pid, python in ((101, own), (202, other)):
        path = tmp_path / f"cmdline-{pid}"
        path.write_bytes(
            ("\0".join([str(python), "-m", "clover_cli.main", "gateway", "run"]) + "\0").encode()
        )
        files[pid] = path
    monkeypatch.setattr(update_cmd, "_gateway_pid_cmdline_path", lambda pid: files[pid])

    plan = UpdatePlan()
    plan.runtimes = [
        RuntimeRecord(kind="gateway", profile="default", pid=101, supervisor="systemd",
                      restart_via="systemd"),
        RuntimeRecord(kind="gateway", profile="dev", pid=202, supervisor="systemd",
                      restart_via="systemd"),
    ]
    outcomes = match_runtime_outcomes(
        plan,
        restarted_services=["clover-gateway"],
        relaunched_profiles=[],
        externally_supervised_profiles=[],
        killed_pids=set(),
        failed_units=[],
    )
    assert {o["profile"]: o["outcome"] for o in outcomes} == {
        "default": "restarted",
        "dev": "other_install",
    }
    assert report_unaccounted_runtimes(outcomes) is False


def test_same_venv_via_directory_symlink_still_reports_stale(fleet_env):
    """A lexical path mismatch (symlinked venv dir) is not proof of another install."""
    import clover_cli.update_receipt as receipt

    own = fleet_env["own"]
    alias = fleet_env["tmp"] / "same-install-alias"
    alias.symlink_to(own.parent.parent, target_is_directory=True)
    aliased_python = alias / "bin" / "python"
    assert os.path.samefile(aliased_python.parent.parent, own.parent.parent)
    fleet_env["run"](aliased_python, "OLDSHA")

    fleet = receipt.collect_fleet_versions(pre_restart_pids=[])

    assert [r["state"] for r in fleet] == ["stale"], fleet


def test_symlinked_alias_of_a_different_venv_is_still_other_install(fleet_env):
    import clover_cli.update_receipt as receipt

    dev_python = _venv(fleet_env["tmp"], "dev-venv")
    alias = fleet_env["tmp"] / "dev-alias"
    alias.symlink_to(dev_python.parent.parent, target_is_directory=True)
    fleet_env["run"](alias / "bin" / "python", "OLDSHA")

    fleet = receipt.collect_fleet_versions(pre_restart_pids=[])

    assert [r["state"] for r in fleet] == ["other_install"], fleet


def test_unreadable_interpreter_identity_is_unknown_not_other(fleet_env):
    """Ambiguity (interpreter missing on disk) must never be classed foreign."""
    ghost = fleet_env["tmp"] / "ghost-venv" / "bin" / "python"
    assert update_cmd._interpreter_identity_verdict(str(ghost), str(fleet_env["own"])) == "unknown"

