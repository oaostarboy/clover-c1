"""Real old/new-interpreter openers during the normal update boundary."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest
import clover_state as state

pytestmark = pytest.mark.skipif(
    state.is_sqlite_wal_reset_vulnerable(),
    reason='positive WAL transition needs a supported SQLite runtime',
)


@pytest.mark.parametrize('obligation', ['live-update', 'fleet-restart'])
@pytest.mark.parametrize('require_wal', [False, True])
def test_update_boundary_defers_delete_to_wal_until_old_holder_exits(
    tmp_path, monkeypatch, request, obligation, require_wal
):
    home=tmp_path/'own-home';home.mkdir()
    monkeypatch.setenv('CLOVER_HOME',str(home))
    db=home/'state.db'
    conn=sqlite3.connect(db)
    conn.executescript('CREATE TABLE proof(value TEXT); INSERT INTO proof VALUES ("preserved");')
    conn.commit()
    old_python=os.environ.get('CLOVER_TEST_OLD_PYTHON',sys.executable)
    old=subprocess.Popen([old_python,'-I','-c',
        'import json,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); '
        'assert c.execute("SELECT value FROM proof").fetchone()==("preserved",); '
        'print(json.dumps({"sqlite":sqlite3.sqlite_version,"transaction":c.in_transaction}),flush=True); '
        'sys.stdin.readline(); c.close()',str(db)],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    marker=home/('.clover-update-in-progress' if obligation=='live-update' else 'fleet_restart_pending')
    try:
        version=json.loads(old.stdout.readline())
        request.node.user_properties.append(('holding_sqlite_version',version['sqlite']))
        request.node.user_properties.append(('new_sqlite_version',sqlite3.sqlite_version))
        assert version['transaction'] is False
        marker.write_text(f'{old.pid}\n{time.time()}\n',encoding='utf-8')
        if require_wal:
            with pytest.raises(state.WalUnsupportedError):
                state.apply_wal_with_fallback(conn,db_label='state',require_wal=True)
        else:
            assert state.apply_wal_with_fallback(conn,db_label='state') == 'delete'
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert old.poll() is None
        assert conn.execute('SELECT value FROM proof').fetchone() == ('preserved',)
    finally:
        old.stdin.close()
        assert old.wait(timeout=10) == 0, old.stderr.read()
        marker.unlink(missing_ok=True)
    assert state.apply_wal_with_fallback(conn,db_label='state') == 'wal'
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert conn.execute('SELECT value FROM proof').fetchone() == ('preserved',)
    conn.close()


def test_foreign_home_restart_marker_does_not_control_this_database(tmp_path, monkeypatch):
    own=tmp_path/'own';own.mkdir();other=tmp_path/'other';other.mkdir()
    marker=other/'fleet_restart_pending';marker.write_text('foreign obligation\n')
    monkeypatch.setenv('CLOVER_HOME',str(own))
    conn=sqlite3.connect(own/'state.db')
    conn.execute('CREATE TABLE proof(value TEXT)');conn.commit()
    assert state.apply_wal_with_fallback(conn,db_label='state') == 'wal'
    assert marker.read_text() == 'foreign obligation\n'
    conn.close()
