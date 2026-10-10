"""Verify that TUI-context subprocess calls specify stdin=.

This is the pytest wrapper for scripts/check_subprocess_stdin.py.
It runs as part of the test suite so CI catches regressions when new
subprocess calls are added without stdin=subprocess.DEVNULL.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_subprocess_stdin.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("_stdin_guard", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_all_tui_subprocess_calls_have_stdin():
    """Every subprocess.run/Popen in TUI-context code must set stdin=."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"subprocess stdin= check failed:\n{result.stdout}\n{result.stderr}"
    )


def test_oauth_setup_token_keeps_inherited_stdin():
    """The interactive 'claude setup-token' login must NOT be muzzled.

    Forcing stdin=subprocess.DEVNULL here would feed the OAuth prompt EOF and
    break interactive token setup. A blanket DEVNULL sweep over TUI-context
    subprocess calls must leave this one inheriting stdin. Regression guard for
    the over-application caught while salvaging the stdin-EOF fix.

    The call's owner moved from agent/anthropic_adapter.py into
    agent/anthropic_credentials.py in the adapter godfile split; the guard
    scans both seams so a future move fails loudly instead of going dark.
    """
    candidates = [
        REPO_ROOT / "agent" / "anthropic_credentials.py",
        REPO_ROOT / "agent" / "anthropic_adapter.py",
    ]
    sources = [p.read_text() for p in candidates if p.exists()]
    owners = [
        src for src in sources
        if 'subprocess.run([claude_path, "setup-token"])' in src
    ]
    assert owners, (
        "interactive setup-token call changed shape or moved; re-verify it "
        "still inherits stdin (no stdin=subprocess.DEVNULL) and update this "
        "guard's candidate list"
    )
    for src in sources:
        assert 'subprocess.run([claude_path, "setup-token"], stdin' not in src, (
            "setup-token must inherit stdin so the user can complete the OAuth "
            "login prompt; do not add stdin=subprocess.DEVNULL"
        )


def test_inline_noqa_marker_exempts_a_call():
    """The guard honors an inline 'noqa: subprocess-stdin' exemption marker."""
    guard = _load_guard()
    flagged = guard.find_subprocess_calls(
        "import subprocess\nsubprocess.run(['ls'])\n", "x.py"
    )
    assert len(flagged) == 1, "unmarked missing-stdin call should be flagged"

    exempt = guard.find_subprocess_calls(
        "import subprocess\nsubprocess.run(['ls'])  # noqa: subprocess-stdin\n",
        "x.py",
    )
    assert exempt == [], "inline marker should exempt the call"



def test_multiline_call_with_args_on_next_line_is_checked():
    """A call whose arguments start on the line after ``(`` is found and checked."""
    guard = _load_guard()
    multiline = "import subprocess\nsubprocess.run(\n    ['ffmpeg', '-y'],\n    check=True,\n)\n"
    assert [v["line"] for v in guard.find_subprocess_calls(multiline, "x.py")] == [2]
    fixed = multiline.replace("check=True,", "stdin=subprocess.DEVNULL, check=True,")
    assert guard.find_subprocess_calls(fixed, "x.py") == []


def test_splatted_kwargs_count_only_when_definition_sets_stdin():
    guard = _load_guard()
    safe = (
        "import subprocess\n"
        "_KW = dict(capture_output=True, stdin=subprocess.DEVNULL)\n"
        "subprocess.run(['ls'], **_KW)\n"
    )
    unsafe = (
        "import subprocess\n"
        "_KW = dict(capture_output=True)\n"
        "subprocess.run(['ls'], **_KW)\n"
        "subprocess.run(['other'], stdin=subprocess.DEVNULL)\n"
    )
    assert guard.find_subprocess_calls(safe, "x.py") == []
    assert [v["line"] for v in guard.find_subprocess_calls(unsafe, "x.py")] == [3]


def test_unparsable_file_fails_closed():
    guard = _load_guard()
    assert len(guard.find_subprocess_calls("def (:\n", "x.py")) == 1


def test_gateway_cron_cli_and_scripts_are_scanned(tmp_path):
    """Gateway/cron/CLI/script code must not be exempt; only tests/ is skipped."""
    guard = _load_guard()
    bad = "import subprocess\nsubprocess.run(\n    ['ls'],\n)\n"
    for d in ("gateway", "cron", "clover_cli", "scripts", "agent"):
        (tmp_path / d / "sub").mkdir(parents=True)
        (tmp_path / d / "sub" / "mod.py").write_text(bad)
    (tmp_path / "gateway" / "tests").mkdir()
    (tmp_path / "gateway" / "tests" / "test_x.py").write_text(bad)

    violations = guard.scan_repo(tmp_path)
    assert {Path(v["file"]).parts[0] for v in violations} == {
        "gateway", "cron", "clover_cli", "scripts", "agent",
    }
    assert not any("tests" in Path(v["file"]).parts for v in violations)


def test_skip_dirs_match_relative_to_scan_root_not_absolute_path(tmp_path):
    """A checkout living under a dir named ``tests`` must still be scanned."""
    guard = _load_guard()
    root = tmp_path / "tests" / "checkout"
    (root / "gateway").mkdir(parents=True)
    (root / "gateway" / "mod.py").write_text("import subprocess\nsubprocess.run(['ls'])\n")
    assert [v["line"] for v in guard.scan_repo(root)] == [2]
