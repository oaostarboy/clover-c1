"""A briefly locked state.db must not fail a helper's turn.

Field failure: three helpers started together; one hit ``database is locked``
on the "does this session exist?" probe, which gave up at once. The caller
then assumed the row existed, skipped creating it, and the first transcript
write died on a FOREIGN KEY error -> "session storage could not be written".

Contracts:
* the probe (``get_session``) retries a transient lock for a bounded time;
* a lock released inside the budget -> success, no error;
* a lock held past the budget -> a clean ``database is locked`` error
  (no hang, no wrong answer);
* a probe that still fails never marks the session row as created.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from clover_state import SessionDB


@pytest.fixture
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    d.close()


def _flaky_read(db, locked_times):
    """Make the first ``locked_times`` reads raise 'database is locked'."""
    real = db._read_ctx
    state = {"left": locked_times, "calls": 0}

    from contextlib import contextmanager

    @contextmanager
    def ctx():
        state["calls"] += 1
        if state["left"] > 0:
            state["left"] -= 1
            raise sqlite3.OperationalError("database is locked")
        with real() as conn:
            yield conn

    db._read_ctx = ctx
    return state


def test_get_session_retries_a_transient_lock(db):
    db.create_session("s1", source="test")
    state = _flaky_read(db, 3)
    started = time.monotonic()
    row = db.get_session("s1")
    assert row is not None and row["id"] == "s1"
    assert state["calls"] == 4, "three locked reads, then success"
    assert time.monotonic() - started < db._READ_LOCK_PATIENCE_S


def test_get_session_missing_row_after_a_lock_is_none_not_an_error(db):
    _flaky_read(db, 2)
    assert db.get_session("nope") is None


def test_get_session_fails_cleanly_when_the_lock_outlasts_the_budget(db, monkeypatch):
    monkeypatch.setattr(db, "_READ_LOCK_PATIENCE_S", 0.4)
    state = _flaky_read(db, 10_000)
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        db.get_session("s1")
    elapsed = time.monotonic() - started
    assert 0.3 <= elapsed < 3.0, f"bounded wait, got {elapsed:.2f}s"
    assert state["calls"] > 2, "it retried before giving up"


def test_non_lock_errors_are_not_retried(db):
    calls = {"n": 0}
    from contextlib import contextmanager

    @contextmanager
    def ctx():
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: sessions")
        yield  # pragma: no cover

    db._read_ctx = ctx
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        db.get_session("s1")
    assert calls["n"] == 1


def _hold_write_lock(path, hold_s, started):
    conn = sqlite3.connect(str(path), timeout=1.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        started.set()
        time.sleep(hold_s)
        conn.execute("COMMIT")
    finally:
        conn.close()


def test_transcript_write_succeeds_after_a_real_lock_is_released(db, tmp_path):
    """A real second connection holds the write lock briefly; the write lands."""
    db.create_session("s1", source="test")
    started = threading.Event()
    t = threading.Thread(target=_hold_write_lock, args=(tmp_path / "state.db", 1.5, started))
    t.start()
    assert started.wait(5)
    t0 = time.monotonic()
    db.append_message("s1", role="user", content="hello")
    waited = time.monotonic() - t0
    t.join(5)
    assert waited >= 1.0, "it really waited for the lock"
    assert [m["content"] for m in db.get_messages("s1")] == ["hello"]


def test_session_row_creation_succeeds_after_a_real_lock_is_released(db, tmp_path):
    started = threading.Event()
    t = threading.Thread(target=_hold_write_lock, args=(tmp_path / "state.db", 1.0, started))
    t.start()
    assert started.wait(5)
    db.create_session("fresh", source="test")
    t.join(5)
    assert db.get_session("fresh") is not None


def test_write_held_past_the_budget_fails_cleanly(db, tmp_path):
    db.create_session("s1", source="test")
    started = threading.Event()
    t = threading.Thread(target=_hold_write_lock, args=(tmp_path / "state.db", 1.5, started))
    t.start()
    assert started.wait(5)
    t0 = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        db._execute_write(lambda c: c.execute("UPDATE sessions SET model='x' WHERE id='s1'"), patience_s=0.3)
    assert time.monotonic() - t0 < 1.4, "gave up at the budget, did not wait out the holder"
    t.join(5)


def test_a_failed_existence_probe_does_not_claim_the_row_exists(monkeypatch):
    """run_conversation: probe still locked -> take the lease anyway, but leave
    ``_session_db_created`` False so the first flush creates the row."""
    from tests.run_agent.test_cross_process_turn_lease import _DB, _agent_with_db
    from run_agent import AIAgent

    db = _DB()

    def locked(_sid):
        raise sqlite3.OperationalError("database is locked")

    db.get_session = locked
    agent = _agent_with_db(db, session_id="new-helper", platform="subagent")
    agent._session_db_created = False
    monkeypatch.setattr(
        "agent.conversation_loop.run_conversation",
        lambda *_a, **_k: {"final_response": "ok", "messages": [], "failed": False},
    )
    AIAgent.run_conversation(agent, "hi", conversation_history=[])
    assert any(e[0] == "acquire" for e in db.events), "still serialized"
    assert agent._session_db_created is False
