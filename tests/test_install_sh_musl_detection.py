"""install.sh must detect musl libc (Alpine and similar) and warn/fail clearly.

uv's managed Python downloads (python-build-standalone) are built for glibc
and do not run on musl. Before this fix, install.sh had no musl detection at
all: `uv python install` would be attempted unconditionally and fail deep
inside with an opaque exec error instead of a clear, actionable message.

These exercise the real shell functions (extracted from install.sh) against
a temp PATH rather than asserting on the text of install.sh.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _extract_function(name: str) -> str:
    result = subprocess.run(
        ["sed", "-n", f"/^{name}()/,/^}}/p", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip(), f"could not extract {name}() from install.sh"
    return result.stdout


def run_is_musl(*, alpine_release_file=None, fake_ldd_output=None, path_extra=None):
    """Source is_musl() in isolation and return the subprocess result."""
    env_lines = []
    if alpine_release_file is not None:
        env_lines.append(f"_ALPINE_RELEASE_FILE={alpine_release_file!s}")
    path_prefix = f'PATH="{path_extra}:$PATH"\n' if path_extra else ""
    script = f"""
set -e
{path_prefix}
{chr(10).join(env_lines)}
{_extract_function("is_musl")}
if is_musl; then echo MUSL; else echo NOT_MUSL; fi
"""
    ldd_setup = ""
    return script


def _run_script(script: str, *, cwd=None):
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30, cwd=cwd
    )


def _write_fake_ldd(bin_dir: Path, output: str, exit_code: int = 1) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    ldd = bin_dir / "ldd"
    ldd.write_text(
        f"#!/bin/sh\ncat <<'EOF' >&2\n{output}\nEOF\nexit {exit_code}\n",
        encoding="utf-8",
    )
    ldd.chmod(0o755)


def test_alpine_release_file_present_detects_musl(tmp_path):
    marker = tmp_path / "alpine-release"
    marker.write_text("3.19.0\n", encoding="utf-8")

    script = f"""
set -e
_ALPINE_RELEASE_FILE={marker!s}
{_extract_function("is_musl")}
if is_musl; then echo MUSL; else echo NOT_MUSL; fi
"""
    result = _run_script(script)
    assert result.returncode == 0, result.stderr
    assert "MUSL" in result.stdout.splitlines()


def test_ldd_musl_banner_detects_musl_without_alpine_file(tmp_path):
    fake_bin = tmp_path / "fakebin"
    _write_fake_ldd(fake_bin, "musl libc (x86_64)\nVersion 1.2.4")
    missing_marker = tmp_path / "no-such-alpine-release"

    script = f"""
set -e
PATH="{fake_bin}:$PATH"
_ALPINE_RELEASE_FILE={missing_marker!s}
{_extract_function("is_musl")}
if is_musl; then echo MUSL; else echo NOT_MUSL; fi
"""
    result = _run_script(script)
    assert result.returncode == 0, result.stderr
    assert "MUSL" in result.stdout.splitlines()


def test_glibc_ldd_is_not_musl(tmp_path):
    fake_bin = tmp_path / "fakebin"
    _write_fake_ldd(fake_bin, "ldd (GNU libc) 2.39", exit_code=0)
    missing_marker = tmp_path / "no-such-alpine-release"

    script = f"""
set -e
PATH="{fake_bin}:$PATH"
_ALPINE_RELEASE_FILE={missing_marker!s}
{_extract_function("is_musl")}
if is_musl; then echo MUSL; else echo NOT_MUSL; fi
"""
    result = _run_script(script)
    assert result.returncode == 0, result.stderr
    assert "NOT_MUSL" in result.stdout.splitlines()


def test_no_ldd_and_no_alpine_file_is_not_musl(tmp_path):
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    missing_marker = tmp_path / "no-such-alpine-release"

    script = f"""
set -e
PATH="{empty_bin}"
_ALPINE_RELEASE_FILE={missing_marker!s}
{_extract_function("is_musl")}
if is_musl; then echo MUSL; else echo NOT_MUSL; fi
"""
    result = _run_script(script)
    assert result.returncode == 0, result.stderr
    assert "NOT_MUSL" in result.stdout.splitlines()


def test_check_python_exits_clearly_on_musl_without_calling_uv_download(tmp_path):
    """check_python() must fail fast with manual steps, never invoke uv's
    doomed glibc-only Python download, when musl was detected."""
    fake_uv = tmp_path / "fake-uv.sh"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "python" ] && [ "$2" = "find" ]; then exit 1; fi\n'
        'if [ "$1" = "python" ] && [ "$2" = "install" ]; then\n'
        "  echo 'UV_DOWNLOAD_ATTEMPTED' >&2\n"
        "  exit 1\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    script = f"""
set -e
DISTRO=alpine
IS_MUSL=true
PYTHON_VERSION="3.11"
UV_CMD={fake_uv!s}
log_info() {{ echo "INFO: $*"; }}
log_success() {{ echo "SUCCESS: $*"; }}
log_error() {{ echo "ERROR: $*"; }}
log_warn() {{ echo "WARN: $*"; }}
{_extract_function("check_python")}
check_python
"""
    result = _run_script(script)
    assert result.returncode != 0, "must exit non-zero when Python is unavailable on musl"
    assert "UV_DOWNLOAD_ATTEMPTED" not in result.stderr, (
        "must not attempt uv's glibc-only Python download on musl"
    )
    assert "apk add" in result.stdout, "must give the apk manual-install step"


def test_check_python_still_uses_uv_when_not_musl(tmp_path):
    """Sanity check: the musl gate must not fire on ordinary (non-musl) systems."""
    fake_python = tmp_path / "fake-python3"
    fake_python.write_text(
        '#!/bin/sh\necho "Python 3.11.9"\n', encoding="utf-8"
    )
    fake_python.chmod(0o755)

    # check_python()'s success path calls `uv python find` a second time
    # *after* `uv python install` succeeds, to resolve the freshly installed
    # interpreter's path. A marker file distinguishes that second `find`
    # (which must now succeed) from the first, pre-install `find` (which
    # must fail, so the real function takes the "not found -> install"
    # branch it's meant to exercise).
    installed_marker = tmp_path / ".installed"
    fake_uv = tmp_path / "fake-uv.sh"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "python" ] && [ "$2" = "find" ]; then\n'
        f'  if [ -f "{installed_marker}" ]; then echo "{fake_python}"; exit 0; fi\n'
        "  exit 1\n"
        "fi\n"
        'if [ "$1" = "python" ] && [ "$2" = "install" ]; then\n'
        "  echo 'UV_DOWNLOAD_ATTEMPTED' >&2\n"
        f'  touch "{installed_marker}"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    script = f"""
set -e
DISTRO=debian
IS_MUSL=false
PYTHON_VERSION="3.11"
UV_CMD={fake_uv!s}
log_info() {{ echo "INFO: $*"; }}
log_success() {{ echo "SUCCESS: $*"; }}
log_error() {{ echo "ERROR: $*"; }}
log_warn() {{ echo "WARN: $*"; }}
{_extract_function("check_python")}
check_python
"""
    result = _run_script(script)
    assert result.returncode == 0, result.stderr
    assert "UV_DOWNLOAD_ATTEMPTED" in result.stderr
