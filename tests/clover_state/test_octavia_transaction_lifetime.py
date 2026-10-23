"""Octavia's actual DELETE runtime: real cross-connection locks and cleanup."""
import sqlite3
import time
import pytest
from clover_state import SessionDB

@pytest.fixture
def store(tmp_path, monkeypatch):
    # Select the existing supported rollback-journal setting before any open.
    # SQLite transactions themselves are real and unmocked.
    monkeypatch.setattr('clover_state.resolve_journal_mode', lambda: 'delete')
    db = SessionDB(tmp_path/'state.db')
    assert db._conn is not None
    db.create_session('worker',source='test')
    other = SessionDB(db.db_path)
    assert other._conn is not None
    assert db._conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    assert other._conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    assert db._wal_active is other._wal_active is False
    db._conn.execute('PRAGMA busy_timeout=1')
    other._conn.execute('PRAGMA busy_timeout=1')
    try:
        yield db, other
    finally:
        other.close()
        db.close()

@pytest.mark.parametrize('holder',['reader','writer'])
def test_real_same_process_blocker_phase_and_rollback(store,holder):
    db, other = store
    other._conn.execute('BEGIN' if holder == 'reader' else 'BEGIN IMMEDIATE')
    other._conn.execute('SELECT * FROM sessions').fetchall()
    attempts=[]
    def body(conn):
        attempts.append(True)
        conn.execute("INSERT INTO state_meta(key,value) VALUES('blocked','redacted-value')")
    start=time.monotonic()
    with pytest.raises(sqlite3.OperationalError) as error:
        db._execute_write(body,patience_s=.025)
    assert time.monotonic()-start < 1.5
    assert not db._conn.in_transaction
    assert other._conn.in_transaction
    assert db.get_meta('blocked') is None
    assert bool(attempts) is (holder == 'reader')
    assert 'phase='+('commit' if holder == 'reader' else 'begin') in str(error.value)
    assert 'redacted-value' not in str(error.value)
    other._conn.rollback()
    db._execute_write(body,patience_s=.025)
    assert db.get_meta('blocked') == 'redacted-value'
    assert other.get_meta('blocked') == 'redacted-value'

@pytest.mark.parametrize('error_class',[ValueError,KeyboardInterrupt])
def test_transaction_body_failure_releases_cross_connection_writer(store,error_class):
    db, other=store
    def body(conn):
        conn.execute("INSERT INTO state_meta(key,value) VALUES('partial','must-rollback')")
        raise error_class('synthetic failure')
    with pytest.raises(error_class):
        db._execute_write(body,patience_s=.025)
    assert not db._conn.in_transaction
    assert other.get_meta('partial') is None
    other.set_meta('after','durable')
    assert db.get_meta('after') == 'durable'

def test_history_and_search_leave_no_transaction_across_external_work(store):
    db, other=store
    db.append_messages_batch('worker',[{'role':'user','content':'synthetic history query'}])
    assert db.get_messages_as_conversation('worker')
    assert not db._conn.in_transaction
    db.search_messages('synthetic')
    assert not db._conn.in_transaction
    # This connection represents work reached after read/search returned.
    other.set_meta('external-boundary','no-lock')
    assert db.get_meta('external-boundary') == 'no-lock'
