"""A final save failure must not turn completed effects into a success receipt."""
import json
from unittest.mock import patch

import pytest

from tests.run_agent.test_tool_call_incremental_persistence import (
    _make_agent, _attach_real_session_db, _mock_tool_call, _mock_response,
)
from tests.run_agent.test_storage_progress_recovery import _hold_reader


@pytest.mark.parametrize("delegate_depth", [0, 1])
def test_final_text_commit_failure_is_not_completed_or_replayed(tmp_path, delegate_depth):
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    agent._delegate_depth = delegate_depth
    db._conn.execute("PRAGMA journal_mode=DELETE")
    db._wal_active = False
    db._WRITE_PATIENCE_S = .01
    readers = []
    effects = tmp_path / "synthetic-effects.jsonl"

    def effect(*args, **kwargs):
        assert not db._conn.in_transaction
        with effects.open("a") as handle:
            handle.write(json.dumps({"tool": "synthetic-external-action", "count": 1}) + "\n")
        return "external action completed once"

    def provider(*args, **kwargs):
        assert not db._conn.in_transaction
        if not effects.exists():
            return _mock_response(content="", finish_reason="tool_calls", tool_calls=[_mock_tool_call()])
        readers.append(_hold_reader(db))
        return _mock_response(content="The external action completed once.", finish_reason="stop")

    agent.client.chat.completions.create.side_effect = provider
    try:
        with (patch("run_agent.handle_function_call", side_effect=effect),
              patch.object(agent, "_execute_tool_calls", side_effect=lambda a, m, t, api_calls=0: agent._execute_tool_calls_sequential(a, m, t)),
              patch("agent.tool_executor.maybe_persist_tool_result", side_effect=lambda **k: k["content"]),
              patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources")):
            result = agent.run_conversation("perform the approved synthetic external action")
        assert effects.read_text().count("synthetic-external-action") == 1
        assert agent.client.chat.completions.create.call_count == 2
        assert readers and readers[0].in_transaction
        durable = db.get_messages_as_conversation("worker")
        assert [m["role"] for m in durable] == ["user", "assistant", "tool"]
        assert result["completed"] is False
        assert result["failed"] is True
        assert result["partial"] is True
        assert result["turn_exit_reason"] == "session_persistence_failed"
        assert result["failure_reason"] == "session_persistence_failed:locked"
        assert "The external action completed once." in result["final_response"]
        assert "already have run" in result["error"]
        assert "send it again" not in result["error"]
        assert result["messages"][-1]["content"] == "The external action completed once."
        assert not db._conn.in_transaction
    finally:
        for reader in readers:
            reader.close()
        db.close()


def test_rejected_persist_does_not_acknowledge_turn(tmp_path):
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    try:
        with (patch.object(agent, "_flush_messages_to_session_db", return_value=False),
              patch("agent.agent_runtime_helpers.note_turn_persisted") as acknowledged):
            assert agent._persist_session([{"role": "user", "content": "pending"}]) is False
        acknowledged.assert_not_called()
    finally:
        db.close()


@pytest.mark.parametrize("failure", ["disk", "unknown", "exception-before-commit", "exception-after-commit"])
def test_final_save_faults_distinguish_canonical_commit_and_never_advise_replay(tmp_path, failure):
    import sqlite3
    from agent.turn_finalizer import finalize_turn
    from tests.run_agent.test_storage_progress_recovery import _history
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    messages = _history()
    assert agent._flush_messages_to_session_db(messages) is True
    messages.append({"role": "assistant", "content": "The action receipt is preserved."})
    if failure == "exception-before-commit":
        target = patch.object(agent, "_save_session_log", side_effect=OSError("No space left on device"))
    elif failure == "exception-after-commit":
        target = patch.object(db, "flush_token_counts", side_effect=RuntimeError("accounting cleanup failed"))
    else:
        target = patch.object(db, "append_messages_batch", side_effect=sqlite3.OperationalError(
            "database or disk is full" if failure == "disk" else "unexpected storage failure"))
    try:
        with target, patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
            result = finalize_turn(agent, final_response="The action receipt is preserved.", api_call_count=2,
                interrupted=False, failed=False, messages=messages, conversation_history=[],
                effective_task_id="synthetic", turn_id="turn", user_message="approved job",
                original_user_message="approved job", _should_review_memory=False,
                _turn_exit_reason="text_response(stop)")
        durable = db.get_messages_as_conversation("worker")
        assert "The action receipt is preserved." in result["final_response"]
        assert result["messages"][-1]["content"] == "The action receipt is preserved."
        if failure == "exception-after-commit":
            assert durable[-1]["content"] == "The action receipt is preserved."
            assert result["completed"] is True
            assert result["failed"] is False
            assert result["cleanup_errors"]
        else:
            assert len(durable) == 3
            assert result["completed"] is False
            assert result["failed"] is True
            assert result["failure_reason"].startswith("session_persistence_failed:")
            assert "already have run" in result["error"]
            assert "send your message again" not in result["error"]
            assert "send it again" not in result["error"]
    finally:
        db.close()
