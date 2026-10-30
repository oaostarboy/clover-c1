from clover_state import SessionDB


def test_helpers_are_explicit_bounded_and_owned_across_compression(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("a", "desktop")
        db.create_session("b", "desktop")
        db.end_session("a", "compression")
        db.create_session("tip", "desktop", parent_session_id="a")
        db.create_session("branch", "desktop", parent_session_id="a", model_config={"_branched_from": "a"})
        for sid, parent, source in [("old-worker", "a", "cli"), ("new-worker", "tip", "cli"),
                                    ("delegate", "tip", "tool"), ("other-worker", "b", "cli")]:
            db.create_session(sid, source, parent_session_id=parent, model_config={"_delegate_from": parent})
            db.append_message(sid, "user", "worker transcript")
        first = db.list_session_helpers("tip", limit=2)
        second = db.list_session_helpers("a", limit=2, offset=2)
        ids = [r["id"] for r in first + second]
        assert len(first) == 2
        assert set(ids) == {"old-worker", "new-worker", "delegate"}
        assert len(set(ids)) == len(ids)
        assert [r["id"] for r in db.list_session_helpers("b")] == ["other-worker"]
        assert db.list_session_helpers("missing") == []
        assert db.get_compression_tip("a") == "tip"
        assert {r["id"] for r in db.list_sessions_rich()} == {"tip", "b", "branch"}
        assert db.resolve_resume_session_id("old-worker") == "old-worker"
        assert db.get_messages("old-worker")
        assert not db.list_sessions_rich(search_query="worker", order_by_last_active=True)
    finally:
        db.close()


def test_inherited_helper_marker_does_not_expose_a_second_helper(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("parent", "desktop")
        db.create_session("worker", "tool", parent_session_id="parent", model_config={"_delegate_from": "parent"})
        db.end_session("worker", "compression")
        db.create_session("worker-tip", "tool", parent_session_id="worker", model_config={"_delegate_from": "parent"})
        rows = db.list_session_helpers("parent")
        assert [r["id"] for r in rows] == ["worker"]
        assert db.list_session_helpers("worker") == []
    finally:
        db.close()


def test_orphan_is_not_reassigned_or_exposed_as_a_root(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("parent", "desktop")
        db.create_session("unrelated", "desktop")
        db.create_session("orphan", "cli", parent_session_id="parent", model_config={"_delegate_from": "parent"})
        db.append_message("orphan", "user", "preserved transcript")
        # Simulate a historical orphan without performing parent-delete cascade.
        db._execute_write(lambda conn: conn.execute("UPDATE sessions SET parent_session_id = NULL WHERE id = 'orphan'"))
        assert db.list_session_helpers("parent") == []
        assert db.list_session_helpers("unrelated") == []
        assert "orphan" not in {r["id"] for r in db.list_sessions_rich()}
        assert db.resolve_resume_session_id("orphan") == "orphan"
        assert db.get_messages("orphan")[0]["content"] == "preserved transcript"
    finally:
        db.close()


def test_rpc_helper_expansion_does_not_change_normal_list(tmp_path, monkeypatch):
    import tui_gateway.server as server

    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_db", db)
    try:
        db.create_session("parent", "desktop")
        db.create_session("worker", "cli", parent_session_id="parent", model_config={"_delegate_from": "parent"})
        db.append_message("worker", "user", "RPC transcript")
        rows = server._methods["session.list"](1, {})["result"]["sessions"]
        assert [r["id"] for r in rows] == ["parent"]
        response = server._methods["session.list"](2, {"helper_parent_id": "parent", "limit": 1})
        page = response["result"]
        assert [r["id"] for r in page["sessions"]] == ["worker"]
        assert page["has_more"] is False
        assert "model_config" not in page["sessions"][0]
    finally:
        db.close()
