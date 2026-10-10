"""Tests for clover_cli.build_info — baked-in build SHA resolution.

The build SHA is written by the Dockerfile's ``CLOVER_GIT_SHA`` build-arg
into ``<project_root>/.clover_build_sha``.  These tests cover the read-side
helper: missing file, malformed file, truncation, and error tolerance.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from clover_cli import build_info


def test_get_build_sha_returns_none_when_file_absent(tmp_path):
    """Source installs: no file present → None, callers fall back to git."""
    from clover_cli import build_info

    missing = tmp_path / ".clover_build_sha"  # never created

    with patch.object(build_info, "_BUILD_SHA_FILE", missing):
        assert build_info.get_build_sha() is None


def test_get_build_sha_respects_short_argument(tmp_path):
    """``short=N`` truncates to N chars; ``short<=0`` returns full SHA."""
    from clover_cli import build_info

    sha_file = tmp_path / ".clover_build_sha"
    full_sha = "abcdef1234567890abcdef1234567890abcdef12"
    sha_file.write_text(full_sha + "\n")

    with patch.object(build_info, "_BUILD_SHA_FILE", sha_file):
        assert build_info.get_build_sha(short=12) == "abcdef123456"
        assert build_info.get_build_sha(short=0) == full_sha
        assert build_info.get_build_sha(short=-1) == full_sha



# ---------------------------------------------------------------------------
# Immutable boot identity (what the running process loaded) vs. the
# refreshable on-disk identity (what update_receipt re-reads after a pull).
# ---------------------------------------------------------------------------


def _git(repo, *args):
    import subprocess

    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    ).stdout.strip()


def _commit(repo, message):
    (repo / "f.txt").write_text(message, encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def tmp_checkout(tmp_path, monkeypatch):
    """A real git repo standing in for the project root; both identity caches empty."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    monkeypatch.setattr(build_info, "_PROJECT_ROOT", repo)
    monkeypatch.setattr(build_info, "_BUILD_SHA_FILE", tmp_path / "no-build-sha")
    monkeypatch.setattr(build_info, "_boot_identity", None)
    monkeypatch.setattr(build_info, "_code_identity_cache", None)
    return repo


def test_boot_identity_is_none_until_captured(tmp_checkout):
    _commit(tmp_checkout, "a")
    assert build_info.get_boot_identity() is None
    # Reading the refreshable identity must not capture a boot identity.
    build_info.get_code_identity()
    assert build_info.get_boot_identity() is None


def test_boot_identity_survives_a_refresh_of_the_code_identity(tmp_checkout):
    a = _commit(tmp_checkout, "a")
    boot = build_info.capture_boot_identity()
    assert boot["sha"] == a

    b = _commit(tmp_checkout, "b")  # a `git pull` under the running process
    # What update_receipt / linux_catchup_handoff do after the pull:
    assert build_info.get_code_identity(refresh=True)["sha"] == b

    assert build_info.get_boot_identity()["sha"] == a
    assert build_info.get_code_identity()["sha"] == b


def test_capture_is_first_call_wins(tmp_checkout):
    a = _commit(tmp_checkout, "a")
    build_info.capture_boot_identity()
    _commit(tmp_checkout, "b")
    assert build_info.capture_boot_identity()["sha"] == a


def test_boot_identity_reads_disk_not_a_stale_refreshable_cache(tmp_checkout):
    a = _commit(tmp_checkout, "a")
    build_info.get_code_identity()  # cache holds a
    b = _commit(tmp_checkout, "b")  # disk moved; cache not yet refreshed
    assert build_info.capture_boot_identity()["sha"] == b
    assert build_info.get_code_identity()["sha"] == a  # untouched


def test_boot_identity_without_git_or_build_file_has_no_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(build_info, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(build_info, "_BUILD_SHA_FILE", tmp_path / "no-build-sha")
    monkeypatch.setattr(build_info, "_boot_identity", None)
    assert build_info.capture_boot_identity()["sha"] is None


def test_returned_boot_identity_is_a_copy(tmp_checkout):
    a = _commit(tmp_checkout, "a")
    first = build_info.capture_boot_identity()
    first["sha"] = "mutated"
    assert build_info.get_boot_identity()["sha"] == a
