"""Real SessionDB COMMIT-pending lock tests (private DELETE-mode database)."""
import sqlite3
import threading
import time

import pytest

from clover_state import SessionDB


def _bare_agent_for_stop():
    """Construct a bare AIAgent with the state used by public hard_interrupt."""
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    for name, value in {
        "_interrupt_requested": False,
        "_interrupt_message": None,
        "_hard_interrupt_requested": threading.Event(),
        "_execution_thread_id": None,
        "_interrupt_thread_signal_pending": False,
        "_active_children": [],
        "_active_children_lock": threading.Lock(),
        "quiet_mode": True,
        "provider": "openrouter",
        "model": "test/model",
        "_base_url": "http://localhost:1234",
    }.items():
        setattr(agent, name, value)
    return agent


def _delete_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db._conn.execute("PRAGMA journal_mode=DELETE")
    db._wal_active = False
    db._conn.execute("PRAGMA busy_timeout=1")
    db.create_session("writer", source="test")
    return db


def test_append_waits_in_commit_for_reader_release_without_replaying_body(tmp_path):
    db = _delete_db(tmp_path)
    db._TRANSCRIPT_WRITE_PATIENCE_S = 0.2
    reader = sqlite3.connect(
        db.db_path, isolation_level=None, timeout=0, check_same_thread=False
    )
    reader.execute("BEGIN")
    cursor = reader.execute("SELECT id FROM messages")
    cursor.fetchone()
    started = threading.Event()
    trace = []

    def record(sql):
        trace.append(sql)
        if sql == "COMMIT":
            started.set()

    db._conn.set_trace_callback(record)

    def release_reader():
        assert started.wait(2), "actual SessionDB write never reached COMMIT"
        time.sleep(0.04)
        reader.rollback()

    releaser = threading.Thread(target=release_reader)
    releaser.start()
    began = time.monotonic()
    try:
        db.append_message("writer", "user", "single attempt")
        elapsed = time.monotonic() - began
        assert elapsed < 0.25
        assert sum(sql == "BEGIN IMMEDIATE" for sql in trace) == 1
        assert db._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content='single attempt'"
        ).fetchone()[0] == 1
        assert db._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1
    finally:
        releaser.join(2)
        cursor.close()
        reader.close()
        db._conn.set_trace_callback(None)
        db.close()


def test_commit_timeout_exhaustion_restores_prior_policy_and_does_not_duplicate(tmp_path):
    db = _delete_db(tmp_path)
    db._conn.execute("PRAGMA busy_timeout=17")
    db._TRANSCRIPT_WRITE_PATIENCE_S = 0.04
    reader = sqlite3.connect(db.db_path, isolation_level=None, timeout=0)
    reader.execute("BEGIN")
    cursor = reader.execute("SELECT id FROM messages")
    cursor.fetchone()
    body_calls = 0

    def body(conn):
        nonlocal body_calls
        body_calls += 1
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            ("writer", "user", "timeout"),
        )

    try:
        began = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="phase=commit"):
            db._execute_write(body, patience_s=0.04)
        elapsed = time.monotonic() - began
        assert elapsed < 0.25
        # SQLite's busy-handler behavior differs by bundled SQLite version:
        # a failed COMMIT may consume the whole deadline (one body attempt),
        # or return early enough for the established definite-BUSY retry
        # (two attempts). In either case the rolled-back insert is not durable.
        assert 1 <= body_calls <= 2
        assert db._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 17
        assert db._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content='timeout'"
        ).fetchone()[0] == 0
    finally:
        cursor.close()
        reader.close()
        db.close()


def test_ambiguous_post_commit_error_is_not_replayed_and_timeout_is_restored(tmp_path):
    db = _delete_db(tmp_path)
    db._conn.execute("PRAGMA busy_timeout=29")
    body_calls = 0
    real_conn = db._conn

    class CommitAcknowledgementLost:
        def __getattr__(self, name):
            return getattr(real_conn, name)

        def commit(self):
            real_conn.commit()
            raise sqlite3.OperationalError("interrupted after commit acknowledgment lost")

    db._conn = CommitAcknowledgementLost()

    def body(conn):
        nonlocal body_calls
        body_calls += 1
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            ("writer", "user", "committed once"),
        )

    try:
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            db._execute_write(body, patience_s=0.2)
        assert body_calls == 1
        assert real_conn.execute("PRAGMA busy_timeout").fetchone()[0] == 29
        assert real_conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content='committed once'"
        ).fetchone()[0] == 1
    finally:
        db._conn = real_conn
        db.close()


