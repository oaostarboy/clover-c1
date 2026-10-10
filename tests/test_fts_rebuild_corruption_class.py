"""rebuild_fts()/optimize_fts() must survive a corrupt-image DatabaseError.

SQLITE_CORRUPT ("database disk image is malformed") is raised as
sqlite3.DatabaseError, a *parent* of OperationalError, not a subclass. The
rebuild is the recovery path for a corrupt FTS index, so the error it hits is
exactly this class. Catching only OperationalError let it escape the
per-index loop without a rollback.
"""

from __future__ import annotations

import sqlite3

import pytest

from clover_state import SessionDB


class _FailingConn:
    """Proxy for the sqlite3 connection (its ``execute`` is read-only on the
    C type) that raises a malformed-image error for chosen FTS commands."""

    def __init__(self, real, command, match):
        self._real = real
        self._command = command
        self._match = match
        self.rollbacks = 0

    def execute(self, sql, *args, **kwargs):
        if f"VALUES('{self._command}')" in sql and self._match(sql):
            # Leave a transaction open, as a half-finished rebuild would.
            if not self._real.in_transaction:
                self._real.execute("BEGIN IMMEDIATE")
            raise sqlite3.DatabaseError("database disk image is malformed")
        return self._real.execute(sql, *args, **kwargs)

    def rollback(self):
        self.rollbacks += 1
        return self._real.rollback()

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    if not d._fts_enabled:
        d.close()
        pytest.skip("FTS5 unavailable in this build")
    d.create_session("s1", source="test")
    d.append_message("s1", "user", "hello world")
    yield d
    try:
        d.close()
    except Exception:
        pass


def test_rebuild_catches_corruption_rolls_back_and_points_to_doctor(
    db, caplog
):
    real = db._conn
    db._conn = _FailingConn(real, "rebuild", lambda sql: True)
    try:
        with caplog.at_level("ERROR"):
            assert db.rebuild_fts() == 0
        assert db._conn.rollbacks >= 1
        assert real.in_transaction is False
    finally:
        db._conn = real
    assert any(
        rec.levelname == "ERROR" and "clover doctor" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.parametrize(
    "method,command", [("rebuild_fts", "rebuild"), ("optimize_fts", "optimize")]
)
def test_one_corrupt_index_does_not_stop_the_other_indexes(
    db, method, command
):
    real = db._conn
    # The trailing "(" keeps the match off messages_fts_trigram/_cjk.
    db._conn = _FailingConn(
        real, command, lambda sql: sql.startswith("INSERT INTO messages_fts(")
    )
    try:
        result = getattr(db, method)()
        assert real.in_transaction is False
    finally:
        db._conn = real
    assert result >= 1
