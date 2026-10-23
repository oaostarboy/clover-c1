"""Late canonical saves reconcile checkpoints without replay or fake completion."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.run_agent.test_storage_progress_recovery import _history, _hold_reader
from tests.run_agent.test_tool_call_incremental_persistence import (
    _make_agent, _attach_real_session_db, _mock_tool_call, _mock_response,
)


def test_later_successful_flush_retires_the_pending_transcript(tmp_path):
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    agent._delegate_depth = 1
    agent._current_turn_id = "turn"
    history = _history()
    assert agent._flush_messages_to_session_db(history[:2]) is True
    reader = _hold_reader(db)
    try:
        assert agent._flush_messages_to_session_db(history) is False
        checkpoint = Path(agent._storage_recovery_checkpoint)
        assert json.loads(checkpoint.read_text())["disposition"] == "pending"
        reader.rollback()
        assert agent._flush_messages_to_session_db(history) is True
        receipt = json.loads(checkpoint.read_text())
        assert receipt["disposition"] == "canonical"
        assert "messages" not in receipt
        assert receipt["automatic_replay_allowed"] is False
        assert agent._flush_messages_to_session_db(history) is True
        assert [m["role"] for m in db.get_messages_as_conversation("worker")] == ["user", "assistant", "tool"]
    finally:
        reader.close()
        db.close()


def test_worker_final_save_after_abort_reports_saved_but_incomplete_without_replay(tmp_path):
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    agent._delegate_depth = 1
    db._conn.execute("PRAGMA journal_mode=DELETE")
    db._wal_active = False
    db._WRITE_PATIENCE_S = .01
    effects, readers = [], []
    agent.client.chat.completions.create.return_value = _mock_response(
        content="", finish_reason="tool_calls", tool_calls=[_mock_tool_call(call_id="effect")])
    def effect(*args, **kwargs):
        effects.append("one completed action")
        readers.append(_hold_reader(db))
        return "actual action receipt"
    def release_before_final_save(*args):
        for reader in readers:
            reader.rollback()
    try:
        with (patch("run_agent.handle_function_call", side_effect=effect),
              patch.object(agent, "_execute_tool_calls", side_effect=lambda a,m,t,api_calls=0: agent._execute_tool_calls_sequential(a,m,t)),
              patch("agent.tool_executor.maybe_persist_tool_result", side_effect=lambda **k: k["content"]),
              patch.object(agent, "_save_trajectory"),
              patch.object(agent, "_cleanup_task_resources", side_effect=release_before_final_save)):
            result = agent.run_conversation("perform one approved action")
        assert effects == ["one completed action"]
        assert agent.client.chat.completions.create.call_count == 1
        assert result["completed"] is False
        assert result["failed"] is True  # the task stopped; do not claim completion
        assert result["turn_exit_reason"] == "session_persistence_recovered"
        assert result["failure_reason"] == "session_persistence_recovered"
        assert "saved" in result["error"]
        assert "already stopped" in result["error"]
        receipt = json.loads(Path(result["storage_recovery"]["path"]).read_text())
        assert receipt["disposition"] == "canonical"
        assert "messages" not in receipt
        assert sum(m["role"] == "tool" for m in db.get_messages_as_conversation("worker")) == 1
    finally:
        for reader in readers:
            reader.close()
        db.close()


def test_shorter_successful_prefix_keeps_the_unsaved_tool_result_pending(tmp_path):
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    agent._delegate_depth = 1
    agent._current_turn_id = "turn"
    history = _history()
    assert agent._flush_messages_to_session_db(history[:2]) is True
    reader = _hold_reader(db)
    try:
        assert agent._flush_messages_to_session_db(history) is False
        checkpoint = Path(agent._storage_recovery_checkpoint)
        pending = json.loads(checkpoint.read_text())
        reader.rollback()
        # A successful no-op prefix flush does not certify the missing result.
        assert agent._flush_messages_to_session_db(history[:2]) is True
        assert json.loads(checkpoint.read_text()) == pending
        assert [m["role"] for m in db.get_messages_as_conversation("worker")] == ["user", "assistant"]
        assert agent._flush_messages_to_session_db(history) is True
        assert json.loads(checkpoint.read_text())["disposition"] == "canonical"
    finally:
        reader.close()
        db.close()


@pytest.mark.linux_only
def test_database_replacement_after_late_commit_keeps_the_pending_evidence(tmp_path):
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    agent._delegate_depth = 1
    agent._current_turn_id = "turn"
    history = _history()
    assert agent._flush_messages_to_session_db(history[:2]) is True
    reader = _hold_reader(db)
    try:
        assert agent._flush_messages_to_session_db(history) is False
        checkpoint = Path(agent._storage_recovery_checkpoint)
        pending = json.loads(checkpoint.read_text())
        reader.rollback()
        original = agent._flush_messages_to_session_db_unlocked
        def replace_after_commit(*args, **kwargs):
            result = original(*args, **kwargs)
            assert result is True
            Path(db.db_path).rename(tmp_path / "original-state.db")
            Path(db.db_path).write_bytes(b"replacement; no canonical rows")
            return result
        with patch.object(agent, "_flush_messages_to_session_db_unlocked", side_effect=replace_after_commit):
            assert agent._flush_messages_to_session_db(history) is False
        assert json.loads(checkpoint.read_text()) == pending
        assert agent._last_session_persist_confirmed is False
    finally:
        reader.close()
        db.close()


def test_older_identical_result_cannot_certify_a_new_pending_result(tmp_path):
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    agent._delegate_depth = 1
    agent._current_turn_id = "turn"
    history = _history()
    assert agent._flush_messages_to_session_db(history) is True
    # Providers can reuse call IDs. These are new objects with identical public
    # fields, not permission to treat the new result as the earlier saved row.
    history.extend({key: value for key, value in message.items() if not key.startswith("_")}
                   for message in list(history[1:]))
    reader = _hold_reader(db)
    try:
        assert agent._flush_messages_to_session_db(history) is False
        checkpoint = Path(agent._storage_recovery_checkpoint)
        pending = json.loads(checkpoint.read_text())
        reader.rollback()
        assert agent._flush_messages_to_session_db(history[:3]) is True
        assert json.loads(checkpoint.read_text()) == pending
        assert not history[-1].get("_db_persisted")
        assert len(db.get_messages_as_conversation("worker")) == 3
    finally:
        reader.close()
        db.close()
