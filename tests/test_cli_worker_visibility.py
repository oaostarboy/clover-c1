from clover_state import SessionDB


def test_explicit_cli_worker_does_not_leak_into_root_session_list(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="chat", source="desktop")
        db.create_session(session_id="worker", source="cli", parent_session_id="chat", model_config={"_delegate_from": "chat"})
        rows = db.list_sessions_rich(limit=100)
        worker = next((row for row in rows if row["id"] == "worker"), None)
        assert worker is None
    finally:
        db.close()


def test_cli_worker_is_not_a_compression_continuation(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="chat", source="desktop")
        db.end_session("chat", "compression")
        db.create_session(session_id="worker", source="cli", parent_session_id="chat", model_config={"_delegate_from": "chat"})
        assert db.get_compression_tip("chat") == "chat"
    finally:
        db.close()


def test_unmarked_cli_compression_continuation_still_projects(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="chat", source="desktop")
        db.end_session("chat", "compression")
        db.create_session(session_id="continuation", source="cli", parent_session_id="chat")
        assert db.get_compression_tip("chat") == "continuation"
        assert [row["id"] for row in db.list_sessions_rich()] == ["continuation"]
    finally:
        db.close()
