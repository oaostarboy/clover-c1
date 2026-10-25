"""Status serialization locks are private without replacing active inodes."""
import json
import os
import stat
import subprocess
import sys
import time

import pytest

from gateway import status


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    monkeypatch.setenv("CLOVER_HOME_MODE", "0701")
    from clover_cli.config import _secure_dir
    _secure_dir(home)
    return home


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
@pytest.mark.parametrize("mask", [0o022, 0o000, 0o077])
def test_new_lock_is_owner_only_in_traversable_home(home, mask):
    old_mask = os.umask(mask)
    try:
        status.write_runtime_status(active_agents=13)
    finally:
        os.umask(old_mask)
    lock = home / "gateway_state.lock"
    assert stat.S_IMODE(home.stat().st_mode) == 0o701
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert json.loads((home / "gateway_state.json").read_text())["active_agents"] == 13


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
@pytest.mark.parametrize("mode,expected", [(0o644, 0o600), (0o666, 0o600), (0o700, 0o600), (0o600, 0o600)])
def test_existing_lock_tightened_without_replacing_inode(home, mode, expected):
    lock = home / "gateway_state.lock"
    lock.write_text("existing lock data")
    lock.chmod(mode)
    before = lock.stat()
    status.write_runtime_status(active_agents=13)
    assert stat.S_IMODE(lock.stat().st_mode) == expected
    assert os.path.samestat(before, lock.stat())
    assert lock.read_text() == "existing lock data"


@pytest.mark.skipif(sys.platform == "win32", reason="Unprivileged POSIX symlinks")
def test_lock_symlink_is_rejected_without_touching_target(home):
    target = home / "external"
    target.write_text("not a lock")
    target.chmod(0o644)
    lock = home / "gateway_state.lock"
    lock.symlink_to(target)
    with pytest.raises(OSError):
        status.write_runtime_status(active_agents=13)
    assert lock.is_symlink()
    assert target.read_text() == "not a lock"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert not (home / "gateway_state.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX hard links")
def test_lock_hardlink_is_rejected_without_chmod_of_other_path(home):
    target = home / "external"
    target.write_text("not a lock")
    target.chmod(0o644)
    lock = home / "gateway_state.lock"
    os.link(target, lock)
    with pytest.raises(OSError):
        status.write_runtime_status(active_agents=13)
    assert target.read_text() == "not a lock"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert lock.stat().st_nlink == 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner metadata")
def test_foreign_owned_lock_fails_closed_before_chmod(home, monkeypatch):
    lock = home / "gateway_state.lock"
    lock.touch(mode=0o644)
    before = lock.stat()
    # Ownership mismatch without a privileged chown of the host fixture.
    monkeypatch.setattr(status.os, "geteuid", lambda: before.st_uid + 1)
    with pytest.raises(PermissionError):
        status.write_runtime_status(active_agents=13)
    assert os.path.samestat(before, lock.stat())
    assert stat.S_IMODE(lock.stat().st_mode) == 0o644
    assert not (home / "gateway_state.json").exists()


def test_changed_inode_during_open_fails_closed_without_chmod(home, monkeypatch):
    lock = home / "gateway_state.lock"
    lock.touch(mode=0o644)
    original = home / "original-lock"
    replacement = home / "replacement"
    replacement.write_text("unrelated file")
    replacement.chmod(0o644)
    real_open = os.open

    def raced_open(path, flags, *args, **kwargs):
        if os.fspath(path) == str(lock) and not flags & os.O_EXCL:
            lock.rename(original)
            replacement.rename(lock)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(status.os, "open", raced_open)
    with pytest.raises(OSError):
        status.write_runtime_status(active_agents=13)
    assert lock.read_text() == "unrelated file"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o644
    assert not (home / "gateway_state.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
@pytest.mark.parametrize("mode", [0o400, 0o200, 0o000])
def test_owner_permissions_are_never_broadened(home, mode):
    lock = home / "gateway_state.lock"
    lock.touch()
    lock.chmod(mode)
    before = lock.stat()
    try:
        with pytest.raises(PermissionError):
            status.write_runtime_status(active_agents=13)
        assert os.path.samestat(before, lock.stat())
        assert stat.S_IMODE(lock.stat().st_mode) == mode
        assert not (home / "gateway_state.json").exists()
    finally:
        lock.chmod(0o600)


def test_replaced_inode_while_waiting_cannot_publish_status(home, monkeypatch):
    lock = home / "gateway_state.lock"
    lock.touch(mode=0o600)
    original = home / "original-lock"
    acquire = status._try_acquire_file_lock

    def replaced(handle):
        lock.rename(original)
        lock.touch(mode=0o600)
        return acquire(handle)

    monkeypatch.setattr(status, "_try_acquire_file_lock", replaced)
    with pytest.raises(OSError):
        status.write_runtime_status(active_agents=13)
    assert not (home / "gateway_state.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX read-only flock and permissions")
def test_preexisting_readonly_holder_is_not_bypassed_by_permission_migration(home):
    status.write_runtime_status(active_agents=0)
    lock = home / "gateway_state.lock"
    lock.chmod(0o644)
    before = lock.stat()
    child = subprocess.Popen([
        sys.executable, "-c",
        "import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDONLY); "
        "fcntl.flock(fd,fcntl.LOCK_EX); print('locked O_RDONLY',flush=True); "
        "sys.stdin.readline(); os.close(fd)", str(lock),
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked O_RDONLY"
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="runtime status writer is busy"):
            status.write_runtime_status(active_agents=13)
        elapsed = time.monotonic() - started
        assert elapsed < 3
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
        assert os.path.samestat(before, lock.stat())
        assert json.loads((home / "gateway_state.json").read_text())["active_agents"] == 0
        # The same UID can still open the private lock. Chmod cannot revoke
        # the existing child's descriptor, nor may migration split the inode.
        import fcntl
        with lock.open("r") as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        stdout, stderr = child.communicate("release\n", timeout=5)
        assert child.returncode == 0, (stdout, stderr)
    status.write_runtime_status(active_agents=13)
    assert os.path.samestat(before, lock.stat())
    assert json.loads((home / "gateway_state.json").read_text())["active_agents"] == 13
    print(json.dumps({"readonly_child": "O_RDONLY + LOCK_EX", "elapsed": elapsed,
                      "mode_after": oct(stat.S_IMODE(lock.stat().st_mode)),
                      "same_inode": True, "persisted_after_release": 13}))


def test_crashed_lock_holder_releases_same_inode(home):
    status.write_runtime_status(active_agents=0)
    lock = home / "gateway_state.lock"
    before = lock.stat()
    child = subprocess.run([
        sys.executable, "-c",
        "import os; from pathlib import Path; from gateway import status; "
        "h=open(Path(os.environ['CLOVER_HOME'])/'gateway_state.lock','r+'); "
        "assert status._try_acquire_file_lock(h); os._exit(0)",
    ], capture_output=True, text=True, timeout=5)
    assert child.returncode == 0, child.stderr
    status.write_runtime_status(active_agents=13)
    assert os.path.samestat(before, lock.stat())
    assert json.loads((home / "gateway_state.json").read_text())["active_agents"] == 13
