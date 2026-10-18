"""Separately versioned opt-in presence store, never a cron job/obligation.

Connection-per-operation follows cron/notepad.py. No private SQL helpers are
shared. The profile home is resolved at operation time; imports create nothing.
Version zero means a genuinely empty DB only. Unknown/corrupt stores are held,
never repaired or overwritten. No emotion sample/history is stored here.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
from typing import Iterator

from agent.presence_state import ContextKey, EvidenceRef, Thought, PresenceStoreError
from agent.workstreams import Scope
from clover_constants import get_clover_home

SCHEMA_VERSION = 1
MAX_RECORDS = 128  # Per profile, each table, including tombstones.
MAX_STORE_BYTES = 64 * 1024
_SOURCE_DDL = """CREATE TABLE presence_invalidations (
    context TEXT NOT NULL, source_owner TEXT NOT NULL, source_id TEXT NOT NULL,
    revision INTEGER NOT NULL, status TEXT NOT NULL,
    PRIMARY KEY (context, source_owner, source_id))"""
_DDL = """CREATE TABLE presence_thoughts (
    thought_id TEXT PRIMARY KEY, context TEXT NOT NULL,
    source_owner TEXT NOT NULL, source_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL, purpose TEXT NOT NULL,
    record TEXT NOT NULL)"""


def _context(key: ContextKey) -> str:
    return json.dumps(asdict(key), sort_keys=True, separators=(",", ":"))


def _encode(thought: Thought) -> str:
    data = asdict(thought)
    for name in ("created_at", "updated_at", "expires_at"):
        data[name] = getattr(thought, name).isoformat()
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _decode(raw: str) -> Thought:
    data = json.loads(raw)
    key = data.pop("key")
    key = ContextKey(Scope(**key["scope"]), key["conversation_root"])
    source = EvidenceRef(**data.pop("source"))
    for name in ("created_at", "updated_at", "expires_at"):
        data[name] = datetime.fromisoformat(data[name])
    return Thought(key=key, source=source, **data)


def _validate_records(conn: sqlite3.Connection) -> None:
    for row in conn.execute("SELECT thought_id,context,source_owner,source_id,source_revision,purpose,record FROM presence_thoughts"):
        thought = _decode(row[6])
        expected = (thought.thought_id, _context(thought.key), thought.source.owner,
                    thought.source.source_id, thought.source.revision, thought.purpose)
        if tuple(row[:6]) != expected:
            raise PresenceStoreError("thought provenance/index mismatch")
    for context, owner, source_id, revision, status in conn.execute("SELECT context,source_owner,source_id,revision,status FROM presence_invalidations"):
        key = json.loads(context)
        ContextKey(Scope(**key["scope"]), key["conversation_root"])
        EvidenceRef(owner, source_id, revision, "0" * 64)
        if status not in {"corrected", "deleted", "invalidated"}:
            raise PresenceStoreError("invalid correction tombstone")


def _check_capacity(conn: sqlite3.Connection) -> None:
    count, size = conn.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(record AS BLOB))),0) FROM presence_thoughts").fetchone()
    guards, guard_size = conn.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(context || source_owner || source_id || status AS BLOB)) + 8),0) FROM presence_invalidations").fetchone()
    if count > MAX_RECORDS or guards > MAX_RECORDS or size + guard_size > MAX_STORE_BYTES:
        raise PresenceStoreError("presence capacity exceeded")


class ThoughtStore:
    """Low-level trusted repository. Hosts authenticate admission and controls."""

    @property
    def path(self) -> Path:
        return get_clover_home().resolve() / "cron" / "presence.db"

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection | None]:
        path = self.path
        if not write and not path.exists():
            yield None
            return
        conn = None
        try:
            if write:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
            conn = sqlite3.connect(path if write else path.as_uri() + "?mode=ro", timeout=5, uri=not write)
            conn.execute("PRAGMA busy_timeout=5000")
            if write:
                # Serialize migration, duplicate detection and mutation across
                # independent connections/processes, not a Python-only lock.
                conn.execute("BEGIN IMMEDIATE")
            # Check BEFORE any journal/schema mutation. Unsupported stores remain
            # byte-unchanged, including when an admission is attempted.
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version not in (0, SCHEMA_VERSION) or (version == 0 and tables):
                raise PresenceStoreError("unsupported presence schema")
            if version == SCHEMA_VERSION and tables != {"presence_thoughts", "presence_invalidations"}:
                raise PresenceStoreError("incomplete presence schema")
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise PresenceStoreError("corrupt presence store")
            if write:
                if version == 0:
                    # The only supported migration: empty -> first candidate
                    # schema. No deployed legacy emotion data is imported.
                    conn.execute(_DDL)
                    conn.execute(_SOURCE_DDL)
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            if version == SCHEMA_VERSION:
                _check_capacity(conn)
                _validate_records(conn)
            yield conn
            if write:
                _check_capacity(conn)
                _validate_records(conn)
                conn.commit()
        except PresenceStoreError:
            if conn is not None:
                conn.rollback()
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            if conn is not None:
                conn.rollback()
            raise PresenceStoreError("presence store unavailable") from exc
        finally:
            if conn is not None:
                conn.close()

    def put(self, thought: Thought) -> Thought | None:
        with self._transaction(write=True) as conn:
            assert conn is not None
            blocked = conn.execute("SELECT revision FROM presence_invalidations WHERE context=? AND source_owner=? AND source_id=?",
                                   (_context(thought.key), thought.source.owner, thought.source.source_id)).fetchone()
            if blocked and blocked[0] >= thought.source.revision:
                return None
            previous = conn.execute("SELECT record FROM presence_thoughts WHERE context=? AND source_owner=? AND source_id=?", (
                _context(thought.key), thought.source.owner, thought.source.source_id)).fetchall()
            existing = [_decode(row[0]) for row in previous]
            for old in existing:
                if old.source.revision > thought.source.revision:
                    return None
                if old.source.revision == thought.source.revision and (old.status != "pending" or old.source != thought.source):
                    return None
            for old in existing:
                if old.source == thought.source and not old.created_at <= thought.created_at < old.expires_at:
                    changed = replace(old, status="expired", summary="", revision=old.revision + 1,
                                      updated_at=max(thought.created_at, old.created_at))
                    conn.execute("UPDATE presence_thoughts SET record=? WHERE thought_id=? AND context=?",
                                 (_encode(changed), old.thought_id, _context(thought.key)))
                    return None
            for old in existing:
                if old.source == thought.source and old.purpose == thought.purpose:
                    return old
            for old in existing:
                if old.source.revision < thought.source.revision and old.status == "pending":
                    changed = replace(old, status="corrected", summary="", revision=old.revision + 1,
                                      updated_at=max(thought.created_at, old.created_at))
                    conn.execute("UPDATE presence_thoughts SET record=? WHERE thought_id=? AND context=?",
                                 (_encode(changed), old.thought_id, _context(thought.key)))
            conn.execute("INSERT INTO presence_thoughts VALUES (?,?,?,?,?,?,?)", (
                thought.thought_id, _context(thought.key), thought.source.owner,
                thought.source.source_id, thought.source.revision, thought.purpose,
                _encode(thought)))
            return thought

    def invalidate_source(self, key: ContextKey, source: EvidenceRef, status: str, now: datetime) -> int:
        with self._transaction(write=True) as conn:
            assert conn is not None
            conn.execute("INSERT INTO presence_invalidations VALUES (?,?,?,?,?) ON CONFLICT(context,source_owner,source_id) DO UPDATE SET revision=MAX(revision,excluded.revision), status=excluded.status",
                         (_context(key), source.owner, source.source_id, source.revision, status))
            rows = conn.execute("SELECT record FROM presence_thoughts WHERE context=? AND source_owner=? AND source_id=? AND source_revision<=?",
                                (_context(key), source.owner, source.source_id, source.revision)).fetchall()
            changed = 0
            for row in rows:
                old = _decode(row[0])
                if old.status != "pending":
                    continue
                new = replace(old, status=status, summary="", revision=old.revision + 1, updated_at=now)
                conn.execute("UPDATE presence_thoughts SET record=? WHERE thought_id=? AND context=?",
                             (_encode(new), old.thought_id, _context(key)))
                changed += 1
            return changed

    def invalidate(self, thought: Thought, status: str, now: datetime) -> bool:
        with self._transaction(write=True) as conn:
            assert conn is not None
            changed = replace(thought, status=status, summary="",
                              revision=thought.revision + 1, updated_at=now)
            result = conn.execute("UPDATE presence_thoughts SET record=? WHERE thought_id=? AND context=? AND record=?",
                                  (_encode(changed), thought.thought_id, _context(thought.key), _encode(thought)))
            return result.rowcount == 1

    def source_is_current(self, key: ContextKey, source: EvidenceRef) -> bool:
        """Read-only high-water/tombstone check; no affect data is written."""
        with self._transaction() as conn:
            if conn is None or conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                return True
            blocked = conn.execute("SELECT revision FROM presence_invalidations WHERE context=? AND source_owner=? AND source_id=?",
                                   (_context(key), source.owner, source.source_id)).fetchone()
            if blocked and blocked[0] >= source.revision:
                return False
            rows = conn.execute("SELECT record FROM presence_thoughts WHERE context=? AND source_owner=? AND source_id=? AND source_revision>=?",
                                (_context(key), source.owner, source.source_id, source.revision))
            return all((old := _decode(row[0])).source == source and old.status == "pending" for row in rows)

    def list(self, key: ContextKey) -> tuple[Thought, ...]:
        with self._transaction() as conn:
            if conn is None or conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                return ()
            return tuple(_decode(row[0]) for row in conn.execute(
                "SELECT record FROM presence_thoughts WHERE context=? ORDER BY thought_id", (_context(key),)))
