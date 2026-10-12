"""Bug C.3: refuse a restart when a fresh interpreter cannot import Clover."""

import sys
from pathlib import Path

from gateway import restart


def test_import_check_passes_with_working_interpreter(tmp_path):
    ok, detail = restart.restart_import_check(sys.executable, str(tmp_path), timeout=120)
    assert ok, detail


def test_import_check_runs_from_clover_home_not_the_repo(tmp_path):
    """A python that cannot see clover_cli (like a venv whose editable install
    points at a deleted worktree) must fail, even if the repo is next door."""
    broken = tmp_path / "py"
    broken.write_text(
        "#!/bin/sh\n"
        "pwd > \"$(dirname \"$0\")/cwd.txt\"\n"
        "echo \"ModuleNotFoundError: No module named 'clover_cli'\" >&2\n"
        "exit 1\n"
    )
    broken.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    ok, detail = restart.restart_import_check(str(broken), str(home))
    assert not ok
    assert "No module named 'clover_cli'" in detail
    assert Path((tmp_path / "cwd.txt").read_text().strip()).resolve() == home.resolve()


def test_unrunnable_probe_counts_as_failure(tmp_path):
    ok, detail = restart.restart_import_check(str(tmp_path / "nope"), str(tmp_path))
    assert not ok and "could not run" in detail


def test_refusal_message_is_plain_words():
    msg = restart.restart_refusal_message("No module named 'clover_cli'")
    assert msg.startswith("I didn't restart")
    assert "offline" in msg
