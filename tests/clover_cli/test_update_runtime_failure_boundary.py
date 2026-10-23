"""The long-standing updater hook must not turn an unsafe repair failure green."""
from pathlib import Path
from unittest.mock import patch
import pytest
from clover_cli import managed_uv as m


def _make_db(home, name, mode):
    import sqlite3
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / name)
    conn.execute(f'PRAGMA journal_mode={mode}')
    conn.execute('CREATE TABLE IF NOT EXISTS t (x)')
    conn.execute('INSERT INTO t VALUES (1)')
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def wal_database_in_home(tmp_path, monkeypatch):
    """The refusal is only owed when a database really is in WAL mode.

    Tests that expect SystemExit(1) run against a home whose state.db is WAL.
    DELETE-mode continuation is covered in test_failed_repair_in_delete_mode_*.
    """
    home = tmp_path / 'clover-home'
    monkeypatch.setenv('CLOVER_HOME', str(home))
    _make_db(home, 'state.db', 'WAL')
    return home


@pytest.mark.parametrize('status', ['failed', 'skipped'])
def test_public_update_hook_refuses_known_vulnerable_unrepaired_runtime(tmp_path, status):
    uv=tmp_path/'uv'
    uv.write_text('synthetic',encoding='utf-8')
    repair=m.RuntimeRepairResult(status,'provisioning unavailable',sqlite_before='3.50.4')
    observed=[]
    with (patch.object(m,'resolve_uv',return_value=str(uv)),
          patch.object(m,'_uv_self_update_is_fresh',return_value=True),
          patch.object(m,'repair_vulnerable_runtime',return_value=repair)):
        with pytest.raises(SystemExit) as error:
            m.update_managed_uv(repair_observer=observed.append)
    assert error.value.code == 1
    assert observed == [repair]


@pytest.mark.parametrize('gateway_mode', [False, True])
def test_normal_command_boundary_records_runtime_failure_and_releases_lock(tmp_path, monkeypatch, gateway_mode):
    import json
    from types import SimpleNamespace
    from clover_cli import main, update_receipt as receipts
    from clover_constants import get_clover_home
    from clover_cli.update_lock import UpdateLock

    uv=tmp_path/'uv'
    uv.write_text('synthetic',encoding='utf-8')
    repair=m.RuntimeRepairResult('failed','provisioning unavailable',sqlite_before='3.50.4')
    monkeypatch.setattr('clover_cli.config.is_managed', lambda: False)
    monkeypatch.setattr('clover_cli.update_contract.evaluate_update_admission', lambda root: None)
    monkeypatch.setattr('clover_cli.update_preflight.gate_cli_update', lambda *a,**kw: None)
    monkeypatch.setattr(main,'_install_hangup_protection',lambda **kw: None)
    monkeypatch.setattr(main,'_finalize_update_output',lambda state: None)
    monkeypatch.setattr('clover_cli.update_restart_watcher.wait_for_cli_verdict',lambda: True)
    monkeypatch.setattr(m,'resolve_uv',lambda: str(uv))
    monkeypatch.setattr(m,'_uv_self_update_is_fresh',lambda: True)
    monkeypatch.setattr(m,'repair_vulnerable_runtime',lambda uv: repair)
    def apply(args,gateway_mode):
        receipts.begin_update_receipt()
        m.update_managed_uv()
        pytest.fail('a failed runtime must not reach restart/completion')
    monkeypatch.setattr(main,'_cmd_update_impl',apply)
    with pytest.raises(SystemExit) as error:
        main.cmd_update(SimpleNamespace(gateway=gateway_mode))
    assert error.value.code == 1
    receipt=json.loads((get_clover_home()/'logs/update_receipts/latest.json').read_text(encoding='utf-8'))
    assert receipt['outcome'] == 'failed'
    assert any(step['name'] == 'sqlite_runtime' and step['ok'] is False for step in receipt['steps'])
    lock=UpdateLock()
    assert lock.acquire()
    lock.release()


