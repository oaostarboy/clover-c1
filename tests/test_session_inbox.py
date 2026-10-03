"""Durable session inbox (``session_inbox``) — SessionDB behaviour contracts.

Real ``SessionDB`` files in a temp dir; no mocks of the storage layer.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from clover_state import SCHEMA_VERSION, SessionDB


@pytest.fixture
def db(tmp_path):
    handle = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield handle
    finally:
        handle.close()


def _raw(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _record(key="deleg:d1", **over):
    rec = {
        "key": key,
        "profile": "default",
        "platform": "telegram",
        "chat_id": "100",
        "thread_id": None,
        "session_key": "agent:main:telegram:dm:100",
        "owner_root_id": "root-1",
        "kind": "delegation",
        "wake": 1,
        "title": "Research task",
        "payload": {"summary": "done"},
        "shown_to_user": 0,
    }
    rec.update(over)
    return rec


def _msg_rows(db_path, session_id):
    conn = _raw(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, role, content FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        )]
    finally:
        conn.close()


def _user_msg(keys, content="[background result]"):
    return {
        "role": "user",
        "content": content,
        "display_kind": "internal_notification",
        "display_metadata": {"inbox_keys": list(keys)},
    }


# 1 ----------------------------------------------------------------------
def test_inbox_put_is_idempotent_on_key(db):
    assert db.inbox_put(_record()) == "inserted"
    assert db.inbox_put(_record(title="changed")) == "exists"
    rows = db.inbox_for_route("default", "telegram", "100", None)
    assert len(rows) == 1
    assert rows[0]["title"] == "Research task"
    assert rows[0]["state"] == "pending"


def test_inbox_put_rejects_invalid_kind_instead_of_reporting_exists(db):
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        db.inbox_put(_record(kind="bogus"))
    assert db.inbox_get("deleg:d1") is None


# 2 ----------------------------------------------------------------------
def test_concurrent_inbox_put_same_key_yields_one_row(tmp_path):
    path = tmp_path / "state.db"
    a, b = SessionDB(db_path=path), SessionDB(db_path=path)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker(handle):
        try:
            barrier.wait(5)
            results.append(handle.inbox_put(_record("proc:p1", kind="process")))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    try:
        threads = [threading.Thread(target=worker, args=(h,)) for h in (a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        assert not errors
        assert sorted(results) == ["exists", "inserted"]
        conn = _raw(path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM session_inbox WHERE key = 'proc:p1'"
            ).fetchone()[0] == 1
        finally:
            conn.close()
    finally:
        a.close()
        b.close()


# 3 ----------------------------------------------------------------------
def test_batch_insert_ingests_pending_keys_in_same_transaction(db, tmp_path):
    db.create_session("s1", "telegram")
    db.inbox_put(_record("deleg:k1"))
    db.inbox_put(_record("deleg:k2"))
    before = time.time()

    inserted = db.append_messages_batch("s1", [_user_msg(["deleg:k1", "deleg:k2"])])
    assert inserted == 1

    rows = _msg_rows(tmp_path / "state.db", "s1")
    assert len(rows) == 1
    for key in ("deleg:k1", "deleg:k2"):
        rec = db.inbox_get(key)
        assert rec["state"] == "ingested"
        assert rec["ingested_session_id"] == "s1"
        assert rec["ingested_message_id"] == rows[0]["id"]
        assert rec["ingested_at"] >= before


def test_ingestion_is_atomic_with_the_message_row(db, tmp_path, monkeypatch):
    db.create_session("s1", "telegram")
    db.inbox_put(_record("deleg:k1"))
    original = SessionDB._insert_message_rows

    def insert_then_fail(self, conn, session_id, messages):
        original(self, conn, session_id, messages)
        raise RuntimeError("boom after insert")

    monkeypatch.setattr(SessionDB, "_insert_message_rows", insert_then_fail)
    with pytest.raises(RuntimeError, match="boom after insert"):
        db.append_messages_batch("s1", [_user_msg(["deleg:k1"])])
    monkeypatch.setattr(SessionDB, "_insert_message_rows", original)

    assert _msg_rows(tmp_path / "state.db", "s1") == []
    rec = db.inbox_get("deleg:k1")
    assert rec["state"] == "pending"
    assert rec["ingested_message_id"] is None
    assert rec["ingested_session_id"] is None


def test_message_without_inbox_keys_is_unchanged(db, tmp_path):
    db.create_session("s1", "telegram")
    db.inbox_put(_record("deleg:k1"))
    db.append_messages_batch("s1", [
        {"role": "user", "content": "hi", "display_metadata": {"other": 1}},
        {"role": "assistant", "content": "hello"},
    ])
    assert db.inbox_get("deleg:k1")["state"] == "pending"
    assert len(_msg_rows(tmp_path / "state.db", "s1")) == 2


def test_display_metadata_as_json_string_is_honoured(db):
    db.create_session("s1", "telegram")
    db.inbox_put(_record("deleg:k1"))
    msg = _user_msg([])
    msg["display_metadata"] = json.dumps({"inbox_keys": ["deleg:k1"]})
    db.append_messages_batch("s1", [msg])
    assert db.inbox_get("deleg:k1")["state"] == "ingested"


def test_unknown_inbox_key_is_a_noop_and_row_is_written(db, tmp_path):
    db.create_session("s1", "telegram")
    assert db.append_messages_batch("s1", [_user_msg(["deleg:never-put"])]) == 1
    assert len(_msg_rows(tmp_path / "state.db", "s1")) == 1


# 4 ----------------------------------------------------------------------
def test_rotation_copy_does_not_reingest_or_error(db, tmp_path):
    db.create_session("s1", "telegram")
    db.create_session("s2", "telegram")
    db.inbox_put(_record("deleg:k1"))
    db.append_messages_batch("s1", [_user_msg(["deleg:k1"])])
    first = db.inbox_get("deleg:k1")

    assert db.append_messages_batch("s2", [_user_msg(["deleg:k1"])]) == 1

    again = db.inbox_get("deleg:k1")
    assert again["state"] == "ingested"
    assert again["ingested_session_id"] == "s1"
    assert again["ingested_message_id"] == first["ingested_message_id"]
    assert again["ingested_at"] == first["ingested_at"]
    assert len(_msg_rows(tmp_path / "state.db", "s2")) == 1


def test_replace_messages_copy_of_ingested_key_keeps_original_ids(db):
    db.create_session("s1", "telegram")
    db.inbox_put(_record("deleg:k1"))
    db.append_messages_batch("s1", [_user_msg(["deleg:k1"])])
    first = db.inbox_get("deleg:k1")
    db.replace_messages("s1", [_user_msg(["deleg:k1"])])
    assert db.inbox_get("deleg:k1")["ingested_message_id"] == first["ingested_message_id"]


def test_dropped_key_is_not_resurrected_by_a_later_row(db):
    db.create_session("s1", "telegram")
    db.inbox_put(_record("deleg:k1"))
    db.inbox_drop("deleg:k1", "unowned")
    db.append_messages_batch("s1", [_user_msg(["deleg:k1"])])
    assert db.inbox_get("deleg:k1")["state"] == "dropped"


# 5 ----------------------------------------------------------------------
def test_single_row_append_message_ingests_pending_keys(db, tmp_path):
    db.create_session("s1", "telegram")
    db.inbox_put(_record("deleg:k1"))
    db.inbox_put(_record("deleg:k2"))
    msg_id = db.append_message(
        "s1", "user", "[background result]",
        display_kind="internal_notification",
        display_metadata={"inbox_keys": ["deleg:k1", "deleg:k2"]},
    )
    for key in ("deleg:k1", "deleg:k2"):
        rec = db.inbox_get(key)
        assert rec["state"] == "ingested"
        assert rec["ingested_session_id"] == "s1"
        assert rec["ingested_message_id"] == msg_id
    assert [r["id"] for r in _msg_rows(tmp_path / "state.db", "s1")] == [msg_id]


# 6 ----------------------------------------------------------------------
def test_inbox_drop_sets_notice_pending(db):
    db.inbox_put(_record("deleg:k1", shown_to_user=0))
    assert db.inbox_drop("deleg:k1", "unowned") is True
    rec = db.inbox_get("deleg:k1")
    assert rec["state"] == "dropped"
    assert rec["drop_reason"] == "unowned"
    assert rec["notice_state"] == "pending"


def test_inbox_drop_of_already_shown_record_needs_no_notice(db):
    db.inbox_put(_record("council:default:r1:final", kind="council", wake=0, shown_to_user=1))
    assert db.inbox_drop("council:default:r1:final", "stale") is True
    rec = db.inbox_get("council:default:r1:final")
    assert rec["state"] == "dropped"
    assert rec["notice_state"] is None


def test_inbox_drop_never_touches_ingested_or_missing(db):
    db.create_session("s1", "telegram")
    db.inbox_put(_record("deleg:k1"))
    db.append_messages_batch("s1", [_user_msg(["deleg:k1"])])
    assert db.inbox_drop("deleg:k1", "late") is False
    assert db.inbox_get("deleg:k1")["state"] == "ingested"
    assert db.inbox_drop("deleg:nope", "late") is False


# 7 ----------------------------------------------------------------------
def _insert_delegation(conn, delegation_id):
    now = time.time()
    conn.execute(
        "INSERT INTO async_delegations (delegation_id, origin_session, state, "
        "dispatched_at, updated_at, delivery_state) VALUES (?, 'sk', 'completed', ?, ?, 'pending')",
        (delegation_id, now, now),
    )


def test_inbox_put_and_mark_commits_both_together(db, tmp_path):
    path = tmp_path / "state.db"
    conn = _raw(path)
    try:
        with conn:
            _insert_delegation(conn, "d1")
        with conn:
            assert db.inbox_put_and_mark(conn, _record("deleg:d1"), delegation_id="d1") == "inserted"
    finally:
        conn.close()

    check = _raw(path)
    try:
        assert check.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id='d1'"
        ).fetchone()[0] == "inboxed"
    finally:
        check.close()
    assert db.inbox_get("deleg:d1")["state"] == "pending"


def test_inbox_put_and_mark_rolls_back_both_together(db, tmp_path):
    path = tmp_path / "state.db"
    conn = _raw(path)
    try:
        with conn:
            _insert_delegation(conn, "d1")
        with pytest.raises(RuntimeError):
            with conn:
                db.inbox_put_and_mark(conn, _record("deleg:d1"), delegation_id="d1")
                raise RuntimeError("caller txn fails")
    finally:
        conn.close()
    check = _raw(path)
    try:
        assert check.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id='d1'"
        ).fetchone()[0] == "pending"
    finally:
        check.close()
    assert db.inbox_get("deleg:d1") is None


def test_inbox_put_and_mark_without_delegation_id_only_inserts(db, tmp_path):
    conn = _raw(tmp_path / "state.db")
    try:
        with conn:
            assert db.inbox_put_and_mark(conn, _record("proc:p1", kind="process")) == "inserted"
    finally:
        conn.close()
    assert db.inbox_get("proc:p1")["kind"] == "process"


def test_inbox_put_and_mark_on_existing_key_still_marks_delegation(db, tmp_path):
    """A re-delivered completion whose key already exists must still leave the
    delegation row 'inboxed' so the claim/replay path cannot pick it up again."""
    db.inbox_put(_record("deleg:d1"))
    conn = _raw(tmp_path / "state.db")
    try:
        with conn:
            _insert_delegation(conn, "d1")
        with conn:
            assert db.inbox_put_and_mark(conn, _record("deleg:d1"), delegation_id="d1") == "exists"
        assert conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id='d1'"
        ).fetchone()[0] == "inboxed"
    finally:
        conn.close()


# 8 ----------------------------------------------------------------------
def test_prune_payloads_keeps_row_and_key_tombstone(db):
    db.create_session("s1", "telegram")
    db.inbox_put(_record("deleg:k1"))
    db.inbox_put(_record("deleg:k2"))
    db.inbox_put(_record("deleg:k3"))
    db.append_messages_batch("s1", [_user_msg(["deleg:k1"])])
    db.inbox_drop("deleg:k2", "stale")

    pruned = db.inbox_prune_payloads(older_than_s=-1)

    assert pruned == 2
    for key in ("deleg:k1", "deleg:k2"):
        rec = db.inbox_get(key)
        assert rec["payload_json"] is None
        assert rec["payload_pruned_at"] is not None
        assert db.inbox_put(_record(key)) == "exists"
        assert db.inbox_get(key)["payload_json"] is None
    pending = db.inbox_get("deleg:k3")
    assert pending["state"] == "pending"
    assert pending["payload_json"] is not None
    assert pending["payload_pruned_at"] is None


def test_prune_payloads_respects_age(db):
    db.create_session("s1", "telegram")
    db.inbox_put(_record("deleg:k1"))
    db.append_messages_batch("s1", [_user_msg(["deleg:k1"])])
    assert db.inbox_prune_payloads(older_than_s=30 * 86400) == 0
    assert db.inbox_get("deleg:k1")["payload_json"] is not None


# 9 ----------------------------------------------------------------------
def test_route_and_owner_lookups_are_ordered_by_seq(db):
    db.create_session("s1", "telegram")
    db.inbox_put(_record("deleg:a", wake=1))
    db.inbox_put(_record("council:default:r:final", kind="council", wake=0))
    db.inbox_put(_record("deleg:b", wake=1))
    db.inbox_put(_record("deleg:other-route", chat_id="999"))
    db.inbox_put(_record("deleg:other-thread", thread_id="7"))
    db.inbox_put(_record("deleg:other-owner", owner_root_id="root-2"))
    seqs = [db.inbox_get(k)["seq"] for k in ("deleg:a", "council:default:r:final", "deleg:b")]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3

    route = ("default", "telegram", "100", None, "agent:main:telegram:dm:100")
    keys = lambda rows: [r["key"] for r in rows]  # noqa: E731
    assert keys(db.inbox_pending_for_route(*route)) == [
        "deleg:a", "council:default:r:final", "deleg:b", "deleg:other-owner",
    ]
    assert keys(db.inbox_pending_for_route(*route, wake=True)) == [
        "deleg:a", "deleg:b", "deleg:other-owner",
    ]
    assert keys(db.inbox_pending_for_route(*route, wake=False)) == ["council:default:r:final"]
    assert keys(db.inbox_pending_for_route(
        "default", "telegram", "100", "7", "agent:main:telegram:dm:100"
    )) == ["deleg:other-thread"]

    assert keys(db.inbox_pending_for_owner("root-1")) == [
        "deleg:a", "council:default:r:final", "deleg:b", "deleg:other-route", "deleg:other-thread",
    ]
    assert keys(db.inbox_pending_for("root-1", include_wake=False)) == ["council:default:r:final"]
    assert keys(db.inbox_pending_for("root-1", include_wake=True)) == keys(
        db.inbox_pending_for_owner("root-1")
    )

    db.append_messages_batch("s1", [_user_msg(["deleg:a"])])
    assert "deleg:a" not in keys(db.inbox_pending_for_owner("root-1"))


def test_inbox_for_route_filters_by_state_and_includes_dropped(db):
    db.create_session("s1", "telegram")
    db.inbox_put(_record("deleg:a"))
    db.inbox_put(_record("deleg:b"))
    db.inbox_put(_record("deleg:c"))
    db.append_messages_batch("s1", [_user_msg(["deleg:a"])])
    db.inbox_drop("deleg:b", "unowned")
    route = ("default", "telegram", "100", None)
    assert [r["key"] for r in db.inbox_for_route(*route)] == ["deleg:a", "deleg:b", "deleg:c"]
    assert [r["key"] for r in db.inbox_for_route(*route, state="dropped")] == ["deleg:b"]
    assert [r["key"] for r in db.inbox_for_route(*route, state="pending")] == ["deleg:c"]


# 10 ---------------------------------------------------------------------
def test_migration_adds_inbox_to_existing_db_and_keeps_data(tmp_path):
    path = tmp_path / "state.db"
    first = SessionDB(db_path=path)
    first.create_session("old", "telegram")
    first.append_message("old", "user", "legacy message")
    first.close()

    # Recreate the pre-inbox shape: no table, previous schema version.
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DROP TABLE session_inbox")
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION - 1,))
        conn.commit()
    finally:
        conn.close()

    migrated = SessionDB(db_path=path)
    try:
        conn = sqlite3.connect(str(path))
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(session_inbox)")}
            assert {
                "key", "profile", "platform", "chat_id", "thread_id", "session_key",
                "owner_root_id", "kind", "wake", "title", "payload_json",
                "shown_to_user", "state", "seq", "created_at", "ingested_at",
                "ingested_session_id", "ingested_message_id", "drop_reason",
                "payload_pruned_at", "notice_state",
            } <= cols
            indexes = {
                tuple(r[2] for r in conn.execute(f"PRAGMA index_info('{idx[1]}')"))
                for idx in conn.execute("PRAGMA index_list(session_inbox)")
            }
            assert ("profile", "platform", "chat_id", "thread_id", "state") in indexes
            assert ("owner_root_id", "state") in indexes
            assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        finally:
            conn.close()
        assert [m["content"] for m in migrated.get_messages("old")] == ["legacy message"]
        assert migrated.inbox_put(_record()) == "inserted"
    finally:
        migrated.close()


def test_schema_rejects_bad_enum_values_at_the_database(db, tmp_path):
    conn = _raw(tmp_path / "state.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO session_inbox (key, kind, state, seq, created_at) VALUES ('x', 'delegation', 'weird', 1, 0)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO session_inbox (key, kind, state, seq, created_at) VALUES ('y', 'nope', 'pending', 2, 0)"
            )
    finally:
        conn.close()
