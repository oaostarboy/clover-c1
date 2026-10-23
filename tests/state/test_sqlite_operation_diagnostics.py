"""Future lock reports identify current connection scopes without SQL or secrets."""
import sqlite3

import pytest

from clover_state import SessionDB


def test_commit_contention_names_live_reader_connection_caller_and_inode(tmp_path, caplog):
    writer = SessionDB(tmp_path / "state.db")
    reader = None
    cursor = None
    try:
        writer.create_session("worker", source="test")
        writer.append_messages_batch("worker", [{"role": "user", "content": str(i)} for i in range(4)])
        writer._conn.execute("PRAGMA journal_mode=DELETE")
        writer._wal_active = False
        writer._conn.execute("PRAGMA busy_timeout=1")
        reader = SessionDB(writer.db_path, read_only=True)
        with reader._read_ctx() as connection:
            cursor = connection.execute("SELECT * FROM messages WHERE content != 'SYNTHETIC_PRIVATE_SQL_VALUE'")
            assert cursor.fetchone() is not None
            assert connection.in_transaction is False
            with pytest.raises(sqlite3.OperationalError, match="phase=commit"):
                writer._execute_write(lambda c: c.execute("UPDATE sessions SET title='progress'"), patience_s=.01)
            assert "connection_id=" + hex(id(connection)) in caplog.text
            assert "test_sqlite_operation_diagnostics.py:test_commit_contention" in caplog.text
            assert "inode=" + str(writer.db_path.stat().st_ino) in caplog.text
            assert "kind=read" in caplog.text
            assert "SYNTHETIC_PRIVATE_SQL_VALUE" not in caplog.text
        # Read scope cleanup, not a larger timeout, releases the blocker.
        writer.append_messages_batch("worker", [{"role": "assistant", "content": "saved after reader returned"}])
    finally:
        if cursor is not None:
            cursor.close()
        if reader is not None:
            reader.close()
        writer.close()


def test_write_diagnostic_scope_tracks_body_and_cleans_up_on_cancel(tmp_path):
    from clover_cli.sqlite_operation_observer import active_connection_operations
    writer = SessionDB(tmp_path / "state.db")
    observed = []
    class Cancelled(BaseException):
        pass
    try:
        assert active_connection_operations(writer.db_path) == []
        def body(connection):
            observed.extend(active_connection_operations(writer.db_path))
            raise Cancelled()
        with pytest.raises(Cancelled):
            writer._execute_write(body, patience_s=.01)
        assert len(observed) == 1
        assert observed[0]["connection_id"] == hex(id(writer._conn))
        assert observed[0]["phase"] == "body"
        assert observed[0]["in_transaction"] is True
        assert observed[0]["inode"] == writer.db_path.stat().st_ino
        assert active_connection_operations(writer.db_path) == []
        assert not writer._conn.in_transaction
    finally:
        writer.close()


def test_tracked_in_memory_read_scope_still_closes_its_cursor():
    from clover_cli.sqlite_safe_read import connect_tracked
    connection = connect_tracked(":memory:")
    try:
        connection.execute("CREATE TABLE data(value)")
        connection.executemany("INSERT INTO data VALUES (?)", [(1,), (2,), (3,)])
        with connection.read_cursor_scope():
            cursor = connection.execute("SELECT * FROM data")
            assert cursor.fetchone()[0] == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
            cursor.fetchone()
    finally:
        connection.close()
