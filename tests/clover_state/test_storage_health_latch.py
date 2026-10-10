"""One "state.db is corrupt" health state shared by every surface (C1.4 R03).

Adapted from NousResearch/hermes-agent bd945ec384 (MIT).  A structurally damaged
state.db used to read as an empty session list, a 500, and green readiness.
These tests damage a real file and drive the real code paths.
"""

from __future__ import annotations

import sqlite3

import pytest

import clover_state_health as health
from clover_state import SessionDB


@pytest.fixture(autouse=True)
def _fresh_latch():
    health.reset_storage_state()
    yield
    health.reset_storage_state()


def _make_store(home, sessions: int = 120):
    path = home / "state.db"
    db = SessionDB(db_path=path)
    for i in range(sessions):
        db.create_session(f"s{i}", source="cli")
        db.append_message(f"s{i}", role="user", content="hello " * 50)
    db.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        root = conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE name='sessions'"
        ).fetchone()[0]
    finally:
        conn.close()
    return path, page_size, root


def _damage(path, page_size, root):
    """Overwrite the sessions table root page with garbage: real B-tree damage."""
    with open(path, "r+b") as fh:
        fh.seek((root - 1) * page_size)
        fh.write(b"\xff" * page_size)


@pytest.fixture
def corrupt_home(tmp_path, monkeypatch):
    import clover_state

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    monkeypatch.setattr(clover_state, "DEFAULT_DB_PATH", home / "state.db")
    path, page_size, root = _make_store(home)
    _damage(path, page_size, root)
    return home


def test_classifier_latches_structural_but_not_fts_or_schema_or_lock():
    structural = sqlite3.DatabaseError("database disk image is malformed")
    assert health.is_structural_corruption_error(structural)
    assert health.is_structural_corruption_error(sqlite3.DatabaseError("file is not a database"))
    # FTS-scoped damage has its own fail-open path
    assert not health.is_structural_corruption_error(
        sqlite3.DatabaseError('fts5: corrupt structure record for table "messages_fts"')
    )
    # malformed schema is healed by the web open path
    assert not health.is_structural_corruption_error(
        sqlite3.DatabaseError("malformed database schema (messages_fts)")
    )
    assert not health.is_structural_corruption_error(sqlite3.OperationalError("database is locked"))
    assert not health.is_structural_corruption_error(RuntimeError("database disk image is malformed"))


def test_latch_is_per_path_and_sticky(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    assert health.storage_state(a) == health.STORAGE_OK
    assert health.note_storage_error(a, sqlite3.DatabaseError("database disk image is malformed"))
    assert health.storage_state(a) == health.STORAGE_CORRUPT
    assert health.storage_state(b) == health.STORAGE_OK
    assert not health.note_storage_error(b, sqlite3.OperationalError("database is locked"))
    assert health.storage_state(b) == health.STORAGE_OK
    # a later healthy-looking observation never clears it
    assert health.storage_state(a) == health.STORAGE_CORRUPT


def test_reader_publishes_corruption_and_readiness_agrees(corrupt_home):
    from gateway.readiness import collect_runtime_readiness

    db = SessionDB(db_path=corrupt_home / "state.db", read_only=True)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            db.list_sessions_rich(limit=10)
    finally:
        db.close()

    assert health.storage_state(corrupt_home / "state.db") == health.STORAGE_CORRUPT

    result = collect_runtime_readiness(
        configured_model="m",
        # the gateway's own cache still says the store is fine
        runtime_status={"gateway_state": "running", "session_store": {"status": "ok"}},
    )
    assert result["status"] == "degraded"
    assert result["checks"]["state_db"] == {"status": "degraded", "detail": "corrupt"}
    assert result["checks"]["session_store"]["status"] == "unavailable"
    assert result["checks"]["session_store"]["detail"] == "corrupt"


def test_failed_write_publishes_corruption(corrupt_home):
    db = SessionDB(db_path=corrupt_home / "state.db")
    try:
        with pytest.raises(sqlite3.DatabaseError):
            for i in range(120):
                db.create_session(f"new{i}", source="cli")
    finally:
        db.close()
    assert health.storage_state(corrupt_home / "state.db") == health.STORAGE_CORRUPT


def test_readiness_green_before_any_damage_is_observed(tmp_path, monkeypatch):
    from gateway.readiness import collect_runtime_readiness

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    _make_store(home, sessions=3)
    result = collect_runtime_readiness(
        configured_model="m", runtime_status={"gateway_state": "running"}
    )
    assert result["checks"]["state_db"]["status"] == "ok"


def test_sessions_endpoint_returns_503_state_db_corrupt(corrupt_home):
    from fastapi.testclient import TestClient

    from clover_cli import web_server

    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    response = client.get("/api/sessions?limit=20&offset=0")
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "state_db_corrupt"

    # Same fact on the other surfaces, without touching the file again.
    profiles = client.get("/api/profiles/sessions?limit=20")
    assert profiles.status_code == 200
    assert profiles.json()["storage"] == {"default": "corrupt"}
    sidebar = client.get("/api/profiles/sessions/sidebar")
    assert sidebar.status_code == 200
    assert sidebar.json()["storage"] == {"default": "corrupt"}
    from gateway.readiness import _probe_state_db

    assert _probe_state_db(corrupt_home)["detail"] == "corrupt"


def test_healthy_sessions_endpoint_has_empty_storage_map(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from clover_cli import web_server

    import clover_state

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    monkeypatch.setattr(clover_state, "DEFAULT_DB_PATH", home / "state.db")
    _make_store(home, sessions=3)
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    response = client.get("/api/sessions?limit=20&offset=0")
    assert response.status_code == 200
    assert response.json()["storage"] == {}
