"""install.sh must create $CLOVER_HOME and its subdirs owner-only (0700).

``copy_config_templates()`` creates ``$CLOVER_HOME``'s subdirectories
(``sessions``, ``memories``, ``pairing``, ``logs``, ...) with ``mkdir -p``,
which inherits the process umask. Under a permissive umask (e.g. 022) these
directories — which hold conversation history, agent memories, and pairing
tokens — end up group/world readable. Only ``.env`` was ever chmod'd.

These tests run the real ``copy_config_templates()`` function (extracted
from install.sh) against a temp ``$CLOVER_HOME``, following the pattern in
``tests/test_install_sh_symlink_stomp.py``.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

SENSITIVE_SUBDIRS = ("sessions", "memories", "pairing")
ALL_SUBDIRS = (
    "cron",
    "sessions",
    "logs",
    "pairing",
    "hooks",
    "image_cache",
    "audio_cache",
    "memories",
    "skills",
)


def _extract_function(name: str) -> str:
    result = subprocess.run(
        ["sed", "-n", f"/^{name}()/,/^}}/p", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip(), f"could not extract {name}() from install.sh"
    return result.stdout


def _run_copy_config_templates(clover_home: Path, install_dir: Path) -> subprocess.CompletedProcess:
    script = f"""
set -e
CLOVER_HOME={clover_home!s}
INSTALL_DIR={install_dir!s}
NO_SKILLS=true
log_info() {{ echo "INFO: $*"; }}
log_success() {{ echo "SUCCESS: $*"; }}
log_warn() {{ echo "WARN: $*"; }}
log_error() {{ echo "ERROR: $*"; }}
configure_browser_env_from_system_browser() {{ :; }}
{_extract_function("copy_config_templates")}
copy_config_templates
"""
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits; install.sh does not run on Windows")
class TestCopyConfigTemplatesPermissions:
    def test_fresh_install_creates_owner_only_dirs(self, tmp_path):
        clover_home = tmp_path / ".clover"
        install_dir = tmp_path / "install"
        install_dir.mkdir()

        old_umask = os.umask(0o022)
        try:
            result = _run_copy_config_templates(clover_home, install_dir)
        finally:
            os.umask(old_umask)

        assert result.returncode == 0, result.stderr
        assert _mode(clover_home) == 0o700
        for name in ALL_SUBDIRS:
            assert _mode(clover_home / name) == 0o700, name

    def test_preexisting_lax_sensitive_dirs_are_tightened_and_logged(self, tmp_path):
        clover_home = tmp_path / ".clover"
        install_dir = tmp_path / "install"
        install_dir.mkdir()
        clover_home.mkdir(mode=0o755)
        for name in SENSITIVE_SUBDIRS:
            d = clover_home / name
            d.mkdir(mode=0o755)
            os.chmod(d, 0o755)
        assert _mode(clover_home / "sessions") == 0o755

        result = _run_copy_config_templates(clover_home, install_dir)

        assert result.returncode == 0, result.stderr
        for name in SENSITIVE_SUBDIRS:
            assert _mode(clover_home / name) == 0o700, name
            assert f"Tightened {clover_home}/{name}" in result.stdout, result.stdout

    def test_already_owner_only_sensitive_dirs_are_not_logged(self, tmp_path):
        clover_home = tmp_path / ".clover"
        install_dir = tmp_path / "install"
        install_dir.mkdir()
        clover_home.mkdir(mode=0o700)
        for name in SENSITIVE_SUBDIRS:
            d = clover_home / name
            d.mkdir(mode=0o700)

        result = _run_copy_config_templates(clover_home, install_dir)

        assert result.returncode == 0, result.stderr
        assert "Tightened" not in result.stdout, result.stdout
