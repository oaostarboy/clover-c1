"""An offline repair exception needs a complete, readable rollback-journal inventory."""
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from clover_cli import managed_uv as m


@pytest.mark.parametrize('relative', [
    'cron/executions.db', 'cron/notepad.db', 'memory/memory_store.db',
    'profiles/p/plugin-data/demo/data.db', 'cache/nested/metrics.sqlite3'])
def test_nested_wal_store_blocks_offline_continuation(monkeypatch, tmp_path, relative):
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    path = tmp_path/relative
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE evidence(x)')
    conn.close()
    repair = m.RuntimeRepairResult('failed', 'offline', sqlite_before='3.50.4')
    assert m._repair_failure_is_safe_to_continue(repair) is False


def test_delete_mode_does_not_waive_runtime_ownership(monkeypatch, tmp_path):
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    monkeypatch.setattr(sqlite3, 'sqlite_version', '3.50.4')
    conn = sqlite3.connect(tmp_path/'state.db')
    conn.execute('CREATE TABLE saved(x)')
    conn.close()
    repair = m.RuntimeRepairResult('refused', 'runtime ownership unverified')
    with pytest.raises(SystemExit) as exc:
        m._finish_update_runtime_attempt(repair, None)
    assert exc.value.code == 1


@pytest.mark.parametrize('store', ['session', 'delivery', 'executions', 'notepad',
                                  'projects', 'kanban', 'plugin', 'verification', 'response', 'discord'])
def test_builtin_store_openers_do_not_force_wal_on_vulnerable_runtime(monkeypatch, tmp_path, store):
    import clover_state
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    monkeypatch.setattr(clover_state, 'is_sqlite_wal_reset_vulnerable', lambda *a, **kw: True)
    if store == 'session':
        db = clover_state.SessionDB(tmp_path/'state.db')
        try:
            assert db._conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        finally:
            db.close()
        return
    path = tmp_path/(store+'.db')
    if store == 'projects':
        from clover_cli import projects_db
        conn = projects_db.connect(path)
    elif store == 'kanban':
        if tmp_path.is_relative_to(Path.home()/'.clover'):
            pytest.skip('kanban live-root guard intentionally rejects this worktree-contained temp root; execute on hosted temp root')
        from clover_cli import kanban_db
        conn = kanban_db.connect(path)
    elif store == 'plugin':
        from plugins.plugin_storage import plugin_db
        conn = plugin_db('offline-proof')
    elif store == 'response':
        from gateway.platforms.api_server import ResponseStore
        db = ResponseStore(db_path=str(path))
        conn = db._conn
    else:
        conn = sqlite3.connect(path)
        if store == 'discord':
            from plugins.platforms.discord.recovery import DiscordRecoveryStore
            DiscordRecoveryStore(tmp_path)._initialize(conn)
        else:
            from gateway import delivery_ledger
            from cron import executions, notepad
            from agent import verification_evidence
            initializer = {'delivery':delivery_ledger._initialize_schema,
                           'executions':executions._initialize_schema,
                           'notepad':notepad._initialize_schema,
                           'verification':verification_evidence._ensure_schema}[store]
            initializer(conn)
    with closing(conn):
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'



@pytest.mark.parametrize('header', [b'', b'not sqlite', b'SQLite format 3\x00'+b'\x00'*84])
def test_unknown_journal_header_is_not_a_delete_mode_receipt(monkeypatch, tmp_path, header):
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    (tmp_path/'state.db').write_bytes(header)
    repair = m.RuntimeRepairResult('failed', 'offline', sqlite_before='3.50.4')
    assert m._repair_failure_is_safe_to_continue(repair) is False


def test_unreadable_store_directory_is_not_an_empty_safe_inventory(monkeypatch, tmp_path):
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    hidden=tmp_path/'cron';hidden.mkdir()
    conn=sqlite3.connect(hidden/'executions.db')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE evidence(x)')
    conn.close()
    hidden.chmod(0)
    try:
        try:
            list(hidden.iterdir())
        except PermissionError:
            pass
        else:
            pytest.skip('host can read chmod(000) directories; native permission failure needs unprivileged runner')
        repair=m.RuntimeRepairResult('failed','offline',sqlite_before='3.50.4')
        assert m._repair_failure_is_safe_to_continue(repair) is False
    finally:
        hidden.chmod(0o700)


@pytest.mark.parametrize('kind', ['custom-name', 'linked-directory'])
def test_plugin_store_inventory_cannot_omit_valid_wal_files(monkeypatch, tmp_path, kind):
    home=tmp_path/'owner';home.mkdir()
    monkeypatch.setenv('CLOVER_HOME',str(home))
    if kind=='custom-name':
        path=home/'plugin-data'/'demo'/'custom-store'
        path.parent.mkdir(parents=True)
    else:
        outside=tmp_path/'external';outside.mkdir()
        (home/'plugin-data').symlink_to(outside,target_is_directory=True)
        path=outside/'data.db'
    conn=sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE evidence(x)')
    conn.close()
    assert m._repair_failure_is_safe_to_continue(
        m.RuntimeRepairResult('failed','offline',sqlite_before='3.50.4')) is False


def test_inventory_limit_cannot_hide_later_wal_store(monkeypatch, tmp_path):
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    real_glob = type(tmp_path).glob
    monkeypatch.setattr(type(tmp_path), 'glob', lambda path, pattern: iter(sorted(real_glob(path, pattern))))
    for i in range(202):
        path=tmp_path/f'a{i:03}.db'
        conn=sqlite3.connect(path)
        conn.execute('CREATE TABLE evidence(x)')
        conn.close()
    conn=sqlite3.connect(tmp_path/'z-wal.db')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE evidence(x)')
    conn.close()
    repair = m.RuntimeRepairResult('failed', 'offline', sqlite_before='3.50.4')
    assert m._repair_failure_is_safe_to_continue(repair) is False