def test_100ms_commit_budget_bounds_stop_and_ui_response(tmp_path):
    db = _delete_db(tmp_path)
    db._TRANSCRIPT_WRITE_PATIENCE_S = 0.1
    reader = sqlite3.connect(
        db.db_path, isolation_level=None, timeout=0, check_same_thread=False
    )
    reader.execute("BEGIN")
    cursor = reader.execute("SELECT id FROM messages")
    cursor.fetchone()
    commit_started = threading.Event()
    stop_returned = threading.Event()
    ui_done = threading.Event()
    trace = []

    def record(sql):
        trace.append(sql)
        if sql == "COMMIT":
            commit_started.set()

    db._conn.set_trace_callback(record)
    write_errors = []

    def write():
        try:
            db.append_message("writer", "user", "stopped while blocked")
        except BaseException as exc:
            write_errors.append(exc)

    stop_agent = _bare_agent_for_stop()

    def stop_and_read():
        assert commit_started.wait(2)
        # Exercise the public AIAgent hard-stop path. It must acknowledge
        # promptly without waiting for SessionDB's write lock; the UI read may
        # queue behind SQLite, but remains bounded by the existing deadline.
        stop_agent.hard_interrupt("Stop requested")
        stop_returned.set()
        db.get_session("writer")  # UI-facing read queues only on SessionDB's lock.
        ui_done.set()

    writer_thread = threading.Thread(target=write)
    stop_thread = threading.Thread(target=stop_and_read)
    began = time.monotonic()
    try:
        writer_thread.start()
        stop_thread.start()
        writer_thread.join(0.22)
        stop_thread.join(0.22)
        elapsed = time.monotonic() - began
        assert not writer_thread.is_alive()
        assert not stop_thread.is_alive()
        assert len(write_errors) == 1
        assert isinstance(write_errors[0], sqlite3.OperationalError)
        assert "phase=commit" in str(write_errors[0]).lower()
        assert getattr(stop_agent, "_hard_interrupt_requested").is_set()
        assert stop_returned.is_set() and ui_done.is_set()
        assert elapsed < 0.25
        assert sum(sql == "BEGIN IMMEDIATE" for sql in trace) == 1
        assert db._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1
        assert db._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content='stopped while blocked'"
        ).fetchone()[0] == 0
    finally:
        reader.rollback()
        cursor.close()
        reader.close()
        db._conn.set_trace_callback(None)
        stop_agent.clear_interrupt()
        db.close()


def test_new_reader_retries_one_pending_lock_on_existing_read_path(tmp_path):
    db = _delete_db(tmp_path)
    db._TRANSCRIPT_WRITE_PATIENCE_S = 0.1
    reader = sqlite3.connect(
        db.db_path, isolation_level=None, timeout=0, check_same_thread=False
    )
    reader.execute("BEGIN")
    cursor = reader.execute("SELECT id FROM messages")
    cursor.fetchone()
    commit_started = threading.Event()
    db._conn.set_trace_callback(
        lambda sql: commit_started.set() if sql == "COMMIT" else None
    )
    observed = {}
    errors = []

    def new_reader():
        assert commit_started.wait(2)
        conn = sqlite3.connect(db.db_path, isolation_level=None, timeout=0)
        attempts = 0

        def query():
            nonlocal attempts
            attempts += 1
            return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

        try:
            observed["rows"] = db._retry_locked_read(query, patience_s=0.2)
            observed["attempts"] = attempts
        except BaseException as exc:
            errors.append(exc)
        finally:
            conn.close()

    def release_reader():
        assert commit_started.wait(2)
        time.sleep(0.06)
        reader.rollback()

    new_thread = threading.Thread(target=new_reader)
    release_thread = threading.Thread(target=release_reader)
    began = time.monotonic()
    try:
        new_thread.start()
        release_thread.start()
        db.append_message("writer", "user", "read retry")
        new_thread.join(1)
        release_thread.join(1)
        assert not new_thread.is_alive()
        assert not release_thread.is_alive()
        assert not errors
        assert observed["attempts"] >= 2
        assert observed["rows"] == 1
        assert time.monotonic() - began < 0.25
    finally:
        cursor.close()
        reader.close()
        db._conn.set_trace_callback(None)
        db.close()
