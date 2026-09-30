"""Cross-install updater service ownership regressions (Linux systemd)."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from clover_cli import update_cmd


CLOVER = "/home/example/agents/clover-c1/venv/bin/python"
OCTAVIA = "/home/example/agents/tentacle-one/venv/bin/python"


def properties(unit, python, home, *, marker="gateway run", include_environment=True):
    environment = f"Environment=CLOVER_HOME={home} VIRTUAL_ENV={Path(python).parent.parent}\n" if include_environment else "Environment=\n"
    return (f"Id={unit}\nWorkingDirectory={home}\n"
            f"ExecStart={{ path={python} ; argv[]={python} -m clover_cli.main {marker} ; ignore_errors=no }}\n"
            f"{environment}FragmentPath=/tmp/{unit}\n")


@pytest.mark.parametrize("interpreter,expected", [(CLOVER, {"clover-gateway", "clover-gateway-work"}), (OCTAVIA, {"tentacle-one-gateway"})])
def test_discovery_only_selects_same_install_including_custom_name_and_profile(monkeypatch, interpreter, expected):
    monkeypatch.setattr(update_cmd, "get_clover_home", lambda: Path("/home/example/.clover" if interpreter == CLOVER else "/home/example/.clover-tentacle"))
    units = {
        "clover-gateway.service": properties("clover-gateway.service", CLOVER, "/home/example/.clover", include_environment=False),
        "clover-gateway-work.service": properties("clover-gateway-work.service", CLOVER, "/home/example/.clover/profiles/work"),
        "tentacle-one-gateway.service": properties("tentacle-one-gateway.service", OCTAVIA, "/home/example/.clover-tentacle"),
        "clover-gateway-other.service": properties("clover-gateway-other.service", "/opt/other/venv/bin/python", "/home/example/.other"),
        "unrelated.service": properties("unrelated.service", CLOVER, "/tmp/other", marker="maintenance run"),
        "unknown.service": "Id=unknown.service\nWorkingDirectory=/tmp\nExecStart=\n",
    }
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        if "list-units" in cmd:
            return SimpleNamespace(returncode=0, stdout="".join(f"{u} loaded active running\n" for u in units))
        if "show" in cmd:
            return SimpleNamespace(returncode=0, stdout=units[cmd[cmd.index("show") + 1]])
        raise AssertionError(f"unexpected command: {cmd}")
    monkeypatch.setattr(update_cmd.subprocess, "run", run)
    found = []
    for scope in (["systemctl", "--user"], ["systemctl"]):
        update_cmd._for_each_systemd_gateway_unit(
            run(scope + ["list-units", "*.service"]).stdout,
            process_unit=found.append,
            on_unit_timeout=lambda *_: None,
            scope_cmd=scope,
            interpreter=interpreter,
        )
    assert set(found) == expected
    assert len(found) == len(expected) * 2
    assert any("show" in c and "tentacle-one-gateway.service" in c for c in calls)


def test_discovery_rejects_missing_or_conflicting_metadata(monkeypatch):
    units = {
        "missing.service": "Id=missing.service\nExecStart=\n",
        "conflict.service": properties("conflict.service", CLOVER, "/tmp/home").replace("VIRTUAL_ENV=/home/example/agents/clover-c1/venv", "VIRTUAL_ENV=/other/venv"),
    }
    monkeypatch.setattr(update_cmd.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=units[cmd[cmd.index("show") + 1]]))
    found = []
    update_cmd._for_each_systemd_gateway_unit(
        "".join(f"{u} loaded active running\n" for u in units),
        process_unit=found.append, on_unit_timeout=lambda *_: None,
        scope_cmd=["systemctl", "--user"], interpreter=CLOVER,
    )
    assert found == []


def test_shared_interpreter_requires_matching_clover_home(monkeypatch):
    monkeypatch.setattr(update_cmd, "get_clover_home", lambda: Path("/home/example/.clover"))
    foreign = properties("foreign.service", "/usr/bin/python3", "/home/example/.clover-tentacle", include_environment=False)
    own = properties("own.service", "/usr/bin/python3", "/home/example/.clover/profiles/work", include_environment=False)
    units = {"foreign.service": foreign, "own.service": own}
    monkeypatch.setattr(update_cmd.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=units[cmd[cmd.index("show") + 1]]))
    assert update_cmd._systemd_unit_owned_by_install(["systemctl"], "foreign.service", "/usr/bin/python3") is False
    assert update_cmd._systemd_unit_owned_by_install(["systemctl"], "own.service", "/usr/bin/python3") is True


def test_pending_restart_uses_owned_custom_unit(monkeypatch):
    monkeypatch.setattr(update_cmd, "get_clover_home", lambda: Path("/home/example/.clover-tentacle"))
    units = {
        "clover-gateway.service": properties("clover-gateway.service", CLOVER, "/home/example/.clover"),
        "tentacle-one-gateway.service": properties("tentacle-one-gateway.service", OCTAVIA, "/home/example/.clover-tentacle"),
    }
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        if "list-units" in cmd:
            return SimpleNamespace(returncode=0, stdout="" if "--user" not in cmd else "".join(f"{u} loaded active running\n" for u in units))
        if "show" in cmd:
            return SimpleNamespace(returncode=0, stdout=units[cmd[cmd.index("show") + 1]])
        return SimpleNamespace(returncode=0, stdout="")
    monkeypatch.setattr(update_cmd.subprocess, "run", run)
    monkeypatch.setattr(update_cmd.sys, "executable", OCTAVIA)
    failed = []
    update_cmd._restart_systemd_gateway_units_best_effort(failed)
    assert failed == []
    assert [c[-1] for c in calls if "restart" in c] == ["tentacle-one-gateway"]


def test_linux_pid_sweep_rejects_another_install(monkeypatch, tmp_path):
    monkeypatch.setattr(update_cmd.sys, "executable", OCTAVIA)
    own = tmp_path / "own"
    other = tmp_path / "other"
    own.write_bytes((OCTAVIA + "\0-m\0clover_cli.main\0gateway\0run\0").encode())
    other.write_bytes((CLOVER + "\0-m\0clover_cli.main\0gateway\0run\0").encode())
    monkeypatch.setattr(update_cmd, "_gateway_pid_cmdline_path", lambda pid: own if pid == 101 else other)
    assert update_cmd._own_install_gateway_pids([101, 202]) == [101]


def test_custom_service_pid_is_protected_from_manual_sweep(monkeypatch):
    monkeypatch.setattr(update_cmd, "get_clover_home", lambda: Path("/home/example/.clover-tentacle"))
    def run(cmd, **kw):
        if "list-units" in cmd:
            return SimpleNamespace(returncode=0, stdout="tentacle-one-gateway.service loaded active running\n" if "--user" in cmd else "")
        if "--property=MainPID" in cmd:
            return SimpleNamespace(returncode=0, stdout="1234\n")
        return SimpleNamespace(returncode=0, stdout=properties("tentacle-one-gateway.service", OCTAVIA, "/home/example/.clover-tentacle"))
    monkeypatch.setattr(update_cmd.subprocess, "run", run)
    monkeypatch.setattr(update_cmd.sys, "executable", OCTAVIA)
    assert update_cmd._owned_systemd_service_pids() == {1234}
