"""Private uv policy regression from Startup run 40133470936.

Real-uv tests use the uv on PATH, a temporary venv and a local stdlib-only
editable backend. No Clover install, runtime replacement or network is used.
Run with both the observed uv 0.13.5 and an older supported uv on PATH.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest

from clover_cli.managed_uv import managed_python_env


@pytest.mark.parametrize("preference", ["only-managed", "managed", "system", "only-system"])
def test_private_policy_supersedes_inherited_preference_without_mutating_input(preference):
    base = {
        "UV_PYTHON_PREFERENCE": preference,
        "UV_OFFLINE": "1",
        "UV_NO_INDEX": "1",
        "UV_NATIVE_TLS": "true",
        "PATH": r"C:\Users\Test User\bin;C:\Windows\System32",
        "KEEP_ME": "yes",
    }
    original = dict(base)
    store = r"C:\Users\Test User\clover\.clover-runtime\python"
    env = managed_python_env(install_dir=Path(store), base_env=base)

    assert "UV_PYTHON_PREFERENCE" not in env
    assert env["UV_MANAGED_PYTHON"] == "1"
    assert env["UV_NO_CONFIG"] == "1"
    assert env["UV_PYTHON_INSTALL_DIR"] == store
    assert env["UV_PYTHON_INSTALL_BIN"] == "0"
    assert env["UV_PYTHON_INSTALL_REGISTRY"] == "0"
    assert {key: env[key] for key in base if key != "UV_PYTHON_PREFERENCE"} == {
        key: value for key, value in base.items() if key != "UV_PYTHON_PREFERENCE"
    }
    assert base == original


@pytest.fixture
def offline_install(tmp_path, monkeypatch):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("real uv CLI is not on PATH")
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    live = root / "venv"
    venv.EnvBuilder(with_pip=False).create(live)
    python = live / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    (live / "keep-existing-runtime").write_text("unchanged", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n'
        '[project]\nname = "uv-policy-probe"\nversion = "0.0.1"\n'
        '[project.optional-dependencies]\nall = []\n', encoding="utf-8",
    )
    (root / "uv_policy_probe.py").write_text('VALUE = "installed offline"\n', encoding="utf-8")
    # Real PEP 660 build without downloading setuptools or any dependency.
    (root / "backend.py").write_text(
        'from pathlib import Path\nimport zipfile\n'
        'def build_editable(wheel_directory, config_settings=None, metadata_directory=None):\n'
        '    name = "uv_policy_probe-0.0.1-py3-none-any.whl"\n'
        '    info = "uv_policy_probe-0.0.1.dist-info/"\n'
        '    with zipfile.ZipFile(Path(wheel_directory) / name, "w") as wheel:\n'
        '        wheel.writestr("uv_policy_probe.pth", str(Path(__file__).parent) + "\\n")\n'
        '        wheel.writestr(info + "METADATA", "Metadata-Version: 2.1\\nName: uv-policy-probe\\nVersion: 0.0.1\\n")\n'
        '        wheel.writestr(info + "WHEEL", "Wheel-Version: 1.0\\nGenerator: test\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n")\n'
        '        wheel.writestr(info + "RECORD", "")\n'
        '    return name\n', encoding="utf-8",
    )
    base = dict(os.environ)
    base.update({
        "UV_PYTHON_PREFERENCE": "only-managed",  # exact fixture input
        "UV_OFFLINE": "1",
        "UV_NO_INDEX": "1",
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "TMPDIR": str(tmp_path),
        "TMP": str(tmp_path),
        "TEMP": str(tmp_path),
    })
    # Exercise normal os.environ inheritance as used by all updater branches.
    monkeypatch.setenv("UV_PYTHON_PREFERENCE", base["UV_PYTHON_PREFERENCE"])
    env = managed_python_env(root, base_env=base)
    env["VIRTUAL_ENV"] = str(live)  # updater's existing-target contract
    return uv, root, live, python, env


def test_real_uv_rejects_frozen_conflicting_policy(offline_install):
    uv, root, live, python, env = offline_install
    frozen = {**env, "UV_PYTHON_PREFERENCE": "only-managed"}
    result = subprocess.run(
        [uv, "pip", "install", "-e", ".[all]"], cwd=root, env=frozen,
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 2
    assert "UV_MANAGED_PYTHON" in result.stderr
    assert "cannot be used with `--python-preference`" in result.stderr
    assert (live / "keep-existing-runtime").read_text() == "unchanged"


def test_real_updater_command_installs_offline_with_inherited_preference(
    offline_install, monkeypatch,
):
    from clover_cli import main

    uv, root, live, python, env = offline_install
    monkeypatch.setattr(main, "PROJECT_ROOT", root)
    commands = []
    original = main._run_install_with_heartbeat

    def record_real_install(cmd, **kwargs):
        commands.append(list(cmd))
        return original(cmd, **kwargs)  # real quarantine, heartbeat and uv process

    monkeypatch.setattr(main, "_run_install_with_heartbeat", record_real_install)
    before = subprocess.check_output(
        [str(python), "-I", "-c", "import sys,sqlite3;print(sys.version);print(sqlite3.sqlite_version)"],
        text=True,
    )
    main._install_python_dependencies_with_optional_fallback([uv, "pip"], env=env)
    assert commands == [[uv, "pip", "install", "-e", ".[all]"]]
    assert subprocess.check_output(
        [str(python), "-I", "-c", "import uv_policy_probe;print(uv_policy_probe.VALUE)"],
        text=True,
    ).strip() == "installed offline"
    assert subprocess.check_output(
        [str(python), "-I", "-c", "import sys,sqlite3;print(sys.version);print(sqlite3.sqlite_version)"],
        text=True,
    ) == before
    assert (live / "keep-existing-runtime").read_text() == "unchanged"
    assert env["VIRTUAL_ENV"] == str(live)
    assert env["UV_MANAGED_PYTHON"] == "1"
    assert os.environ["UV_PYTHON_PREFERENCE"] == "only-managed"
