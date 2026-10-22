"""Checkpoint lifetime contracts; real commits, private synthetic state only."""
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

import agent.storage_recovery as recovery
from tests.run_agent.test_storage_progress_recovery import (
    _make_agent, _attach_real_session_db, _history, _hold_reader, ReleaseAfterFailure,
)


@pytest.fixture
def blocked_result(tmp_path):
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / 'state.db', 'worker')
    agent._delegate_depth = 1
    agent._current_turn_id = 'approved-turn'
    history = _history()
    history[-1]['content'] = 'SYNTHETIC private effect result'
    assert agent._flush_messages_to_session_db(history[:2]) is True
    reader = _hold_reader(db)
    release = ReleaseAfterFailure(reader)
    log = logging.getLogger('run_agent')
    log.addHandler(release)
    try:
        yield agent, db, history, release
    finally:
        log.removeHandler(release)
        reader.close()
        db.close()


def test_canonical_receipt_retires_transcript_before_session_deletion(blocked_result, tmp_path):
    agent, db, history, release = blocked_result
    assert agent._flush_messages_to_session_db(history) is True
    assert release.seen
    path = Path(agent._storage_recovery_checkpoint)
    receipt = json.loads(path.read_text())
    assert receipt == {
        'schema': 1, 'session_id': 'worker', 'turn_id': 'approved-turn',
        'model': agent.model, 'provider': agent.provider,
        'database_identity': [Path(db.db_path).stat().st_dev, Path(db.db_path).stat().st_ino],
        'disposition': 'canonical', 'automatic_replay_allowed': False,
    }
    assert db.get_messages_as_conversation('worker')[-1]['content'] == history[-1]['content']
    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    assert db.delete_session('worker', sessions_dir=sessions)
    assert db.get_session('worker') is None
    assert json.loads(path.read_text()) == receipt  # stable diagnostic identity
    assert all(history[-1]['content'] not in p.read_text()
               for p in path.parent.glob('*.json'))


def test_canonical_receipt_write_failure_keeps_uncertain_pending_evidence(blocked_result):
    agent, db, history, release = blocked_result
    write = recovery._write_checkpoint
    def fail_receipt(path, payload):
        if payload['disposition'] == 'canonical':
            raise OSError('synthetic canonical receipt failure')
        return write(path, payload)
    with patch.object(recovery, '_write_checkpoint', fail_receipt):
        assert agent._flush_messages_to_session_db(history) is True
    path = Path(agent._storage_recovery_checkpoint)
    pending = json.loads(path.read_text())
    assert pending['disposition'] == 'pending'
    assert pending['messages'][-1]['content'] == history[-1]['content']
    assert pending['automatic_replay_allowed'] is False
    assert agent._flush_messages_to_session_db(history) is True
    assert sum(m['role'] == 'tool' for m in db.get_messages_as_conversation('worker')) == 1
    assert json.loads(path.read_text()) == pending  # no loader or replay/cleanup authority
