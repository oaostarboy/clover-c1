"""A read checkout must not leak an implicit cursor snapshot into other jobs."""
import sqlite3

import pytest

from clover_state import SessionDB
from clover_cli.sqlite_runtime import is_sqlite_wal_reset_vulnerable


@pytest.mark.parametrize("journal", ["delete", "wal"])
@pytest.mark.parametrize("explicit_transaction", [False, True])
def test_abandoned_read_cursor_cannot_block_helper_commit_or_pin_next_snapshot(tmp_path, journal, explicit_transaction):
    if journal == "wal" and is_sqlite_wal_reset_vulnerable(sqlite3.sqlite_version_info):
        pytest.skip("do not bypass the vulnerable-runtime WAL gate")
    path = tmp_path / "state.db"
    helper = SessionDB(path)
    reader_owner = None
    cursor = None
    try:
        helper.create_session("worker", source="test")
        rows = [{"role": role, "content": str(i)} for i in range(6) for role in ("user", "assistant")]
        helper.append_messages_batch("worker", rows)
        assert helper._conn.execute("PRAGMA journal_mode=" + journal).fetchone()[0] == journal
        helper._wal_active = journal == "wal"
        helper._conn.execute("PRAGMA busy_timeout=1")
        helper._TRANSCRIPT_WRITE_PATIENCE_S = .02
        # Real sidebar/cross-profile attach: no schema writes or journal flip.
        reader_owner = SessionDB(path, read_only=True)
        with reader_owner._read_ctx() as connection:
            if explicit_transaction:
                connection.execute("BEGIN")
            cursor = connection.execute("SELECT * FROM messages ORDER BY id")
            assert cursor.fetchone() is not None  # deliberately leave unread rows
            assert connection.in_transaction is explicit_transaction
            # The implicit SELECT is invisible to in_transaction.
        # The owner has returned from its read; model/tool IO may now happen.
        # A different helper's COMMIT must not wait behind the abandoned cursor.
        helper.append_messages_batch("worker", [{"role": "user", "content": "helper saved"}])
        with reader_owner._read_ctx() as connection:
            assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 13
        assert [m["content"] for m in helper.get_messages_as_conversation("worker")] == [m["content"] for m in rows] + ["helper saved"]
        with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
            cursor.fetchone()
    finally:
        if cursor is not None:
            cursor.close()
        if reader_owner is not None:
            reader_owner.close()
        helper.close()


def test_nested_read_leases_close_only_their_own_explicit_cursors(tmp_path):
    writer = SessionDB(tmp_path / "state.db")
    reader = None
    try:
        writer.create_session("worker", source="test")
        writer.append_messages_batch("worker", [{"role": "user", "content": str(i)} for i in range(5)])
        reader = SessionDB(writer.db_path, read_only=True)
        with reader._read_ctx() as connection:
            outer = connection.cursor()
            outer.execute("SELECT * FROM messages ORDER BY id")
            assert outer.fetchone() is not None
            with connection.read_cursor_scope():
                inner = connection.cursor()
                inner.execute("SELECT * FROM messages ORDER BY id")
                assert inner.fetchone() is not None
            with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
                inner.fetchone()
            assert outer.fetchone() is not None
        with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
            outer.fetchone()
    finally:
        if reader is not None:
            reader.close()
        writer.close()


def test_fallback_read_inside_writer_never_rolls_back_the_owners_transaction(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("worker", source="test")
        db._conn.execute("PRAGMA journal_mode=DELETE")
        db._wal_active = False
        def body(connection):
            connection.execute("UPDATE sessions SET title='committed owner work' WHERE id='worker'")
            with db._read_ctx() as reader:
                assert reader is connection
                assert reader.in_transaction
                assert reader.execute("SELECT title FROM sessions WHERE id='worker'").fetchone()[0] == "committed owner work"
            assert connection.in_transaction
        # An existing owner transaction predates this read lease. Do not
        # recursively acquire SessionDB's deliberately non-reentrant mutex.
        db._conn.execute("BEGIN IMMEDIATE")
        body(db._conn)
        db._conn.commit()
        assert db.get_session("worker")["title"] == "committed owner work"
        assert not db._conn.in_transaction
    finally:
        db.close()


def test_cancelled_read_scope_releases_its_explicit_transaction(tmp_path):
    class Cancelled(BaseException):
        pass
    writer = SessionDB(tmp_path / "state.db")
    reader = None
    try:
        writer.create_session("worker", source="test")
        writer._conn.execute("PRAGMA journal_mode=DELETE")
        writer._wal_active = False
        reader = SessionDB(writer.db_path, read_only=True)
        with pytest.raises(Cancelled):
            with reader._read_ctx() as connection:
                connection.execute("BEGIN")
                connection.execute("SELECT * FROM sessions").fetchone()
                raise Cancelled()
        assert not reader._conn.in_transaction
        writer.append_messages_batch("worker", [{"role": "user", "content": "bounded recovery"}])
        assert writer.get_messages_as_conversation("worker")[0]["content"] == "bounded recovery"
    finally:
        if reader is not None:
            reader.close()
        writer.close()
