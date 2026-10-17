"""Durable allowance/consent owners, isolated files only."""
import importlib.util
import json
import sqlite3
import pytest


def watcher():
    assert importlib.util.find_spec('cron.inbox_watcher'), 'missing durable watcher owner'
    from cron import inbox_watcher
    return inbox_watcher


def test_durable_pre_dispatch_allowance_never_refunds_or_rolls_back_clock(tmp_path):
    w = watcher()
    store = w.Store(tmp_path / 'watcher.db', create=True)
    limits = w.Limits(attempts_day=2)
    store.reserve(limits, now=172800, run_id='a', input_units=100)
    store.reserve(limits, now=172801, run_id='b', input_units=100)
    with pytest.raises(w.Denied):
        store.reserve(limits, now=172802, run_id='c', input_units=100)
    restarted = w.Store(tmp_path / 'watcher.db')
    with pytest.raises(w.Denied):
        restarted.reserve(limits, now=86400, run_id='d', input_units=100)
    restarted.reserve(limits, now=259200, run_id='e', input_units=100)
    with pytest.raises(w.Denied):
        restarted.reserve(limits, now=259200, run_id='e', input_units=100)
    assert restarted.usage()['attempts'] == 1
    assert restarted.usage()['input_units'] == 100
    assert restarted.usage()['output_tokens'] == limits.output_tokens


def test_corrupt_missing_and_zero_budget_fail_closed(tmp_path):
    w = watcher()
    with pytest.raises(w.Denied): w.Store(tmp_path/'absent.db')
    p = tmp_path/'bad.db'; p.write_bytes(b'corrupted')
    with pytest.raises(w.Denied): w.Store(p)
    s = w.Store(tmp_path/'valid.db', create=True)
    with pytest.raises(w.Denied): s.reserve(w.Limits(attempts_day=0), now=172800, run_id='a', input_units=10)
    with sqlite3.connect(s.path) as c:
        c.execute("UPDATE state SET body=? WHERE id=1", (json.dumps({'budget': []}),))
    with pytest.raises(w.Denied): s.usage()
