"""Regression coverage for a clear repair message when the launcher's venv
interpreter is missing or broken.

Before this fix, the generated ``clover`` / ``clover-c1`` launcher scripts
did a bare ``exec "$CLOVER_BIN" ...``. If the venv interpreter later goes
missing (corrupted venv, partial reinstall, deleted directory), that ``exec``
fails with a raw, confusing shell error and the user cannot even run
``clover doctor`` to self-repair. The launcher must instead print one clear
line naming the exact repair command.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _setup_path_function() -> str:
    match = re.search(
        r"^setup_path\(\) \{\n.*?^}\n",
        INSTALL_SH.read_text(encoding="utf-8"),
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "setup_path function not found in scripts/install.sh"
    return match.group(0)


def _run_setup_path(tmp_path: Path, *, install_dir: Path, command_dir: Path) -> None:
    harness = "\n".join(
        [
            "set -e",
            'get_command_link_dir() { printf "%s" "$COMMAND_LINK_DIR"; }',
            'get_command_link_display_dir() { printf "%s" "$COMMAND_LINK_DIR"; }',
            "log_info() { :; }",
            "log_success() { :; }",
            "log_warn() { :; }",
            _setup_path_function(),
            "setup_path",
        ]
    )
    env = os.environ | {
        "USE_VENV": "true",
        "INSTALL_DIR": str(install_dir),
        "DISTRO": "linux",
        "COMMAND_LINK_DIR": str(command_dir),
    }
    subprocess.run(["/bin/bash", "-c", harness], env=env, check=True)


def _prepare_install_dir(tmp_path: Path) -> Path:
    install_dir = tmp_path / "install"
    venv_bin = install_dir / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    _make_executable(venv_bin / "python", "#!/bin/sh\nexit 0\n")
    (install_dir / "clover").write_text("# source entrypoint\n", encoding="utf-8")
    (install_dir / "run_agent.py").write_text("# agent entrypoint\n", encoding="utf-8")
    return install_dir


def test_clover_launcher_reports_repair_command_when_interpreter_missing(
    tmp_path: Path,
) -> None:
    install_dir = _prepare_install_dir(tmp_path)
    command_dir = tmp_path / "command"
    _run_setup_path(tmp_path, install_dir=install_dir, command_dir=command_dir)

    # Simulate a broken/missing venv AFTER install: the interpreter the
    # launcher was baked with is gone.
    (install_dir / "venv" / "bin" / "python").unlink()

    completed = subprocess.run(
        [str(command_dir / "clover"), "--version"],
        text=True,
        capture_output=True,
    )

    assert completed.returncode != 0
    combined = completed.stdout + completed.stderr
    lines = [line for line in combined.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one clear line, got: {lines!r}"
    assert "install.sh" in lines[0]
    assert str(install_dir / "venv" / "bin" / "python") in lines[0]


def test_clover_c1_launcher_reports_repair_command_when_interpreter_missing(
    tmp_path: Path,
) -> None:
    install_dir = _prepare_install_dir(tmp_path)
    command_dir = tmp_path / "command"
    _run_setup_path(tmp_path, install_dir=install_dir, command_dir=command_dir)

    (install_dir / "venv" / "bin" / "python").unlink()

    completed = subprocess.run(
        [str(command_dir / "clover-c1"), "--version"],
        text=True,
        capture_output=True,
    )

    assert completed.returncode != 0
    combined = completed.stdout + completed.stderr
    lines = [line for line in combined.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one clear line, got: {lines!r}"
    assert "install.sh" in lines[0]


def test_clover_launcher_still_execs_normally_when_interpreter_is_healthy(
    tmp_path: Path,
) -> None:
    install_dir = _prepare_install_dir(tmp_path)
    command_dir = tmp_path / "command"
    _run_setup_path(tmp_path, install_dir=install_dir, command_dir=command_dir)

    completed = subprocess.run(
        [str(command_dir / "clover"), "--version"],
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr
