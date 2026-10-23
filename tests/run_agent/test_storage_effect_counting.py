"""Synthetic external effects: persistence recovery never reissues an action."""
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.task_outcome import task_outcome
from agent.tool_dispatch_helpers import make_tool_result_message
from tests.run_agent.test_tool_call_incremental_persistence import (
    _make_agent, _attach_real_session_db, _mock_tool_call, _mock_response,
)
from tests.run_agent.test_storage_progress_recovery import _hold_reader, ReleaseAfterFailure


@pytest.mark.parametrize("kind", ["tool", "third-party-message", "payment", "unknown-commit"])
@pytest.mark.parametrize("release", [False, True])
def test_recovery_preserves_action_receipts_without_replay(tmp_path, kind, release):
    # Only local files change. These labels model external actions; no real
    # payment, message, cloud job, or provider call is issued by the test.
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    agent._delegate_depth = 1
    db._conn.execute("PRAGMA journal_mode=DELETE")
    db._wal_active = False
    db._WRITE_PATIENCE_S = .01
    readers, handlers = [], []
    ledger = tmp_path / "effect.jsonl"
    receipt = ("commit unknown; owner must reconcile; do not retry" if kind == "unknown-commit"
               else f"synthetic {kind} acknowledged once")
    final = ("Result: not done\nExternal commit is unknown; reconcile without replay."
             if kind == "unknown-commit" else "Result: done\nVerified synthetic action receipt.")
    agent.client.chat.completions.create.side_effect = [
        _mock_response(content="", finish_reason="tool_calls", tool_calls=[_mock_tool_call(call_id="effect")]),
        _mock_response(content=final, finish_reason="stop"),
    ]
    expected_receipt = make_tool_result_message("web_search", receipt, "effect")["content"]
    log = logging.getLogger("run_agent")

    def effect(*args, **kwargs):
        assert not db._conn.in_transaction
        with ledger.open("a") as handle:
            handle.write(json.dumps({"kind": kind, "effect_id": "effect"}) + "\n")
        reader = _hold_reader(db)
        readers.append(reader)
        if release:
            handler = ReleaseAfterFailure(reader)
            handlers.append(handler)
            log.addHandler(handler)
        return receipt

    try:
        with (patch("run_agent.handle_function_call", side_effect=effect),
              patch.object(agent, "_execute_tool_calls", side_effect=lambda a, m, t, api_calls=0: agent._execute_tool_calls_sequential(a, m, t)),
              patch("agent.tool_executor.maybe_persist_tool_result", side_effect=lambda **k: k["content"]),
              patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources")):
            result = agent.run_conversation("perform one approved synthetic action")
        assert [json.loads(line) for line in ledger.read_text().splitlines()] == [{"kind": kind, "effect_id": "effect"}]
        assert agent.client.chat.completions.create.call_count == (2 if release else 1)
        checkpoint_path = Path(result["storage_recovery"]["path"])
        checkpoint = json.loads(checkpoint_path.read_text())
        assert checkpoint["automatic_replay_allowed"] is False
        durable = db.get_messages_as_conversation("worker")
        if release:
            assert handlers[0].seen
            assert result["failed"] is False
            assert checkpoint["disposition"] == "canonical"
            assert [(m["role"], m["content"]) for m in durable] == [
                ("user", "perform one approved synthetic action"), ("assistant", ""),
                ("tool", expected_receipt), ("assistant", final),
            ]
            assert durable[1]["tool_calls"][0]["id"] == durable[2]["tool_call_id"] == "effect"
            assert task_outcome(result, result["final_response"])["status"] == ("partial" if kind == "unknown-commit" else "done")
        else:
            assert result["completed"] is False
            assert result["failed"] is True
            assert result["failure_reason"] == "session_persistence_failed:locked"
            assert checkpoint["disposition"] == "pending"
            assert checkpoint["messages"][-1]["content"] == expected_receipt
            assert [(m["role"], m["content"]) for m in durable] == [
                ("user", "perform one approved synthetic action"), ("assistant", ""),
            ]
            assert task_outcome(result, result["final_response"])["status"] == "unknown"
        for reader in readers:
            reader.rollback()
        db.close()
        # Reopening only observes canonical state. It must not adopt pending
        # uncertain action files or invoke the already-performed action again.
        from clover_state import SessionDB
        reopened = SessionDB(tmp_path / "state.db")
        try:
            assert [(m["role"], m["content"]) for m in reopened.get_messages_as_conversation("worker")] == [(m["role"], m["content"]) for m in durable]
            assert ledger.read_text().count('"effect_id"') == 1
            assert checkpoint_path.exists()
        finally:
            reopened.close()
    finally:
        for handler in handlers:
            log.removeHandler(handler)
        for reader in readers:
            reader.close()
        db.close()
