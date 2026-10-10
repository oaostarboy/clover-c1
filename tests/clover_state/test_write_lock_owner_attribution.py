"""Regression: a state.db write-lock timeout names the process holding the lock (C1.4 R04).

Before, ``database is locked (another Clover process held the state.db write lock
for over Ns ...)`` identified the victim only. ``/proc/locks`` names the holder.
The holder goes to the log, never into the exception text.
"""

import io
import logging
import os
import sqlite3
import subprocess
import sys
import textwrap
import time

import pytest

import clover_state_lockowners
from clover_state import SessionDB
from clover_state_lockowners import parse_proc_locks, state_db_write_lock_holders


def test_parse_proc_locks_keeps_only_write_locks_on_our_inodes_and_decodes_the_wal_write_byte():
    inodes = {(os.makedev(0x103, 0x02), 4194737): "-shm", (os.makedev(0x103, 0x02), 4228330): ""}
    text = textwrap.dedent("""\
        1: POSIX  ADVISORY  WRITE 594094 103:02:4194737 120 120
        2: POSIX  ADVISORY  READ 594094 103:02:4194737 125 125
        3: POSIX  ADVISORY  READ 99493 103:02:4194737 128 128
        4: OFDLCK ADVISORY  WRITE -1 103:02:4228330 1073741825 1073741825
        5: POSIX  ADVISORY  WRITE 4242 103:02:99999 120 120
        6: -> POSIX  ADVISORY  WRITE 777 103:02:4194737 120 120
    """)
    assert parse_proc_locks(text, inodes) == [
        (594094, "WAL write", "-shm"),
        (-1, "RESERVED", ""),
    ]


@pytest.mark.linux_only
def test_holder_skipped_by_one_proc_locks_pass_is_still_named(tmp_path, monkeypatch):
    """/proc/locks is served over several read()s, so churn elsewhere can skip an entry in one pass."""
    db = tmp_path / "state.db"
    db.write_bytes(b"")
    st = os.stat(db)
    held = (f"1: POSIX  ADVISORY  WRITE {os.getpid()} "
            f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino} 1073741825 1073741825\n")
    passes = iter(["", held, ""])
    real_open = open

    def fake_open(path, *a, **k):
        return io.StringIO(next(passes)) if path == "/proc/locks" else real_open(path, *a, **k)

    monkeypatch.setattr(clover_state_lockowners, "open", fake_open, raising=False)
    lines = state_db_write_lock_holders(db)
    assert len(lines) == 1 and f"PID {os.getpid()} " in lines[0] and "RESERVED" in lines[0], lines


def _spawn_wal_writer(db, sleep_s=30):
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sqlite3, time
            c = sqlite3.connect({str(db)!r}, isolation_level=None)
            c.execute("BEGIN IMMEDIATE")
            c.execute("CREATE TABLE IF NOT EXISTS holder_probe(x)")
            c.execute("INSERT INTO holder_probe VALUES (1)")
            print("held", flush=True)
            time.sleep({sleep_s})
        """)],
        stdout=subprocess.PIPE, text=True,
    )


@pytest.mark.linux_only
def test_live_writer_in_another_process_is_named_by_pid(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=wal")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()

    holder = _spawn_wal_writer(db)
    try:
        assert holder.stdout.readline().strip() == "held"
        deadline = time.monotonic() + 5
        lines = []
        while time.monotonic() < deadline:
            lines = state_db_write_lock_holders(db)
            if lines:
                break
            time.sleep(0.05)
        assert any(f"PID {holder.pid} " in line and "WAL write" in line for line in lines), lines
    finally:
        holder.kill()
        holder.wait()
    assert state_db_write_lock_holders(db) == []


@pytest.mark.linux_only
def test_execute_write_timeout_logs_holder_pid_but_keeps_it_out_of_the_exception(tmp_path, caplog):
    """End to end through SessionDB._execute_write with a real foreign writer."""
    db_path = tmp_path / "state.db"
    sdb = SessionDB(db_path=db_path)
    sdb.create_session("s1", source="cli")
    holder = _spawn_wal_writer(db_path)
    try:
        assert holder.stdout.readline().strip() == "held"
        sdb._WRITE_RETRY_SLOW_AFTER_S = 0.0
        with caplog.at_level(logging.WARNING, logger="clover_state_lockowners"):
            with pytest.raises(sqlite3.OperationalError) as excinfo:
                sdb._execute_write(lambda conn: conn.execute("DELETE FROM sessions"), patience_s=0.5)
        assert "database is locked" in str(excinfo.value)
        assert str(holder.pid) not in str(excinfo.value)
        records = [r.getMessage() for r in caplog.records if r.name == "clover_state_lockowners"]
        assert any(f"PID {holder.pid} " in m and ("WAL write" in m or "RESERVED" in m) for m in records), records
    finally:
        holder.kill()
        holder.wait()
        sdb.close()


def test_log_write_lock_holders_never_raises(tmp_path, monkeypatch):
    def boom(_path):
        raise RuntimeError("proc exploded")

    monkeypatch.setattr(clover_state_lockowners, "state_db_write_lock_holders", boom)
    clover_state_lockowners.log_write_lock_holders(tmp_path / "state.db", 1.0)


def test_non_linux_fails_open_and_logs_nothing(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(clover_state_lockowners.sys, "platform", "darwin")
    with caplog.at_level(logging.WARNING, logger="clover_state_lockowners"):
        clover_state_lockowners.log_write_lock_holders(tmp_path / "state.db", 1.0)
    assert state_db_write_lock_holders(tmp_path / "state.db") == []
    assert not caplog.records
