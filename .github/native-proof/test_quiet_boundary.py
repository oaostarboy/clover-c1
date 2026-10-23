"""The maintenance verifier must require a real ACK, not exact undecorated text."""
from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3

import pytest

spec = importlib.util.spec_from_file_location('quiet_boundary_probe', Path(__file__).with_name('quiet_boundary.py'))
quiet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quiet)


@pytest.mark.parametrize('state,expected', [('attempting', False), ('failed', False), ('delivered', True)])
def test_decorated_payload_still_requires_actual_ack(tmp_path, state, expected):
    path = tmp_path / 'state.db'
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE delivery_obligations (obligation_id TEXT,state TEXT,attempts INTEGER,owner_pid INTEGER,content TEXT)')
        db.execute('INSERT INTO delivery_obligations VALUES (?,?,?,?,?)', ('row', state, 0, 4321, 'hello!\n\nTEST_REPLY_TOKEN'))
        db.commit()
    before = path.read_bytes()
    result = quiet.read_acknowledged_boundary(tmp_path, 'TEST_REPLY_TOKEN')
    assert bool(result) is expected
    if expected:
        assert result['state'] == 'delivered' and result['owner_pid'] == 4321
    assert path.read_bytes() == before


def test_other_uncertain_row_blocks_boundary(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'state.db')) as db:
        db.execute('CREATE TABLE delivery_obligations (obligation_id TEXT,state TEXT,attempts INTEGER,owner_pid INTEGER,content TEXT)')
        db.executemany('INSERT INTO delivery_obligations VALUES (?,?,?,?,?)', [('row', 'delivered', 0, 4321, 'TEST_REPLY_TOKEN'), ('other', 'attempting', 0, 4321, 'other reply')])
        db.commit()
    assert quiet.read_acknowledged_boundary(tmp_path, 'TEST_REPLY_TOKEN') is None


def test_missing_database_is_not_quiescence(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        quiet.read_acknowledged_boundary(tmp_path, 'TEST_REPLY_TOKEN')
    assert not (tmp_path / 'state.db').exists()
