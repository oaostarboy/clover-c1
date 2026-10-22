"""Real SQLite reader isolation and truthful contention regressions."""
import sqlite3
import pytest
from clover_state import SessionDB
from clover_cli.sqlite_runtime import is_sqlite_wal_reset_vulnerable


@pytest.mark.skipif(is_sqlite_wal_reset_vulnerable(sqlite3.sqlite_version_info), reason="requires repaired SQLite runtime for nonblocking long-reader acceptance")
def test_supported_runtime_makes_progress_with_reader_held(tmp_path):
    # Never bypass the vulnerable-runtime WAL gate.
    assert not is_sqlite_wal_reset_vulnerable(sqlite3.sqlite_version_info), sqlite3.sqlite_version
    db = SessionDB(tmp_path / "state.db")
    try:
        assert db._wal_active
        db.create_session("worker", source="test", model="openai-codex/gpt-6.1-sol")
        reader = sqlite3.connect(f"file:{db.db_path}?mode=ro", uri=True, isolation_level=None)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM sessions").fetchall()
            db._TRANSCRIPT_WRITE_PATIENCE_S = .1
            db.append_messages_batch("worker", [{"role": "user", "content": "approved task"}])
            opened = SessionDB(db.db_path)
            try:
                assert opened.get_messages_as_conversation("worker")[0]["content"] == "approved task"
            finally:
                opened.close()
            assert reader.in_transaction  # reader did not silently finish
        finally:
            reader.close()
    finally:
        db.close()


def test_returned_reader_does_not_keep_explicit_snapshot(tmp_path):
    if is_sqlite_wal_reset_vulnerable(sqlite3.sqlite_version_info):
        pytest.skip("read pool is used only on supported WAL runtimes")
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("worker", source="test")
        with db._read_ctx() as reader:
            reader.execute("BEGIN")
            assert reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        db.append_messages_batch("worker", [{"role": "user", "content": "durable progress"}])
        with db._read_ctx() as reader:
            assert reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
            assert not reader.in_transaction
    finally:
        db.close()


def test_held_delete_reader_is_not_reported_released(tmp_path, caplog):
    db = SessionDB(tmp_path / "state.db")
    reader = None
    try:
        # Controlled synthetic DELETE database: never change a live store.
        db._conn.execute("PRAGMA journal_mode=DELETE")
        db._wal_active = False
        db._conn.execute("PRAGMA busy_timeout=1")
        db.create_session("worker", source="test")
        reader = sqlite3.connect(f"file:{db.db_path}?mode=ro", uri=True, isolation_level=None)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM sessions").fetchall()
        with pytest.raises(sqlite3.OperationalError) as exc:
            db._execute_write(lambda c: c.execute("UPDATE sessions SET title='progress'"), patience_s=.02)
        assert reader.in_transaction
        assert "released just before" not in caplog.text
        assert "database itself is healthy" not in str(exc.value)
        assert "phase=commit" in str(exc.value)
        assert "sqlite_errorname=SQLITE_BUSY" in str(exc.value)
    finally:
        if reader is not None:
            reader.close()
        db.close()


def test_supported_runtime_reader_and_crashed_writer_processes(tmp_path):
    import subprocess, sys
    if is_sqlite_wal_reset_vulnerable(sqlite3.sqlite_version_info):
        pytest.skip("requires the repaired, supported SQLite runtime")
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("worker", source="test", model="openai-codex/gpt-6.1-sol")
    reader_code = """
import sqlite3, sys
c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True,isolation_level=None)
c.execute('BEGIN');c.execute('SELECT * FROM messages').fetchall()
print('reader-held',flush=True)
sys.stdin.readline()
assert c.in_transaction
c.close()
"""
    writer_code = """
import os, sys
from pathlib import Path
from clover_state import SessionDB
s=SessionDB(Path(sys.argv[1]))
s.append_messages_batch('worker',[{'role':'user','content':'durable worker progress'}])
os._exit(74)
"""
    reader = subprocess.Popen([sys.executable, "-c", reader_code, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert reader.stdout.readline().strip() == "reader-held"
        writer = subprocess.run([sys.executable, "-c", writer_code, str(path)], capture_output=True, text=True, timeout=15)
        assert writer.returncode == 74, writer.stderr
        assert reader.poll() is None
        reopened = SessionDB(path)
        try:
            assert reopened.get_messages_as_conversation("worker")[0]["content"] == "durable worker progress"
        finally:
            reopened.close()
    finally:
        reader.communicate("release\n", timeout=10)
        db.close()
    assert reader.returncode == 0
