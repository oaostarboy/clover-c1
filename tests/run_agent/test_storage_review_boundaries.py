"""Reviewed boundaries: saved work, Stop, and safe persistence explanations."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from run_agent import AIAgent
from tests.run_agent.test_storage_checkpoint_lifetime import blocked_result


@pytest.mark.parametrize("cause", ["locked", "disk", "corrupt", "compression", "turn_lease", "unknown"])
def test_persistence_guidance_never_invites_replay_of_completed_actions(cause):
    text = AIAgent._format_turn_completion_explanation("session_persistence_failed", cause)
    assert "send your message again" not in text
    assert "send it again" not in text
    assert "before retrying" in text


def test_stop_after_recovery_commit_retires_payload_without_authorizing_continuation(blocked_result):
    agent, db, history, release = blocked_result
    original = agent._flush_messages_to_session_db_unlocked
    def stop_after_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("_recovery_patience_s") is not None and result is True:
            agent._interrupt_requested = True
        return result
    with patch.object(agent, "_flush_messages_to_session_db_unlocked", side_effect=stop_after_commit):
        assert agent._flush_messages_to_session_db(history) is False
    assert release.seen
    assert agent._interrupt_requested is True
    checkpoint = json.loads(Path(agent._storage_recovery_checkpoint).read_text())
    assert checkpoint["disposition"] == "canonical"
    assert "messages" not in checkpoint
    assert checkpoint["automatic_replay_allowed"] is False
    assert db.get_messages_as_conversation("worker")[-1]["content"] == history[-1]["content"]
    assert agent._last_session_persist_confirmed is True


def test_real_worker_stop_after_recovery_commit_is_interrupted_not_unsaved(tmp_path):
    import logging
    from tests.run_agent.test_storage_progress_recovery import _hold_reader, ReleaseAfterFailure
    from tests.run_agent.test_tool_call_incremental_persistence import (
        _make_agent, _attach_real_session_db, _mock_tool_call, _mock_response,
    )
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    agent._delegate_depth = 1
    db._conn.execute("PRAGMA journal_mode=DELETE")
    db._wal_active = False
    db._WRITE_PATIENCE_S = .01
    readers, handlers, effects = [], [], []
    log = logging.getLogger("run_agent")
    agent.client.chat.completions.create.return_value = _mock_response(
        content="", finish_reason="tool_calls", tool_calls=[_mock_tool_call(call_id="effect")])
    original = agent._flush_messages_to_session_db_unlocked
    def stop_after_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("_recovery_patience_s") is not None and result is True:
            agent._interrupt_requested = True
        return result
    def effect(*args, **kwargs):
        effects.append("one synthetic action")
        reader = _hold_reader(db)
        handler = ReleaseAfterFailure(reader)
        readers.append(reader); handlers.append(handler); log.addHandler(handler)
        return "real receipt"
    try:
        with (patch.object(agent, "_flush_messages_to_session_db_unlocked", side_effect=stop_after_commit),
              patch("run_agent.handle_function_call", side_effect=effect),
              patch.object(agent, "_execute_tool_calls", side_effect=lambda a,m,t,api_calls=0: agent._execute_tool_calls_sequential(a,m,t)),
              patch("agent.tool_executor.maybe_persist_tool_result", side_effect=lambda **k: k["content"]),
              patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources")):
            result = agent.run_conversation("perform one approved action")
        assert effects == ["one synthetic action"]
        assert agent.client.chat.completions.create.call_count == 1
        assert result["interrupted"] is True
        assert result["failed"] is False
        assert result["completed"] is False
        assert result["turn_exit_reason"] != "session_persistence_failed"
        checkpoint = json.loads(Path(result["storage_recovery"]["path"]).read_text())
        assert checkpoint["disposition"] == "canonical"
        assert "messages" not in checkpoint
    finally:
        for handler in handlers: log.removeHandler(handler)
        for reader in readers: reader.close()
        db.close()


@pytest.mark.linux_only
def test_database_replacement_after_recovery_commit_keeps_uncertain_payload(blocked_result, tmp_path):
    agent, db, history, release = blocked_result
    original = agent._flush_messages_to_session_db_unlocked
    def replace_after_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("_recovery_patience_s") is not None and result is True:
            Path(db.db_path).rename(tmp_path / "original-state.db")
            Path(db.db_path).write_bytes(b"synthetic replacement; no original canonical rows")
        return result
    with patch.object(agent, "_flush_messages_to_session_db_unlocked", side_effect=replace_after_commit):
        assert agent._flush_messages_to_session_db(history) is False
    checkpoint = json.loads(Path(agent._storage_recovery_checkpoint).read_text())
    assert checkpoint["disposition"] == "pending"
    assert checkpoint["messages"][-1]["content"] == history[-1]["content"]
    assert agent._last_session_persist_confirmed is False