def test_failed_bootstrap_reports_runtime_failure_once(tmp_path, capsys):
    uv=tmp_path/'uv'
    uv.write_text('synthetic',encoding='utf-8')
    repair=m.RuntimeRepairResult('failed','probe unavailable')
    from types import SimpleNamespace
    with (patch.object(m,'resolve_uv',side_effect=[None,None,str(uv)]),
          patch.object(m,'managed_uv_path',return_value=uv),
          patch.object(m,'_install_uv'),
          patch.object(m,'repair_vulnerable_runtime',return_value=repair),
          patch.object(m.subprocess,'run',return_value=SimpleNamespace(stdout='uv fixture'))):
        assert m.update_managed_uv() == str(uv)
    assert capsys.readouterr().out.count('Managed Python runtime was not replaced') == 1


def test_unproved_runtime_ownership_refuses_update_without_executing_foreign_python(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr(sqlite3, 'sqlite_version', '3.50.4')
    uv=tmp_path/'uv';uv.write_text('fixture',encoding='utf-8')
    repair=m.RuntimeRepairResult('refused','virtual environment ownership could not be established')
    with (patch.object(m,'resolve_uv',return_value=str(uv)),
          patch.object(m,'_uv_self_update_is_fresh',return_value=True),
          patch.object(m,'repair_vulnerable_runtime',return_value=repair),
          patch.object(m,'probe_sqlite_runtime',side_effect=AssertionError('do not execute an unowned interpreter'))):
        with pytest.raises(SystemExit) as error:
            m.update_managed_uv()
    assert error.value.code == 1


def test_public_update_hook_keeps_unknown_separate_from_known_unsafe(tmp_path):
    uv=tmp_path/'uv'
    uv.write_text('synthetic',encoding='utf-8')
    repair=m.RuntimeRepairResult('skipped','probe unavailable')
    with (patch.object(m,'resolve_uv',return_value=str(uv)),
          patch.object(m,'_uv_self_update_is_fresh',return_value=True),
          patch.object(m,'repair_vulnerable_runtime',return_value=repair)):
        assert m.update_managed_uv() == str(uv)


def test_public_update_bootstrap_cannot_hide_unrepaired_runtime(tmp_path):
    uv=tmp_path/'uv'
    repair=m.RuntimeRepairResult('failed','provisioning unavailable',sqlite_before='3.50.4')
    def bootstrap(**kwargs):
        observer=kwargs.get('repair_observer')
        assert observer is not None
        observer(repair)
        return str(uv)
    with (patch.object(m,'resolve_uv',return_value=None),
          patch.object(m,'ensure_uv',side_effect=bootstrap)):
        with pytest.raises(SystemExit) as error:
            m.update_managed_uv()
    assert error.value.code == 1


@pytest.mark.parametrize('failure', ['exception', 'no-uv'])
def test_unexpected_repair_failure_rechecks_actual_target_before_success(tmp_path, failure):
    from tests.clover_cli.test_managed_uv import _runtime_info
    uv=tmp_path/'uv'
    uv.write_text('synthetic',encoding='utf-8')
    info=_runtime_info(Path('target-python'), (3,50,4))
    with (patch.object(m,'resolve_uv',return_value=None if failure == 'no-uv' else str(uv)),
          patch.object(m,'_uv_self_update_is_fresh',return_value=True),
          patch.object(m,'repair_vulnerable_runtime',side_effect=OSError('synthetic failure')),
          patch.object(m,'ensure_uv',return_value=None),
          patch.object(m,'probe_sqlite_runtime',return_value=info)):
        with pytest.raises(SystemExit) as error:
            m.update_managed_uv()
    assert error.value.code == 1


def test_unrecognized_layout_does_not_hide_known_vulnerable_interpreter(tmp_path):
    from tests.clover_cli.test_managed_uv import _runtime_info
    uv=tmp_path/'uv'
    uv.write_text('synthetic',encoding='utf-8')
    info=_runtime_info(Path('target-python'), (3,50,4))
    with (patch.object(m,'resolve_uv',return_value=str(uv)),
          patch.object(m,'_uv_self_update_is_fresh',return_value=True),
          patch.object(m,'repair_vulnerable_runtime',return_value=m.RuntimeRepairResult('not-applicable')),
          patch.object(m,'probe_sqlite_runtime',return_value=info)):
        with pytest.raises(SystemExit) as error:
            m.update_managed_uv()
    assert error.value.code == 1


@pytest.mark.parametrize('status', ['failed', 'skipped'])
def test_failed_repair_in_delete_mode_is_non_fatal(tmp_path, wal_database_in_home, status, capsys):
    """Offline / no-uv user on an old SQLite whose databases are already DELETE mode."""
    import sqlite3
    home = wal_database_in_home
    (home / 'state.db').unlink()
    for leftover in home.glob('state.db-*'):
        leftover.unlink()
    _make_db(home, 'state.db', 'DELETE')
    _make_db(home, 'response_store.db', 'DELETE')
    uv = tmp_path / 'uv'
    uv.write_text('synthetic', encoding='utf-8')
    repair = m.RuntimeRepairResult(status, 'provisioning unavailable', sqlite_before='3.50.4')
    with (patch.object(m, 'resolve_uv', return_value=str(uv)),
          patch.object(m, '_uv_self_update_is_fresh', return_value=True),
          patch.object(m, 'repair_vulnerable_runtime', return_value=repair)):
        assert m.update_managed_uv() == str(uv)
    out = capsys.readouterr().out
    assert 'Continuing' in out and 'next `clover update` will retry' in out
    assert 'Update incomplete' not in out
    # the data is untouched and still readable
    assert sqlite3.connect(home / 'state.db').execute('select x from t').fetchone() == (1,)


def test_failed_repair_still_refuses_when_any_database_is_wal(tmp_path, wal_database_in_home):
    _make_db(wal_database_in_home / 'plugin-data' / 'p', 'data.db', 'WAL')
    (wal_database_in_home / 'state.db').unlink()
    for leftover in wal_database_in_home.glob('state.db-*'):
        leftover.unlink()
    _make_db(wal_database_in_home, 'state.db', 'DELETE')
    assert m._clover_databases_in_wal_mode() == ['plugin-data/p/data.db']
    uv = tmp_path / 'uv'
    uv.write_text('synthetic', encoding='utf-8')
    repair = m.RuntimeRepairResult('failed', 'provisioning unavailable', sqlite_before='3.50.4')
    with (patch.object(m, 'resolve_uv', return_value=str(uv)),
          patch.object(m, '_uv_self_update_is_fresh', return_value=True),
          patch.object(m, 'repair_vulnerable_runtime', return_value=repair)):
        with pytest.raises(SystemExit) as error:
            m.update_managed_uv()
    assert error.value.code == 1


def test_unprovable_database_mode_keeps_the_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(m, '_clover_databases_in_wal_mode',
                        lambda: (_ for _ in ()).throw(RuntimeError('cannot inspect')))
    repair = m.RuntimeRepairResult('failed', 'x', sqlite_before='3.50.4')
    assert m._repair_failure_is_safe_to_continue(repair) is False


def test_header_probe_does_not_modify_databases(wal_database_in_home):
    before = {p.name: p.read_bytes() for p in wal_database_in_home.glob('state.db*')}
    assert m._clover_databases_in_wal_mode() == ['state.db']
    assert {p.name: p.read_bytes() for p in wal_database_in_home.glob('state.db*')} == before


def test_plugin_storage_honors_the_vulnerable_sqlite_fallback(tmp_path, monkeypatch):
    """plugin_db used to hard-set WAL; on a vulnerable SQLite it must stay DELETE."""
    import clover_state
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    monkeypatch.setattr(clover_state, 'is_sqlite_wal_reset_vulnerable', lambda *a, **k: True)
    from plugins.plugin_storage import plugin_db
    conn = plugin_db('demo')
    try:
        assert conn.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'delete'
    finally:
        conn.close()


def test_kanban_connect_uses_the_vulnerable_sqlite_fallback():
    import inspect
    from clover_cli import kanban_db
    assert 'apply_wal_with_fallback' in inspect.getsource(kanban_db)
