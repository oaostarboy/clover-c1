"""Independent offline probes for PR 148 (no installed/live gateway involved)."""
import fcntl
import json
import multiprocessing
import os
import stat
import time

import pytest

from gateway import status

CAPABILITY = "reply-carriers-through-ack-v1"


@pytest.fixture
def owner(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("CLOVER_HOME", str(home))
    monkeypatch.setenv("CLOVER_HOME_MODE", "0701")
    # A documented supported layout: an unrelated web server may traverse
    # the home but cannot list it or write the private JSON status.
    from clover_cli.config import _secure_dir
    _secure_dir(home)
    assert stat.S_IMODE(home.stat().st_mode) == 0o701
    monkeypatch.setattr(status, "_get_code_identity_fields", lambda: {"code_sha": "isolated-owner"})
    record = status._build_pid_record()
    (home / "gateway.pid").write_text(json.dumps(record))
    status.write_runtime_status(gateway_state="running", active_agents=0,
                                update_restart_capability=CAPABILITY)
    return home


def _platform_writer(connection, platform):
    try:
        status.write_runtime_status(platform=platform, platform_state="connected")
        connection.send({"pid": os.getpid(), "start_time": status.get_process_start_time(os.getpid())})
    finally:
        connection.close()


def _hold_lock_readonly(connection, path):
    with open(path, "r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        connection.send("locked using O_RDONLY")
        connection.recv()
    connection.close()


def _crash_with_lock(connection, path):
    handle = open(path, "a+", encoding="utf-8")
    assert status._try_acquire_file_lock(handle)
    connection.send("locked")
    connection.close()
    os._exit(0)  # fixture crash, no application process is signalled


def test_real_fork_health_and_owner_merge_preserve_contract(owner):
    ctx = multiprocessing.get_context("fork")
    workers = []
    reports = []
    for platform in ["feishu", "matrix", "telegram", "slack"]:
        parent, child = ctx.Pipe()
        process = ctx.Process(target=_platform_writer, args=(child, platform))
        process.start()
        child.close()
        workers.append((process, parent, platform))
    status.write_runtime_status(active_agents=7)
    for process, connection, platform in workers:
        assert connection.poll(5)
        reports.append((platform, connection.recv()))
        process.join(5)
        assert process.exitcode == 0
        connection.close()
    row = json.loads((owner / "gateway_state.json").read_text())
    assert row["pid"] == os.getpid()
    assert row["start_time"] == status.get_process_start_time(os.getpid())
    assert row["code_sha"] == "isolated-owner"
    assert row["active_agents"] == 7
    assert row["update_restart_capability"] == CAPABILITY
    for platform, report in reports:
        entry = row["platforms"][platform]
        assert entry["state"] == "connected"
        assert entry["writer_pid"] == report["pid"]
        assert entry["writer_start_time"] == report["start_time"]


def test_crashed_writer_releases_lock_without_unlink(owner):
    lock = owner / "gateway_state.lock"
    lock.touch(exist_ok=True)
    inode = lock.stat().st_ino
    parent, child = multiprocessing.get_context("fork").Pipe()
    process = multiprocessing.get_context("fork").Process(target=_crash_with_lock, args=(child, lock))
    process.start()
    child.close()
    assert parent.poll(5)
    assert parent.recv() == "locked"
    process.join(5)
    assert process.exitcode == 0
    parent.close()
    status.write_runtime_status(active_agents=9)
    assert lock.stat().st_ino == inode
    assert json.loads((owner / "gateway_state.json").read_text())["active_agents"] == 9


def test_read_only_lock_holder_cannot_disable_status(owner):
    """Security expectation: read permission must not confer write-side DoS.

    A default-umask lock is readable by other users if the home is traversable.
    The fixture uses a read-only fd (not write access or metadata forgery), which
    is sufficient for exclusive flock on Linux. This assertion intentionally
    exposes whether the newly introduced lock can stop owner status writes.
    """
    lock = owner / "gateway_state.lock"
    # The head creates it itself; the baseline has no status lock at all.
    lock.touch(exist_ok=True)
    assert stat.S_IMODE(lock.stat().st_mode) == 0o644
    parent, child = multiprocessing.get_context("fork").Pipe()
    process = multiprocessing.get_context("fork").Process(target=_hold_lock_readonly, args=(child, lock))
    process.start()
    child.close()
    assert parent.poll(5)
    assert parent.recv() == "locked using O_RDONLY"
    started = time.monotonic()
    try:
        status.write_runtime_status(active_agents=13)
        assert json.loads((owner / "gateway_state.json").read_text())["active_agents"] == 13
    finally:
        elapsed = time.monotonic() - started
        print(json.dumps({"lock_mode": oct(stat.S_IMODE(lock.stat().st_mode)),
                          "lock_fd_access": "O_RDONLY", "elapsed_seconds": elapsed,
                          "persisted_active_agents": json.loads((owner / "gateway_state.json").read_text())["active_agents"]}))
        parent.send("release")
        process.join(5)
        parent.close()
        assert process.exitcode == 0


def test_lock_is_private_under_default_umask(owner):
    """Other users should not be able to acquire the status serialization lock."""
    lock = owner / "gateway_state.lock"
    if not lock.exists():
        pytest.skip("baseline does not create a status lock")
    assert stat.S_IMODE(lock.stat().st_mode) & 0o077 == 0


def test_status_symlink_target_and_mode_remain_intact(owner):
    original = owner / "gateway_state.json"
    target = owner / "saved-state.json"
    original.rename(target)
    target.chmod(0o600)
    original.symlink_to(target)
    status.write_runtime_status(active_agents=11)
    assert original.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text())["active_agents"] == 11
