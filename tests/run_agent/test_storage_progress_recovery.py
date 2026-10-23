"""Real failed COMMIT -> checkpoint -> same-owner flush. No tool replay."""
import json
import logging
import sqlite3
from unittest.mock import patch
import pytest
from tests.run_agent.test_tool_call_incremental_persistence import (
    _make_agent, _attach_real_session_db, _mock_tool_call, _mock_response,
)


def _hold_reader(db):
    if db._conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
        db._conn.execute("PRAGMA journal_mode=DELETE")
    db._wal_active = False
    db._conn.execute("PRAGMA busy_timeout=1")
    db._TRANSCRIPT_WRITE_PATIENCE_S = .01
    reader = sqlite3.connect(f"file:{db.db_path}?mode=ro", uri=True, isolation_level=None, check_same_thread=False)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM messages").fetchall()
    return reader


class ReleaseAfterFailure(logging.Handler):
    def __init__(self, reader, callback=lambda: None):
        super().__init__(); self.reader = reader; self.callback = callback; self.seen = False
    def emit(self, record):
        if "Session DB append_message failed" in record.getMessage() and not self.seen:
            self.seen = True
            self.reader.rollback()
            self.callback()


def _history():
    return [
        {"role": "user", "content": "approved job"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "effect1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "effect1", "name": "web_search", "content": "effect already happened"},
    ]


def test_failed_result_flush_recovers_once_without_replaying_effect(tmp_path):
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    agent._current_turn_id = "approved-turn"
    agent._delegate_depth = 1
    history = _history()
    assert agent._flush_messages_to_session_db(history[:2]) is True
    reader = _hold_reader(db)
    release = ReleaseAfterFailure(reader)
    log = logging.getLogger("run_agent"); log.addHandler(release)
    try:
        assert agent._flush_messages_to_session_db(history) is True
        assert release.seen
        assert agent._flush_messages_to_session_db(history) is True
        assert [m["role"] for m in db.get_messages_as_conversation("worker")] == ["user", "assistant", "tool"]
        files = list((tmp_path / "storage-recovery").glob("*.json"))
        assert len(files) == 1
        checkpoint = json.loads(files[0].read_text())
        assert checkpoint["disposition"] == "canonical"
        assert "messages" not in checkpoint
        assert db.get_messages_as_conversation("worker")[-1]["content"] == "effect already happened"
        assert checkpoint["automatic_replay_allowed"] is False
    finally:
        log.removeHandler(release); reader.close(); db.close()


@pytest.mark.parametrize("revoke", ["stop", "turn", "session", "model", "provider", "pin", "lease", "epoch"])
def test_recovery_never_continues_revoked_owner(tmp_path, revoke):
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    agent._current_turn_id = "approved-turn"
    agent._delegate_depth = 1
    history = _history()
    assert agent._flush_messages_to_session_db(history[:2]) is True
    reader = _hold_reader(db)
    def invalidate():
        if revoke == "stop": agent._interrupt_requested = True
        if revoke == "turn": agent._current_turn_id = "reset-turn"
        if revoke == "session": agent.session_id = "replacement"
        if revoke == "model": agent.model = "not-the-pinned-model"
        if revoke == "provider": agent.provider = "different-provider"
        if revoke == "pin": agent.model_pinned = not agent.model_pinned
        if revoke == "lease": agent._active_session_turn_lease_holder = "replacement-holder"
        if revoke == "epoch": agent._orchestration_stop_epoch = 99
    release = ReleaseAfterFailure(reader, invalidate)
    log = logging.getLogger("run_agent"); log.addHandler(release)
    try:
        assert agent._flush_messages_to_session_db(history) is False
        assert release.seen
        assert not any(m["role"] == "tool" for m in db.get_messages_as_conversation("worker"))
    finally:
        log.removeHandler(release); reader.close(); db.close()


@pytest.mark.parametrize("executor_mode", ["sequential", "concurrent"])
def test_worker_finishes_after_real_locked_result_commit(tmp_path, executor_mode):
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    agent._delegate_depth = 1
    call = _mock_tool_call(call_id="effect1")
    # Choose the synthetic test mode before the agent opens pooled readers.
    db._conn.execute("PRAGMA journal_mode=DELETE")
    db._wal_active = False
    agent.client.chat.completions.create.side_effect = [
        _mock_response(content="", finish_reason="tool_calls", tool_calls=[call]),
        _mock_response(content="verified complete", finish_reason="stop"),
    ]
    readers = []
    releases = []
    effects = []
    log = logging.getLogger("run_agent")
    def effect(*args, **kwargs):
        effects.append("external-effect")
        reader = _hold_reader(db)
        release = ReleaseAfterFailure(reader)
        readers.append(reader); releases.append(release); log.addHandler(release)
        return "effect already happened"
    from types import SimpleNamespace
    def execute(assistant, messages, task_id, api_calls=0):
        method = (agent._execute_tool_calls_sequential if executor_mode == "sequential"
                  else agent._execute_tool_calls_concurrent)
        return method(assistant, messages, task_id)
    dispatch = (patch("run_agent.handle_function_call", side_effect=effect)
                if executor_mode == "sequential" else patch.object(agent, "_invoke_tool", side_effect=effect))
    try:
        with (dispatch, patch.object(agent, "_execute_tool_calls", side_effect=execute),
              patch("agent.tool_executor.maybe_persist_tool_result", side_effect=lambda **k: k["content"]),
              patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources")):
            result = agent.run_conversation("finish the approved task")
        assert result["final_response"] == "verified complete"
        assert not result.get("failed")
        assert effects == ["external-effect"]
        assert releases[0].seen
        assert result["storage_recovery"]["automatic_replay_allowed"] is False
        assert json.loads(__import__("pathlib").Path(result["storage_recovery"]["path"]).read_text())["disposition"] == "canonical"
        durable = db.get_messages_as_conversation("worker")
        assert sum(m["role"] == "tool" for m in durable) == 1
        assert durable[-1]["content"] == "verified complete"
        assert agent.client.chat.completions.create.call_count == 2
    finally:
        for release in releases: log.removeHandler(release)
        for reader in readers: reader.close()
        db.close()


def test_persistent_lock_fails_closed_with_durable_checkpoint(tmp_path):
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    agent._delegate_depth = 1; agent._current_turn_id = "approved-turn"
    history = _history()
    assert agent._flush_messages_to_session_db(history[:2]) is True
    reader = _hold_reader(db)
    try:
        assert agent._flush_messages_to_session_db(history) is False
        assert reader.in_transaction
        checkpoint = json.loads(next((tmp_path / "storage-recovery").glob("*.json")).read_text())
        assert checkpoint["disposition"] == "pending"
        assert checkpoint["automatic_replay_allowed"] is False
        assert not any(m["role"] == "tool" for m in db.get_messages_as_conversation("worker"))
    finally:
        reader.close(); db.close()


def test_checkpoint_survives_process_crash_without_replay(tmp_path):
    import subprocess, sys
    code = r"""
import os, sys
from pathlib import Path
from tests.run_agent.test_storage_progress_recovery import _make_agent, _attach_real_session_db, _history, _hold_reader
import agent.storage_recovery as recovery
root = Path(sys.argv[1])
a = _make_agent(); db = _attach_real_session_db(a, root / 'state.db', 'worker')
a._delegate_depth = 1; a._current_turn_id = 'approved-turn'
history = _history(); assert a._flush_messages_to_session_db(history[:2]) is True
reader = _hold_reader(db)
write = recovery._write_checkpoint
def crash(path, payload):
    write(path, payload)
    os._exit(73)
recovery._write_checkpoint = crash
a._flush_messages_to_session_db(history)
raise AssertionError('no durable checkpoint reached')
"""
    child = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=45)
    assert child.returncode == 73, child.stderr
    file = next((tmp_path / "storage-recovery").glob("*.json"))
    checkpoint = json.loads(file.read_text())
    assert checkpoint["disposition"] == "pending"
    assert checkpoint["messages"][-1]["content"] == "effect already happened"
    from clover_state import SessionDB
    db = SessionDB(tmp_path / "state.db")
    try:
        # Reopen is read/verify-only. It must not replay a file's uncertain effects.
        assert [m["role"] for m in db.get_messages_as_conversation("worker")] == ["user", "assistant"]
        assert file.exists()
    finally:
        db.close()


def test_parent_stop_epoch_blocks_recovery_after_interrupt_flag_clear(tmp_path):
    import weakref
    from types import SimpleNamespace
    class Parent: pass
    parent = Parent(); parent.session_id = "parent"; parent._orchestration_stop_epoch = 3
    agent = _make_agent(); agent._delegate_parent_ref = weakref.ref(parent)
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    agent._delegate_depth = 1; agent._current_turn_id = "approved-turn"
    history = _history(); assert agent._flush_messages_to_session_db(history[:2]) is True
    reader = _hold_reader(db)
    def stop_then_clear():
        parent._orchestration_stop_epoch += 1
        parent._interrupt_requested = False
    release = ReleaseAfterFailure(reader, stop_then_clear)
    log = logging.getLogger("run_agent"); log.addHandler(release)
    try:
        assert agent._flush_messages_to_session_db(history) is False
        assert not any(m["role"] == "tool" for m in db.get_messages_as_conversation("worker"))
    finally:
        log.removeHandler(release); reader.close(); db.close()


def test_checkpoint_failure_does_not_allow_unrecorded_continuation(tmp_path):
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    agent._delegate_depth = 1; agent._current_turn_id = "approved-turn"
    history = _history(); assert agent._flush_messages_to_session_db(history[:2]) is True
    # Real filesystem failure; no mocked commit and no permission bypass.
    (tmp_path / "storage-recovery").write_text("not a directory")
    agent._storage_recovery_turn = "previous-turn"
    agent._storage_recovery_checkpoint = "/private/previous-turn.json"
    reader = _hold_reader(db)
    release = ReleaseAfterFailure(reader)
    log = logging.getLogger("run_agent"); log.addHandler(release)
    try:
        assert agent._flush_messages_to_session_db(history) is False
        assert release.seen
        assert not any(m["role"] == "tool" for m in db.get_messages_as_conversation("worker"))
        assert agent._storage_recovery_checkpoint is None
    finally:
        log.removeHandler(release); reader.close(); db.close()


def test_storage_busy_explanation_does_not_advise_replaying_effects():
    from run_agent import AIAgent
    text = AIAgent._format_turn_completion_explanation("session_persistence_failed", "locked")
    assert "reader" in text
    assert "already have run" in text
    assert "send it again" not in text


@pytest.mark.parametrize("change", ["none", "revoked", "epoch", "replaced", "missing"])
def test_accepted_job_identity_fences_storage_recovery(tmp_path, monkeypatch, change):
    from tools import async_delegation as ad
    agent = _make_agent()
    db = _attach_real_session_db(agent, tmp_path / "state.db", "worker")
    agent._delegate_depth = 1; agent._current_turn_id = "approved-turn"
    agent._delegation_id = "owned-job"
    record = {"status": "running", "job_epoch": "original", "_ownership_revoked": False}
    monkeypatch.setitem(ad._records, "owned-job", record)
    history = _history(); assert agent._flush_messages_to_session_db(history[:2]) is True
    reader = _hold_reader(db)
    def change_owner():
        if change == "revoked": record["_ownership_revoked"] = True
        if change == "epoch": record["job_epoch"] = "replacement"
        if change == "replaced": ad._records["owned-job"] = dict(record)
        if change == "missing": ad._records.pop("owned-job")
    release = ReleaseAfterFailure(reader, change_owner)
    log = logging.getLogger("run_agent"); log.addHandler(release)
    try:
        assert agent._flush_messages_to_session_db(history) is (change == "none")
        assert release.seen
        assert any(m["role"] == "tool" for m in db.get_messages_as_conversation("worker")) is (change == "none")
    finally:
        log.removeHandler(release); reader.close(); db.close()
