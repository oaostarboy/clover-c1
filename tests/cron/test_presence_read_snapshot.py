"""A read fence must return bytes from the snapshot it fully validated."""
import sqlite3

import pytest

from agent.presence_state import ContextKey, PresenceStoreError
from agent.workstreams import Scope
from cron import presence


def test_runtime_read_does_not_mix_validated_rows_with_later_unvalidated_commit(monkeypatch, tmp_path):
    store = presence.ThoughtStore()
    key = ContextKey(Scope(str(tmp_path), '/project', 'private:telegram', 'telegram'), 'root')
    generation = store.advance_generation()
    # WAL permits a real second connection to commit while a reader is open.
    with sqlite3.connect(store.path) as writer:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    validate = presence._validate_records
    changes = []
    def race(conn):
        validate(conn)
        if changes:
            return
        with sqlite3.connect(store.path) as writer:
            writer.execute('UPDATE presence_epoch SET generation=-1 WHERE id=1')
        changes.append(True)
    with monkeypatch.context() as mp:
        mp.setattr(presence, '_validate_records', race)
        assert store.runtime_state(key) == (key, generation, (0, False))
    assert changes == [True]
    # A new observation must reject the committed corruption, not reuse state.
    before = store.path.read_bytes()
    with pytest.raises(PresenceStoreError, match='invalid shared generation'):
        store.runtime_state(key)
    assert store.path.read_bytes() == before
