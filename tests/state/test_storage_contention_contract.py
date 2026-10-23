"""Real SQLite transaction phases, cancellation, and independent job writers."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from clover_state import SessionDB
from clover_cli.sqlite_runtime import is_sqlite_wal_reset_vulnerable


def _delete_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db._conn.execute("PRAGMA journal_mode=DELETE")
    db._wal_active = False
    db._conn.execute("PRAGMA busy_timeout=1")
    db.create_session("worker", source="test")
    return db


@pytest.mark.parametrize("phase", ["begin", "commit"])
@pytest.mark.parametrize("release", [False, True])
def test_real_contention_phase_and_transaction_lifetime(tmp_path, monkeypatch, phase, release):
    db = _delete_db(tmp_path)
    blocker = sqlite3.connect(db.db_path, isolation_level=None)
    trace = []
    retries = []
    try:
        if phase == "begin":
            blocker.execute("BEGIN IMMEDIATE")
        else:
            blocker.execute("BEGIN")
            blocker.execute("SELECT * FROM messages").fetchall()
        db._conn.set_trace_callback(trace.append)
        real_retry = db._sleep_before_write_retry

        def observe_retry(deadline, patience):
            retries.append(db._conn.in_transaction)
            if release:
                blocker.rollback()
            return real_retry(deadline, patience)

        monkeypatch.setattr(db, "_sleep_before_write_retry", observe_retry)
        write = lambda conn: conn.execute("UPDATE sessions SET title='durable progress' WHERE id='worker'")
        if release:
            db._execute_write(write, patience_s=.2)
            assert db.get_session("worker")["title"] == "durable progress"
        else:
            with pytest.raises(sqlite3.OperationalError, match="phase=" + phase):
                db._execute_write(write, patience_s=.01)
            assert blocker.in_transaction
        assert retries and not any(retries), "retry sleep must not hold a write transaction"
        assert not db._conn.in_transaction
        assert any(sql == "BEGIN IMMEDIATE" for sql in trace)
        assert any(sql.startswith("UPDATE sessions SET title") for sql in trace) is (phase == "commit" or release)
        assert any(sql == "COMMIT" for sql in trace) is (phase == "commit" or release)
        if phase == "commit":
            assert "ROLLBACK" in trace
    finally:
        db._conn.set_trace_callback(None)
        blocker.close()
        db.close()


def test_cancellation_rolls_back_before_next_writer(tmp_path):
    class Cancelled(BaseException):
        pass

    db = _delete_db(tmp_path)
    trace = []
    db._conn.set_trace_callback(trace.append)
    try:
        def cancelled_write(conn):
            conn.execute("UPDATE sessions SET title='cancelled' WHERE id='worker'")
            raise Cancelled()
        with pytest.raises(Cancelled):
            db._execute_write(cancelled_write, patience_s=.01)
        assert not db._conn.in_transaction
        assert trace[-1] == "ROLLBACK"
        other = SessionDB(db.db_path)
        try:
            other.append_messages_batch("worker", [{"role": "user", "content": "next writer"}])
            assert other.get_session("worker")["title"] != "cancelled"
        finally:
            other.close()
    finally:
        db.close()


@pytest.mark.skipif(is_sqlite_wal_reset_vulnerable(sqlite3.sqlite_version_info), reason="supported WAL runtime required")
def test_cron_helpers_and_snapshot_reader_preserve_exact_order(tmp_path):
    path = tmp_path / "state.db"
    owners = ["cron", "helper-1", "helper-2", "parent"]
    databases = [SessionDB(path) for _ in owners]
    reader = None
    barrier = threading.Barrier(len(owners))
    try:
        for owner, db in zip(owners, databases):
            db.create_session(owner, source="test")
            assert db._wal_active
        reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM messages").fetchall()

        def work(item):
            owner, db = item
            barrier.wait(timeout=10)
            expected = []
            for i in range(5):
                pair = [{"role": "user", "content": f"{owner}:request:{i}"},
                        {"role": "assistant", "content": f"{owner}:result:{i}"}]
                db.append_messages_batch(owner, pair)
                assert not db._conn.in_transaction
                expected.extend((m["role"], m["content"]) for m in pair)
            return owner, expected

        with ThreadPoolExecutor(max_workers=len(owners)) as pool:
            expected = dict(pool.map(work, zip(owners, databases)))
        assert reader.in_transaction
        for owner, db in zip(owners, databases):
            rows = db.get_messages_as_conversation(owner)
            assert [(m["role"], m["content"]) for m in rows] == expected[owner]
        assert reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        reader.rollback()
        assert reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 40
    finally:
        if reader is not None:
            reader.close()
        for db in databases:
            db.close()
