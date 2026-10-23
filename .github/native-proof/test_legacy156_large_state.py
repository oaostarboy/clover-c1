"""Portable verifier checks; these do not certify native updater execution."""
import importlib.util
from pathlib import Path
import sqlite3
from contextlib import closing

import pytest

spec=importlib.util.spec_from_file_location('large_state',Path(__file__).with_name('legacy156_large_state.py'))
large=importlib.util.module_from_spec(spec)
spec.loader.exec_module(large)


def test_grow_keeps_existing_committed_rows_and_creates_real_pages(tmp_path, monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    monkeypatch.setenv('CLOVER_HOME',str(tmp_path))
    evidence=tmp_path/'evidence';evidence.mkdir()
    with closing(sqlite3.connect(tmp_path/'state.db')) as db:
        db.execute('CREATE TABLE committed(value TEXT)')
        db.execute("INSERT INTO committed VALUES('newest real row')")
        db.commit()
    result=large.grow(tmp_path,evidence,size=200_000)
    assert result['bytes']>=200_000
    with closing(sqlite3.connect(tmp_path/'state.db')) as db:
        assert db.execute('SELECT value FROM committed').fetchall()==[('newest real row',)]
        assert db.execute('SELECT sum(length(payload)) FROM native_startup_padding').fetchone()[0]>0
        assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'


def test_native_mutation_rejects_non_hosted_and_foreign_home(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS',raising=False)
    monkeypatch.setenv('CLOVER_HOME',str(tmp_path))
    with pytest.raises(AssertionError):large.grow(tmp_path,tmp_path,size=100)
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    with pytest.raises(AssertionError):large.grow(tmp_path/'foreign',tmp_path,size=100)
    assert not (tmp_path/'state.db').exists()


def test_health_watch_does_not_certify_absent_candidate(tmp_path,monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    monkeypatch.setenv('CLOVER_HOME',str(tmp_path))
    with pytest.raises(TimeoutError):large.watch(tmp_path,tmp_path,'f'*40,timeout=0)
    assert not (tmp_path/'large-state-first-health.json').exists()
