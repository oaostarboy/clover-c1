"""The updater's own-gateway PID sweep must use the same-venv alias rule.

A systemd unit may start ``venv/bin/python`` while the updater runs as
``venv/bin/python3`` (or ``python3.13``); all three are one install. A different
venv that shares the same base interpreter is not.
"""
import sys
from pathlib import Path

import pytest

from clover_cli import update_cmd


def _venv(tmp_path, name, base):
    """Real venv-shaped dir: bin/python3.13 -> base, python/python3 -> python3.13."""
    env = tmp_path / name
    bin_dir = env / "bin"
    bin_dir.mkdir(parents=True)
    (env / "pyvenv.cfg").write_text("home = /usr\n")
    try:
        (bin_dir / "python3.13").symlink_to(base)
        (bin_dir / "python").symlink_to("python3.13")
        (bin_dir / "python3").symlink_to("python3.13")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink fixtures unavailable: {exc}")
    return bin_dir


@pytest.fixture
def base_python(tmp_path):
    base = tmp_path / "base-python3.13"
    base.write_text("interpreter fixture\n")
    return base


@pytest.fixture
def cmdline_reader(monkeypatch, tmp_path):
    """Patch only the pid -> /proc cmdline reader; return a pid registrar."""
    files = {}

    def register(pid, argv0):
        path = tmp_path / f"cmdline-{pid}"
        path.write_bytes(("\0".join([str(argv0), "-m", "clover_cli.main", "gateway", "run"]) + "\0").encode())
        files[pid] = path

    monkeypatch.setattr(update_cmd, "_gateway_pid_cmdline_path", lambda pid: files[pid])
    return register


@pytest.mark.linux_only
@pytest.mark.parametrize("running", ["python", "python3", "python3.13"])
@pytest.mark.parametrize("updater", ["python", "python3", "python3.13"])
def test_same_venv_alias_gateway_is_selected(monkeypatch, tmp_path, base_python, cmdline_reader, running, updater):
    bin_dir = _venv(tmp_path, "venv", base_python)
    monkeypatch.setattr(update_cmd.sys, "executable", str(bin_dir / updater))
    cmdline_reader(101, bin_dir / running)
    assert update_cmd._own_install_gateway_pids([101]) == [101]


@pytest.mark.linux_only
def test_other_venv_sharing_base_python_is_not_selected(monkeypatch, tmp_path, base_python, cmdline_reader):
    own = _venv(tmp_path, "own", base_python)
    other = _venv(tmp_path, "other", base_python)
    assert (own / "python3").samefile(other / "python")
    monkeypatch.setattr(update_cmd.sys, "executable", str(own / "python3"))
    cmdline_reader(101, own / "python")
    cmdline_reader(202, other / "python")
    cmdline_reader(303, other / "python3")
    assert update_cmd._own_install_gateway_pids([101, 202, 303]) == [101]


@pytest.mark.linux_only
def test_same_dir_alias_resolving_to_a_different_interpreter_is_not_selected(monkeypatch, tmp_path, base_python, cmdline_reader):
    bin_dir = _venv(tmp_path, "venv", base_python)
    stray = tmp_path / "stray-python"
    stray.write_text("another interpreter\n")
    (bin_dir / "python3.12").symlink_to(stray)
    monkeypatch.setattr(update_cmd.sys, "executable", str(bin_dir / "python3"))
    cmdline_reader(101, bin_dir / "python3.12")
    assert update_cmd._own_install_gateway_pids([101]) == []


@pytest.mark.linux_only
def test_unreadable_cmdline_is_spared(monkeypatch, tmp_path, base_python):
    bin_dir = _venv(tmp_path, "venv", base_python)
    monkeypatch.setattr(update_cmd.sys, "executable", str(bin_dir / "python3"))
    monkeypatch.setattr(update_cmd, "_gateway_pid_cmdline_path", lambda pid: tmp_path / "gone")
    assert update_cmd._own_install_gateway_pids([101]) == []


@pytest.mark.linux_only
def test_pending_marker_sweep_finds_same_venv_alias_gateway(monkeypatch, tmp_path, base_python, cmdline_reader, capsys):
    """Octavia's 2026-10-03 /update: marker present, gateway runs ``python``, updater ``python3``."""
    from clover_cli import gateway

    bin_dir = _venv(tmp_path, "venv", base_python)
    monkeypatch.setattr(update_cmd.sys, "executable", str(bin_dir / "python3"))
    cmdline_reader(101, bin_dir / "python")

    update_cmd._write_fleet_restart_pending_marker(expected_sha="deadbeef")
    assert update_cmd._pending_fleet_restart_needed()

    restarted = []

    def restart_units(failed):
        restarted.append(True)

    monkeypatch.setattr(update_cmd._m(), "_purge_stale_clover_modules", lambda: None)
    monkeypatch.setattr(update_cmd, "_owned_systemd_service_pids", lambda: set())
    monkeypatch.setattr(update_cmd, "_restart_systemd_gateway_units_best_effort", restart_units)
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
    monkeypatch.setattr(gateway, "_wait_for_gateway_exit", lambda **kw: None)
    # The pre-restart gateway is gone once the restart ran.
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **kw: [] if restarted else [101])

    update_cmd._apply_pending_fleet_restart_catchup()

    out = capsys.readouterr().out
    assert restarted == [True]
    assert "No running gateways" not in out
    assert "Pending fleet restart completed." in out
    assert not update_cmd._fleet_restart_pending_marker_path().exists()


@pytest.mark.windows_only
def test_windows_sweep_passes_every_pid_through():
    assert update_cmd._own_install_gateway_pids([11, 22]) == [11, 22]


@pytest.mark.macos_only
def test_macos_sweep_passes_every_pid_through():
    assert update_cmd._own_install_gateway_pids([11, 22]) == [11, 22]
