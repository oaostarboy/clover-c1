"""Fail-closed systemd ownership checks for same-venv Python aliases."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from clover_cli import update_cmd


pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="systemd ExecStart serialization and venv bin paths are Linux-specific",
)


def _unit(python_path, argv_path=None, *, unit="clover-gateway.service", home="/home/example/.clover", venv=None, command="-m clover_cli.main gateway run"):
    argv_path = argv_path or python_path
    venv = venv if venv is not None else str(Path(python_path).parent.parent)
    return (
        f"Id={unit}\n"
        f"WorkingDirectory={home}\n"
        f"ExecStart={{ path={python_path} ; argv[]={argv_path} {command} ; ignore_errors=no }}\n"
        f"Environment=CLOVER_HOME={home} VIRTUAL_ENV={venv}\n"
        "FragmentPath=/tmp/clover-gateway.service\n"
    )


def _probe(monkeypatch, text, expected):
    monkeypatch.setattr(update_cmd.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=text))
    monkeypatch.setattr(update_cmd, "get_clover_home", lambda: Path("/home/example/.clover"))
    return update_cmd._systemd_unit_owned_by_install(["systemctl", "--user"], "clover-gateway.service", str(expected))


def _venv(tmp_path, name):
    env = tmp_path / name
    bin_dir = env / "bin"
    bin_dir.mkdir(parents=True)
    (env / "pyvenv.cfg").write_text("home = /usr\n")
    real = tmp_path / f"base-python-{name}"
    real.write_text("interpreter fixture\n")
    return env, bin_dir, real


def _symlink_or_skip(link, target):
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink fixtures unavailable: {exc}")


@pytest.mark.parametrize(("unit_alias", "expected_name"), [("python", "python3"), ("python3", "python"), ("python3.11", "python3"), ("python3", "python3.11")])
def test_same_venv_python_aliases_are_owned_on_real_filesystem(monkeypatch, tmp_path, unit_alias, expected_name):
    env, bin_dir, target = _venv(tmp_path, "venv")
    expected = bin_dir / expected_name
    unit_path = bin_dir / unit_alias
    _symlink_or_skip(expected, target)
    if unit_path != expected:
        _symlink_or_skip(unit_path, target)
    assert expected.parent == unit_path.parent
    assert expected.samefile(unit_path)
    assert _probe(monkeypatch, _unit(str(unit_path)), expected) is True


def test_exact_lexical_match_keeps_working_without_existing_interpreter(monkeypatch):
    path = "/home/example/agents/clover-c1/venv/bin/python"
    assert _probe(monkeypatch, _unit(path), path) is True


def test_distinct_venvs_sharing_base_python_are_rejected(monkeypatch, tmp_path):
    _, own_bin, target = _venv(tmp_path, "own")
    _, other_bin, _ = _venv(tmp_path, "other")
    expected, unit_path = own_bin / "python3", other_bin / "python"
    _symlink_or_skip(expected, target)
    _symlink_or_skip(unit_path, target)
    assert expected.samefile(unit_path)
    assert _probe(monkeypatch, _unit(str(unit_path)), expected) is False


@pytest.mark.parametrize("case", ["different-target", "missing-alias", "non-python", "system-bin", "non-ascii-version", "cfg-directory", "dangling-alias"])
def test_unsafe_or_incomplete_aliases_are_rejected(monkeypatch, tmp_path, case):
    env, bin_dir, target = _venv(tmp_path, "venv")
    expected = bin_dir / "python3"
    _symlink_or_skip(expected, target)
    if case == "different-target":
        unit_path = bin_dir / "python"
        other = tmp_path / "different-python"
        other.write_text("other interpreter\n")
        _symlink_or_skip(unit_path, other)
    elif case == "missing-alias":
        unit_path = bin_dir / "python"
    elif case == "non-python":
        unit_path = bin_dir / "python-helper"
        _symlink_or_skip(unit_path, target)
    elif case == "non-ascii-version":
        unit_path = bin_dir / "python3.١١"
        _symlink_or_skip(unit_path, target)
    elif case == "cfg-directory":
        marker = env / "pyvenv.cfg"
        marker.unlink()
        marker.mkdir()
        unit_path = bin_dir / "python"
        _symlink_or_skip(unit_path, target)
    elif case == "dangling-alias":
        unit_path = bin_dir / "python"
        _symlink_or_skip(unit_path, tmp_path / "nonexistent-python")
    else:
        unit_path = Path("/usr/bin/python3")
    assert _probe(monkeypatch, _unit(str(unit_path)), expected) is False


def test_python_alias_without_pyvenv_metadata_is_rejected(monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    target = tmp_path / "base-python"
    target.write_text("interpreter fixture\n")
    expected, unit_path = bin_dir / "python3", bin_dir / "python"
    _symlink_or_skip(expected, target)
    _symlink_or_skip(unit_path, target)
    assert _probe(monkeypatch, _unit(str(unit_path)), expected) is False


def test_execstart_path_and_argv_must_match_lexically(monkeypatch, tmp_path):
    _, bin_dir, target = _venv(tmp_path, "venv")
    expected = bin_dir / "python3"
    alias = bin_dir / "python"
    _symlink_or_skip(expected, target)
    _symlink_or_skip(alias, target)
    assert _probe(monkeypatch, _unit(str(alias), str(expected)), expected) is False


@pytest.mark.parametrize("metadata", ["wrong-home", "wrong-venv"])
def test_existing_home_and_virtual_env_checks_still_reject(monkeypatch, tmp_path, metadata):
    _, bin_dir, target = _venv(tmp_path, "venv")
    expected, alias = bin_dir / "python3", bin_dir / "python"
    _symlink_or_skip(expected, target)
    _symlink_or_skip(alias, target)
    if metadata == "wrong-home":
        text = _unit(str(alias), home="/home/example/.other")
    else:
        text = _unit(str(alias), venv="/other/venv")
    assert _probe(monkeypatch, text, expected) is False


def test_gateway_discovery_selects_own_alias_unit_but_excludes_foreign(monkeypatch, tmp_path):
    _, own_bin, target = _venv(tmp_path, "own")
    _, foreign_bin, _ = _venv(tmp_path, "foreign")
    expected, own_alias, foreign_alias = own_bin / "python3", own_bin / "python", foreign_bin / "python"
    _symlink_or_skip(expected, target)
    _symlink_or_skip(own_alias, target)
    _symlink_or_skip(foreign_alias, target)
    units = {
        "clover-gateway-own.service": _unit(str(own_alias), unit="clover-gateway-own.service", home="/home/example/.clover"),
        "clover-gateway-foreign.service": _unit(str(foreign_alias), unit="clover-gateway-foreign.service", home="/home/example/.clover/profiles/foreign"),
    }
    monkeypatch.setattr(update_cmd, "get_clover_home", lambda: Path("/home/example/.clover"))
    monkeypatch.setattr(update_cmd.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=units[cmd[cmd.index("show") + 1]]))
    found = []
    update_cmd._for_each_systemd_gateway_unit(
        "".join(f"{name} loaded active running\n" for name in units),
        process_unit=found.append, on_unit_timeout=lambda *_: None,
        scope_cmd=["systemctl", "--user"], interpreter=str(expected),
    )
    assert found == ["clover-gateway-own"]
