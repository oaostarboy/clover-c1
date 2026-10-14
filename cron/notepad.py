"""Per-job durable notepad for cron jobs.

A tiny KV scratchpad each cron job can use to carry state across scheduled
wake-ups (cursors, watermarks, watchlists). Stored in its own profile-local
SQLite file next to the executions ledger, following the same
connection/pragma pattern as ``cron/executions.py``.

Size caps (documented contract):

- ``MAX_VALUE_BYTES`` (16 KB): per-key value cap, measured in UTF-8 bytes.
- ``MAX_JOB_TOTAL_BYTES`` (64 KB): per-job cap over the sum of key+value
  bytes. Oversized writes raise ``ValueError`` and leave the store
  untouched — the notepad is prompt-injected each run, so unbounded growth
  would bloat every wake-up's prompt.

Write path is the CLI (``clover cron notepad <job_id> set <key> <value>``),
which the running agent invokes via its terminal tool; no model tool is
added.

The same DB also hosts the opt-in workstream ledger (see the section at the
end of this module and ``agent/workstreams.py``). The legacy KV functions
never create or touch those tables, and the DB path is resolved from the
active profile at transaction time (``NOTEPAD_FILE`` is a test override).

Inspired by: Amp (Sourcegraph) cron notepad (idea-level, proprietary — zero
code).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence

from clover_constants import get_clover_home
from clover_time import now as _clover_now

# Optional override (tests). Production resolves the active profile's path at
# transaction time, like ``cron/executions.py``, so a process that enters
# another profile never writes into the import-time home.
NOTEPAD_FILE: Optional[Path] = None
MAX_VALUE_BYTES = 16 * 1024
MAX_KEY_CHARS = 128
MAX_JOB_TOTAL_BYTES = 64 * 1024
_lock = threading.RLock()


def _notepad_path() -> Path:
    return NOTEPAD_FILE or (get_clover_home().resolve() / "cron" / "notepad.db")


def _connect() -> sqlite3.Connection:
    from cron.jobs import _ensure_cron_dir

    path = _notepad_path()
    _ensure_cron_dir(path.parent)
    return sqlite3.connect(path, timeout=5)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from clover_state import apply_wal_with_fallback

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    apply_wal_with_fallback(conn, db_label="cron/notepad.db")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cron_notepad (
             job_id TEXT NOT NULL,
             key TEXT NOT NULL,
             value TEXT NOT NULL,
             updated_at TEXT NOT NULL,
             PRIMARY KEY (job_id, key)
           )"""
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, always close.

    Mirrors ``cron.executions._transaction``: schema init runs inside the
    ``try`` so a PRAGMA/DDL failure still closes the connection instead of
    leaking it.
    """
    with _lock:
        conn = _connect()
        try:
            _initialize_schema(conn)
            with conn:
                yield conn
        finally:
            conn.close()


def _validate(job_id: str, key: str, value: str) -> None:
    if not str(job_id):
        raise ValueError("job_id must be non-empty")
    if not key:
        raise ValueError("key must be non-empty")
    if len(key) > MAX_KEY_CHARS:
        raise ValueError(f"key too long (max {MAX_KEY_CHARS} characters)")
    if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        raise ValueError(
            f"value too large (max {MAX_VALUE_BYTES} bytes per key)"
        )


def set_note(job_id: str, key: str, value: str) -> Dict[str, Any]:
    """Upsert one key. Raises ValueError when a size cap would be exceeded."""
    job_id, key, value = str(job_id), str(key), str(value)
    _validate(job_id, key, value)
    now = _clover_now().isoformat()
    with _transaction() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(LENGTH(CAST(key AS BLOB))
                 + LENGTH(CAST(value AS BLOB))), 0)
               FROM cron_notepad WHERE job_id=? AND key<>?""",
            (job_id, key),
        ).fetchone()
        other_bytes = int(row[0])
        entry_bytes = len(key.encode("utf-8")) + len(value.encode("utf-8"))
        if other_bytes + entry_bytes > MAX_JOB_TOTAL_BYTES:
            raise ValueError(
                f"notepad full: job '{job_id}' would exceed "
                f"{MAX_JOB_TOTAL_BYTES} bytes total; delete unused keys first"
            )
        conn.execute(
            """INSERT INTO cron_notepad (job_id, key, value, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(job_id, key)
               DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (job_id, key, value, now),
        )
    return {"job_id": job_id, "key": key, "value": value, "updated_at": now}


def get_note(job_id: str, key: str) -> Optional[str]:
    with _transaction() as conn:
        row = conn.execute(
            "SELECT value FROM cron_notepad WHERE job_id=? AND key=?",
            (str(job_id), str(key)),
        ).fetchone()
    return None if row is None else row["value"]


def delete_note(job_id: str, key: str) -> bool:
    with _transaction() as conn:
        cur = conn.execute(
            "DELETE FROM cron_notepad WHERE job_id=? AND key=?",
            (str(job_id), str(key)),
        )
    return cur.rowcount > 0


def list_notes(job_id: str) -> List[Dict[str, Any]]:
    """All entries for one job, sorted by key."""
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT job_id, key, value, updated_at FROM cron_notepad "
            "WHERE job_id=? ORDER BY key",
            (str(job_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def clear_notepad(job_id: str) -> int:
    """Delete every key for one job (e.g. on job removal). Returns row count.

    Called from ``cron.jobs.remove_job`` so deleted jobs don't orphan their
    rows. No-ops without creating the DB when no notepad file exists yet.
    """
    if not _notepad_path().exists():
        return 0
    with _transaction() as conn:
        cur = conn.execute(
            "DELETE FROM cron_notepad WHERE job_id=?", (str(job_id),)
        )
    return cur.rowcount


def render_notepad_section(job_id: str) -> str:
    """Render a job's notepad as a prompt section, or '' when empty/unavailable.

    Empty notepad MUST return the empty string so jobs that never use the
    feature get a byte-identical prompt (prompt-cache + drift safety).
    """
    try:
        notes = list_notes(job_id)
    except Exception:
        return ""
    if not notes:
        return ""
    lines = [f"- {note['key']}: {note['value']}" for note in notes]
    return (
        "## Job notepad (persistent across runs)\n"
        "This durable scratchpad survives between scheduled runs of this "
        "job. Update it via the CLI, e.g.:\n"
        f"`clover cron notepad {job_id} set <key> <value>` "
        f"(also: get/delete/list; `clover cron notepad {job_id} delete "
        "<key>` removes an entry).\n\n" + "\n".join(lines) + "\n\n"
    )


# ── Workstream ledger (PLAN-v1) ────────────────────────────────────────────
#
# Typed, scoped, additive tables in the same DB. Legacy KV calls above never
# create or touch them: the workstream migration runs only from the write
# helpers below, and reads return empty without creating a file or tables.
# Callers (``agent/workstreams.py``) gate on ``proactive.enabled`` first.

WORKSTREAM_SCHEMA_VERSION = 1
MAX_WORKSTREAM_SUMMARY_BYTES = 2048
MAX_WORKSTREAM_TITLE_CHARS = 160
MAX_SCOPE_BYTES = 64 * 1024

KINDS = (
    "obligation", "blocker", "correction", "decision", "constraint",
    "progress", "procedure",
)
STATUSES = ("open", "resolved", "superseded", "cancelled", "deferred", "expired")
TERMINAL = ("resolved", "superseded", "cancelled", "expired")
EVIDENCE_STATUSES = ("verified", "failed", "unverified", "unknown")
RECEIPT_KINDS = (
    "proposed", "scheduled", "authorized", "executed", "saved", "delivered",
    "deferred", "cancelled", "unknown",
    "feedback_accept", "feedback_reject", "feedback_correct", "feedback_defer",
    "feedback_done", "feedback_stop",
)
FEEDBACK_KINDS = ("accept", "reject", "correct", "defer", "done", "stop")
OCC_STATES = ("pending", "claimed", "executed", "deferred", "cancelled", "unknown")
OCC_SETTLED = ("executed", "deferred", "cancelled", "unknown")
_BUDGET_KEYS = ("turns", "tokens", "seconds")


class WorkstreamError(Exception):
    """Base for workstream ledger failures."""


class WorkstreamSchemaError(WorkstreamError):
    """On-disk workstream schema is newer than this build understands."""


class WorkstreamCapError(WorkstreamError):
    """A per-scope item or byte cap would be exceeded."""


class StaleRevision(WorkstreamError):
    """A compare-and-set matched no row (stale revision or wrong owner)."""


class NotFound(WorkstreamError):
    """No such record in this exact scope."""


class BudgetExhausted(WorkstreamError):
    """The authority budget cannot cover the requested spend."""


_WS_DDL = (
    """CREATE TABLE IF NOT EXISTS workstream_meta (
         key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS workstreams (
         profile TEXT NOT NULL, project TEXT NOT NULL,
         audience TEXT NOT NULL, root TEXT NOT NULL,
         workstream_id TEXT NOT NULL,
         kind TEXT NOT NULL,
         title TEXT NOT NULL,
         summary TEXT NOT NULL,
         fingerprint TEXT NOT NULL,
         status TEXT NOT NULL,
         revision INTEGER NOT NULL,
         supersedes_id TEXT,
         session_id TEXT NOT NULL DEFAULT '',
         approved INTEGER NOT NULL DEFAULT 0,
         source TEXT NOT NULL DEFAULT 'user',
         created_at TEXT NOT NULL,
         updated_at TEXT NOT NULL,
         expires_at TEXT,
         PRIMARY KEY (profile, project, audience, root, workstream_id))""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_workstreams_live_fp
         ON workstreams (profile, project, audience, root, fingerprint)
         WHERE status IN ('open','deferred')""",
    """CREATE TABLE IF NOT EXISTS work_evidence (
         evidence_id TEXT PRIMARY KEY,
         profile TEXT NOT NULL, project TEXT NOT NULL,
         audience TEXT NOT NULL, root TEXT NOT NULL,
         workstream_id TEXT NOT NULL,
         source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
         status TEXT NOT NULL,
         trust TEXT NOT NULL DEFAULT 'runtime',
         key TEXT NOT NULL DEFAULT '',
         procedure_key TEXT NOT NULL DEFAULT '',
         session_id TEXT NOT NULL DEFAULT '',
         summary TEXT NOT NULL DEFAULT '',
         event_at TEXT, observed_at TEXT NOT NULL,
         UNIQUE (profile, project, audience, root, workstream_id,
                 source_kind, source_id))""",
    """CREATE TABLE IF NOT EXISTS work_receipts (
         receipt_id TEXT PRIMARY KEY,
         profile TEXT NOT NULL, project TEXT NOT NULL,
         audience TEXT NOT NULL, root TEXT NOT NULL,
         workstream_id TEXT NOT NULL,
         kind TEXT NOT NULL, owner TEXT NOT NULL, source_id TEXT NOT NULL,
         detail TEXT NOT NULL DEFAULT '',
         created_at TEXT NOT NULL,
         UNIQUE (profile, project, audience, root, workstream_id,
                 kind, source_id))""",
    """CREATE TABLE IF NOT EXISTS work_feedback (
         feedback_id TEXT PRIMARY KEY,
         profile TEXT NOT NULL, project TEXT NOT NULL,
         audience TEXT NOT NULL, root TEXT NOT NULL,
         workstream_id TEXT NOT NULL,
         kind TEXT NOT NULL, actor TEXT NOT NULL,
         note TEXT NOT NULL DEFAULT '',
         defer_until TEXT,
         created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS work_history (
         history_id INTEGER PRIMARY KEY AUTOINCREMENT,
         profile TEXT NOT NULL, project TEXT NOT NULL,
         audience TEXT NOT NULL, root TEXT NOT NULL,
         workstream_id TEXT NOT NULL,
         revision INTEGER NOT NULL, op TEXT NOT NULL, actor TEXT NOT NULL,
         detail TEXT NOT NULL DEFAULT '',
         created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS work_occurrences (
         profile TEXT NOT NULL, project TEXT NOT NULL,
         audience TEXT NOT NULL, root TEXT NOT NULL,
         workstream_id TEXT NOT NULL,
         occurrence_key TEXT NOT NULL,
         source TEXT NOT NULL DEFAULT 'monitor',
         state TEXT NOT NULL,
         claimer TEXT NOT NULL DEFAULT '',
         detail TEXT NOT NULL DEFAULT '',
         created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
         PRIMARY KEY (profile, project, audience, root, workstream_id,
                      occurrence_key))""",
    """CREATE TABLE IF NOT EXISTS work_authority (
         profile TEXT NOT NULL, project TEXT NOT NULL,
         audience TEXT NOT NULL, root TEXT NOT NULL,
         workstream_id TEXT NOT NULL,
         plan_ref TEXT NOT NULL,
         budget TEXT NOT NULL,
         linked_at TEXT NOT NULL,
         PRIMARY KEY (profile, project, audience, root, workstream_id))""",
)


def _utc_iso(now: Optional[datetime] = None) -> str:
    moment = now if now is not None else _clover_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _scope_key(scope: Any) -> tuple:
    key = tuple(
        str(getattr(scope, name, "") if getattr(scope, name, None) is not None else "")
        for name in ("profile", "project", "audience", "root")
    )
    if not key[0] or not key[1] or not key[2]:
        raise ValueError("scope requires profile, project and audience")
    return key


_SCOPE_WHERE = "profile=? AND project=? AND audience=? AND root=?"


def _meta_version(conn: sqlite3.Connection) -> int:
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workstream_meta'"
    ).fetchone()
    if not has:
        return 0
    row = conn.execute(
        "SELECT value FROM workstream_meta WHERE key='schema_version'"
    ).fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def _migrate_workstreams(conn: sqlite3.Connection) -> None:
    version = _meta_version(conn)
    if version > WORKSTREAM_SCHEMA_VERSION:
        raise WorkstreamSchemaError(
            f"workstream schema v{version} is newer than supported "
            f"v{WORKSTREAM_SCHEMA_VERSION}; refusing to touch it"
        )
    if version == WORKSTREAM_SCHEMA_VERSION:
        return
    for ddl in _WS_DDL:
        conn.execute(ddl)
    conn.execute(
        "INSERT INTO workstream_meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(WORKSTREAM_SCHEMA_VERSION),),
    )


@contextmanager
def _ws_write() -> Iterator[sqlite3.Connection]:
    """Write transaction: ``BEGIN IMMEDIATE`` so cap checks and CAS are
    serialised across processes, migration inside the same transaction."""
    with _lock:
        conn = _connect()
        try:
            _initialize_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                _migrate_workstreams(conn)
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            conn.close()


@contextmanager
def _ws_read() -> Iterator[Optional[sqlite3.Connection]]:
    """Read connection, or ``None`` when no DB/workstream tables exist yet.

    Never creates the file or any table. A newer schema fails closed.
    """
    with _lock:
        path = _notepad_path()
        if not path.exists():
            yield None
            return
        conn = sqlite3.connect(path, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            version = _meta_version(conn)
            if version > WORKSTREAM_SCHEMA_VERSION:
                raise WorkstreamSchemaError(
                    f"workstream schema v{version} is newer than supported "
                    f"v{WORKSTREAM_SCHEMA_VERSION}"
                )
            yield conn if version else None
        finally:
            conn.close()


def workstream_schema_version(*, path: Optional[Path] = None) -> int:
    """On-disk workstream schema version; 0 when absent. Never creates."""
    target = path or _notepad_path()
    if not Path(target).exists():
        return 0
    conn = sqlite3.connect(target, timeout=5)
    try:
        return _meta_version(conn)
    finally:
        conn.close()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out = dict(row)
    if "approved" in out:
        out["approved"] = bool(out["approved"])
    return out


def _history(conn, key, workstream_id, revision, op, actor, detail, at) -> None:
    conn.execute(
        "INSERT INTO work_history (profile, project, audience, root, "
        "workstream_id, revision, op, actor, detail, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (*key, workstream_id, revision, op, actor, detail, at),
    )


def _fetch(conn, key, workstream_id) -> Optional[Dict[str, Any]]:
    return _row(conn.execute(
        f"SELECT * FROM workstreams WHERE {_SCOPE_WHERE} AND workstream_id=?",
        (*key, str(workstream_id)),
    ).fetchone())


def _require(conn, key, workstream_id) -> Dict[str, Any]:
    row = _fetch(conn, key, workstream_id)
    if row is None:
        raise NotFound(f"no workstream {workstream_id!r} in this scope")
    return row


def _check_choice(value: str, allowed: Sequence[str], what: str) -> str:
    if value not in allowed:
        raise ValueError(f"unknown {what}: {value!r}")
    return value


# -- reads --------------------------------------------------------------

def get_workstream(scope: Any, workstream_id: str) -> Optional[Dict[str, Any]]:
    key = _scope_key(scope)
    with _ws_read() as conn:
        return None if conn is None else _fetch(conn, key, workstream_id)


def list_workstreams(
    scope: Any, *, statuses: Optional[Sequence[str]] = None,
    kinds: Optional[Sequence[str]] = None, limit: int = 100,
) -> List[Dict[str, Any]]:
    key = _scope_key(scope)
    clauses, params = [_SCOPE_WHERE], list(key)
    if statuses:
        clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
        params.extend(statuses)
    if kinds:
        clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
        params.extend(kinds)
    params.append(max(1, min(int(limit), 500)))
    with _ws_read() as conn:
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT * FROM workstreams WHERE " + " AND ".join(clauses)
            + " ORDER BY updated_at DESC, workstream_id LIMIT ?",
            params,
        ).fetchall()
    return [_row(r) for r in rows]


def _list_children(table: str, order: str, scope: Any, workstream_id: Optional[str],
                   kinds: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    key = _scope_key(scope)
    clauses, params = [_SCOPE_WHERE], list(key)
    if workstream_id is not None:
        clauses.append("workstream_id=?")
        params.append(str(workstream_id))
    if kinds:
        clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
        params.extend(kinds)
    with _ws_read() as conn:
        if conn is None:
            return []
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE " + " AND ".join(clauses)
            + f" ORDER BY {order}",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def list_evidence(scope: Any, workstream_id: str) -> List[Dict[str, Any]]:
    return _list_children("work_evidence", "observed_at, evidence_id", scope, workstream_id)


def list_receipts(scope: Any, workstream_id: Optional[str] = None, *,
                  kinds: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    return _list_children("work_receipts", "created_at, receipt_id", scope,
                          workstream_id, kinds)


def list_feedback(scope: Any, workstream_id: Optional[str] = None) -> List[Dict[str, Any]]:
    return _list_children("work_feedback", "created_at, feedback_id", scope, workstream_id)


def list_history(scope: Any, workstream_id: str) -> List[Dict[str, Any]]:
    return _list_children("work_history", "history_id", scope, workstream_id)


def list_procedure_scopes() -> List[Dict[str, str]]:
    """Distinct scopes holding verified procedure-run evidence (intake sweep).

    Never creates the DB or tables. Rows are plain dicts with the four scope
    fields, ordered for deterministic sweeps.
    """
    with _ws_read() as conn:
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT DISTINCT profile, project, audience, root FROM work_evidence "
            "WHERE procedure_key<>'' AND status='verified' "
            "ORDER BY profile, project, audience, root"
        ).fetchall()
    return [dict(r) for r in rows]


def list_scope_evidence(scope: Any, *, procedure_key: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every evidence row in a scope (procedure intake reads this)."""
    rows = _list_children("work_evidence", "observed_at, evidence_id", scope, None)
    if procedure_key is not None:
        rows = [r for r in rows if r.get("procedure_key") == procedure_key]
    return rows


# -- records ------------------------------------------------------------

def create_workstream(
    scope: Any, *, kind: str, title: str, summary: str, fingerprint: str,
    session_id: str = "", approved: bool = False, source: str = "user",
    expires_at: Optional[str] = None, max_items: int = 16,
    max_bytes: int = MAX_SCOPE_BYTES, workstream_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Create one scoped record; idempotent per live (scope, fingerprint)."""
    key = _scope_key(scope)
    _check_choice(kind, KINDS, "kind")
    summary, fingerprint = str(summary), str(fingerprint)
    if not fingerprint:
        raise ValueError("fingerprint must be non-empty")
    if len(summary.encode("utf-8")) > MAX_WORKSTREAM_SUMMARY_BYTES:
        raise ValueError(
            f"summary too large (max {MAX_WORKSTREAM_SUMMARY_BYTES} bytes)"
        )
    title = str(title)[:MAX_WORKSTREAM_TITLE_CHARS]
    at = _utc_iso(now)
    with _ws_write() as conn:
        existing = conn.execute(
            f"SELECT * FROM workstreams WHERE {_SCOPE_WHERE} AND fingerprint=? "
            "AND status IN ('open','deferred')",
            (*key, fingerprint),
        ).fetchone()
        if existing is not None:
            return _row(existing)
        live = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(summary AS BLOB)) "
            "+ LENGTH(CAST(title AS BLOB))), 0) FROM workstreams "
            f"WHERE {_SCOPE_WHERE} AND status IN ('open','deferred')",
            key,
        ).fetchone()
        entry_bytes = len(summary.encode("utf-8")) + len(title.encode("utf-8"))
        if int(live[0]) >= int(max_items):
            raise WorkstreamCapError(
                f"scope already holds {live[0]} live items (max {max_items})"
            )
        if int(live[1]) + entry_bytes > int(max_bytes):
            raise WorkstreamCapError(
                f"scope would exceed {max_bytes} bytes of live workstream records"
            )
        wid = workstream_id or _new_id("ws")
        conn.execute(
            "INSERT INTO workstreams (profile, project, audience, root, "
            "workstream_id, kind, title, summary, fingerprint, status, revision, "
            "session_id, approved, source, created_at, updated_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'open',1,?,?,?,?,?,?)",
            (*key, wid, kind, title, summary, fingerprint, str(session_id),
             1 if approved else 0, str(source), at, at, expires_at),
        )
        _history(conn, key, wid, 1, "create", str(source), kind, at)
        return _fetch(conn, key, wid)


def update_workstream(
    scope: Any, workstream_id: str, *, expected_revision: int,
    status: Optional[str] = None, summary: Optional[str] = None,
    approved: Optional[bool] = None, actor: str = "user",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    key = _scope_key(scope)
    if status is not None:
        _check_choice(status, STATUSES, "status")
    if summary is not None and len(str(summary).encode("utf-8")) > MAX_WORKSTREAM_SUMMARY_BYTES:
        raise ValueError(f"summary too large (max {MAX_WORKSTREAM_SUMMARY_BYTES} bytes)")
    at = _utc_iso(now)
    with _ws_write() as conn:
        current = _require(conn, key, workstream_id)
        sets, params = ["revision=revision+1", "updated_at=?"], [at]
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if summary is not None:
            sets.append("summary=?")
            params.append(str(summary))
        if approved is not None:
            sets.append("approved=?")
            params.append(1 if approved else 0)
        cur = conn.execute(
            f"UPDATE workstreams SET {', '.join(sets)} WHERE {_SCOPE_WHERE} "
            "AND workstream_id=? AND revision=?",
            (*params, *key, str(workstream_id), int(expected_revision)),
        )
        if cur.rowcount != 1:
            raise StaleRevision(
                f"{workstream_id}: expected r{expected_revision}, "
                f"found r{current['revision']}"
            )
        _history(conn, key, str(workstream_id), int(expected_revision) + 1,
                 "update", str(actor), status or "", at)
        return _fetch(conn, key, workstream_id)


def supersede_workstream(
    scope: Any, workstream_id: str, *, expected_revision: int, kind: str,
    title: str, summary: str, fingerprint: str, actor: str = "user",
    session_id: str = "", now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Atomically retire one record and create its replacement."""
    key = _scope_key(scope)
    _check_choice(kind, KINDS, "kind")
    summary, title = str(summary), str(title)[:MAX_WORKSTREAM_TITLE_CHARS]
    if len(summary.encode("utf-8")) > MAX_WORKSTREAM_SUMMARY_BYTES:
        raise ValueError(f"summary too large (max {MAX_WORKSTREAM_SUMMARY_BYTES} bytes)")
    at = _utc_iso(now)
    with _ws_write() as conn:
        current = _require(conn, key, workstream_id)
        cur = conn.execute(
            f"UPDATE workstreams SET status='superseded', revision=revision+1, "
            f"updated_at=? WHERE {_SCOPE_WHERE} AND workstream_id=? AND revision=?",
            (at, *key, str(workstream_id), int(expected_revision)),
        )
        if cur.rowcount != 1:
            raise StaleRevision(
                f"{workstream_id}: expected r{expected_revision}, "
                f"found r{current['revision']}"
            )
        _history(conn, key, str(workstream_id), int(expected_revision) + 1,
                 "supersede", str(actor), "", at)
        wid = _new_id("ws")
        conn.execute(
            "INSERT INTO workstreams (profile, project, audience, root, "
            "workstream_id, kind, title, summary, fingerprint, status, revision, "
            "supersedes_id, session_id, approved, source, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'open',1,?,?,?,?,?,?)",
            (*key, wid, kind, title, summary, str(fingerprint), str(workstream_id),
             str(session_id), int(current["approved"]), str(actor), at, at),
        )
        _history(conn, key, wid, 1, "create", str(actor),
                 f"supersedes {workstream_id}", at)
        return _fetch(conn, key, wid)


def add_evidence(
    scope: Any, workstream_id: str, *, source_kind: str, source_id: str,
    status: str, summary: str = "", session_id: str = "", key: str = "",
    procedure_key: str = "", event_at: Optional[str] = None,
    trust: str = "runtime", now: Optional[datetime] = None,
) -> Dict[str, Any]:
    skey = _scope_key(scope)
    _check_choice(status, EVIDENCE_STATUSES, "evidence status")
    if not source_kind or not source_id:
        raise ValueError("evidence needs source_kind and source_id")
    summary = str(summary)[:512]
    at = _utc_iso(now)
    with _ws_write() as conn:
        _require(conn, skey, workstream_id)
        found = conn.execute(
            f"SELECT * FROM work_evidence WHERE {_SCOPE_WHERE} AND workstream_id=? "
            "AND source_kind=? AND source_id=?",
            (*skey, str(workstream_id), str(source_kind), str(source_id)),
        ).fetchone()
        if found is not None:
            # verified/failed are settled; unknown/unverified may resolve once.
            if found["status"] in ("unknown", "unverified") and status != found["status"]:
                conn.execute(
                    "UPDATE work_evidence SET status=?, summary=?, event_at=?, "
                    "observed_at=? WHERE evidence_id=?",
                    (status, summary, event_at, at, found["evidence_id"]),
                )
                found = conn.execute(
                    "SELECT * FROM work_evidence WHERE evidence_id=?",
                    (found["evidence_id"],),
                ).fetchone()
            return dict(found)
        eid = _new_id("ev")
        conn.execute(
            "INSERT INTO work_evidence (evidence_id, profile, project, audience, "
            "root, workstream_id, source_kind, source_id, status, trust, key, "
            "procedure_key, session_id, summary, event_at, observed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, *skey, str(workstream_id), str(source_kind), str(source_id),
             status, str(trust), str(key), str(procedure_key), str(session_id),
             summary, event_at, at),
        )
        return dict(conn.execute(
            "SELECT * FROM work_evidence WHERE evidence_id=?", (eid,)
        ).fetchone())


def add_receipt(
    scope: Any, workstream_id: str, *, kind: str, owner: str, source_id: str,
    detail: str = "", now: Optional[datetime] = None,
) -> Dict[str, Any]:
    skey = _scope_key(scope)
    _check_choice(kind, RECEIPT_KINDS, "receipt kind")
    if not owner or not source_id:
        raise ValueError("receipt needs owner and source_id")
    at = _utc_iso(now)
    with _ws_write() as conn:
        _require(conn, skey, workstream_id)
        found = conn.execute(
            f"SELECT * FROM work_receipts WHERE {_SCOPE_WHERE} AND workstream_id=? "
            "AND kind=? AND source_id=?",
            (*skey, str(workstream_id), kind, str(source_id)),
        ).fetchone()
        if found is not None:
            return dict(found)
        rid = _new_id("rc")
        conn.execute(
            "INSERT INTO work_receipts (receipt_id, profile, project, audience, "
            "root, workstream_id, kind, owner, source_id, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, *skey, str(workstream_id), kind, str(owner), str(source_id),
             str(detail)[:512], at),
        )
        return dict(conn.execute(
            "SELECT * FROM work_receipts WHERE receipt_id=?", (rid,)
        ).fetchone())


def add_feedback(
    scope: Any, workstream_id: str, *, kind: str, actor: str, note: str = "",
    defer_until: Optional[str] = None, now: Optional[datetime] = None,
    ingress_event_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Append one feedback row.

    With ``ingress_event_id`` the row and its ``feedback_<kind>`` receipt are
    written in ONE transaction and a replay of the same event returns the
    original row (a crash can neither lose the receipt nor duplicate the row).
    """
    skey = _scope_key(scope)
    _check_choice(kind, FEEDBACK_KINDS, "feedback kind")
    receipt_kind = f"feedback_{kind}"
    if ingress_event_id:
        _check_choice(receipt_kind, RECEIPT_KINDS, "receipt kind")
    at = _utc_iso(now)
    with _ws_write() as conn:
        _require(conn, skey, workstream_id)
        if ingress_event_id:
            prior = conn.execute(
                f"SELECT f.* FROM work_receipts r JOIN work_feedback f "
                f"ON f.feedback_id = r.detail WHERE r.profile=? AND r.project=? "
                f"AND r.audience=? AND r.root=? AND r.workstream_id=? "
                f"AND r.kind=? AND r.source_id=?",
                (*skey, str(workstream_id), receipt_kind, str(ingress_event_id)),
            ).fetchone()
            if prior is not None:
                return dict(prior)
        fid = _new_id("fb")
        conn.execute(
            "INSERT INTO work_feedback (feedback_id, profile, project, audience, "
            "root, workstream_id, kind, actor, note, defer_until, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fid, *skey, str(workstream_id), kind, str(actor), str(note)[:512],
             defer_until, at),
        )
        if ingress_event_id:
            conn.execute(
                "INSERT INTO work_receipts (receipt_id, profile, project, audience, "
                "root, workstream_id, kind, owner, source_id, detail, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (_new_id("rc"), *skey, str(workstream_id), receipt_kind,
                 "human_ingress", str(ingress_event_id), fid, at),
            )
        return dict(conn.execute(
            "SELECT * FROM work_feedback WHERE feedback_id=?", (fid,)
        ).fetchone())


_CHILD_TABLES = (
    "work_evidence", "work_receipts", "work_feedback", "work_history",
    "work_occurrences", "work_authority",
)


def prune(scope: Any, *, retention_days: int, now: Optional[datetime] = None) -> int:
    """Expire past-due open rows, then delete terminal rows older than the
    retention window with their children. Returns deleted workstream rows.
    Without workstream tables this is a no-op that creates nothing."""
    key = _scope_key(scope)
    with _ws_read() as probe:
        if probe is None:
            return 0
    moment = now if now is not None else _clover_now()
    at = _utc_iso(moment)
    cutoff = _utc_iso(moment - timedelta(days=int(retention_days)))
    with _ws_write() as conn:
        due = conn.execute(
            f"SELECT workstream_id, revision FROM workstreams WHERE {_SCOPE_WHERE} "
            "AND status IN ('open','deferred') AND expires_at IS NOT NULL "
            "AND expires_at < ?",
            (*key, at),
        ).fetchall()
        for row in due:
            conn.execute(
                f"UPDATE workstreams SET status='expired', revision=revision+1, "
                f"updated_at=? WHERE {_SCOPE_WHERE} AND workstream_id=?",
                (at, *key, row["workstream_id"]),
            )
            _history(conn, key, row["workstream_id"], row["revision"] + 1,
                     "expire", "retention", "", at)
        old = conn.execute(
            f"SELECT workstream_id FROM workstreams WHERE {_SCOPE_WHERE} AND "
            f"status IN ({','.join('?' for _ in TERMINAL)}) AND updated_at < ?",
            (*key, *TERMINAL, cutoff),
        ).fetchall()
        for row in old:
            for table in _CHILD_TABLES:
                conn.execute(
                    f"DELETE FROM {table} WHERE {_SCOPE_WHERE} AND workstream_id=?",
                    (*key, row["workstream_id"]),
                )
            conn.execute(
                f"DELETE FROM workstreams WHERE {_SCOPE_WHERE} AND workstream_id=?",
                (*key, row["workstream_id"]),
            )
        return len(old)


# -- monitor occurrences ------------------------------------------------

def _occ(conn, key, workstream_id, occurrence_key) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        f"SELECT * FROM work_occurrences WHERE {_SCOPE_WHERE} AND workstream_id=? "
        "AND occurrence_key=?",
        (*key, str(workstream_id), str(occurrence_key)),
    ).fetchone()
    return None if row is None else dict(row)


def ensure_occurrence(
    scope: Any, workstream_id: str, *, occurrence_key: str,
    source: str = "monitor", now: Optional[datetime] = None,
) -> Dict[str, Any]:
    key = _scope_key(scope)
    if not occurrence_key:
        raise ValueError("occurrence_key must be non-empty")
    at = _utc_iso(now)
    with _ws_write() as conn:
        _require(conn, key, workstream_id)
        conn.execute(
            "INSERT OR IGNORE INTO work_occurrences (profile, project, audience, "
            "root, workstream_id, occurrence_key, source, state, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,'pending',?,?)",
            (*key, str(workstream_id), str(occurrence_key), str(source), at, at),
        )
        return _occ(conn, key, workstream_id, occurrence_key)


def claim_occurrence(
    scope: Any, workstream_id: str, *, occurrence_key: str, claimer: str,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """CAS pending→claimed. ``None`` unless this call won the claim."""
    key = _scope_key(scope)
    if not claimer:
        raise ValueError("claimer must be non-empty")
    at = _utc_iso(now)
    with _ws_write() as conn:
        cur = conn.execute(
            "UPDATE work_occurrences SET state='claimed', claimer=?, updated_at=? "
            f"WHERE {_SCOPE_WHERE} AND workstream_id=? AND occurrence_key=? "
            "AND state='pending'",
            (str(claimer), at, *key, str(workstream_id), str(occurrence_key)),
        )
        if cur.rowcount != 1:
            return None
        return _occ(conn, key, workstream_id, occurrence_key)


def settle_occurrence(
    scope: Any, workstream_id: str, *, occurrence_key: str, claimer: str,
    state: str, detail: str = "", now: Optional[datetime] = None,
) -> Dict[str, Any]:
    key = _scope_key(scope)
    _check_choice(state, OCC_SETTLED, "settled occurrence state")
    at = _utc_iso(now)
    with _ws_write() as conn:
        cur = conn.execute(
            "UPDATE work_occurrences SET state=?, detail=?, updated_at=? "
            f"WHERE {_SCOPE_WHERE} AND workstream_id=? AND occurrence_key=? "
            "AND state='claimed' AND claimer=?",
            (state, str(detail)[:512], at, *key, str(workstream_id),
             str(occurrence_key), str(claimer)),
        )
        if cur.rowcount != 1:
            raise StaleRevision(
                f"occurrence {occurrence_key!r} is not claimed by {claimer!r}"
            )
        return _occ(conn, key, workstream_id, occurrence_key)


def list_occurrences(scope: Any, workstream_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every occurrence row (any state) in a scope, oldest first."""
    return _list_children("work_occurrences", "created_at, occurrence_key",
                          scope, workstream_id)


def pending_occurrences(scope: Any, *, workstream_id: Optional[str] = None) -> List[Dict[str, Any]]:
    key = _scope_key(scope)
    clauses, params = [_SCOPE_WHERE, "state='pending'"], list(key)
    if workstream_id is not None:
        clauses.append("workstream_id=?")
        params.append(str(workstream_id))
    with _ws_read() as conn:
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT * FROM work_occurrences WHERE " + " AND ".join(clauses)
            + " ORDER BY created_at, occurrence_key",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def recover_occurrences(
    scope: Any, *, owner_alive: Callable[[str], bool],
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Claimed rows whose claimer is provably gone become ``unknown``.

    They never return to ``pending``: the effect may have happened, so only an
    owner reconciliation may decide. Pending rows are untouched (never claimed,
    so still safe to claim exactly once).
    """
    key = _scope_key(scope)
    at = _utc_iso(now)
    changed: List[Dict[str, Any]] = []
    with _ws_write() as conn:
        rows = conn.execute(
            f"SELECT * FROM work_occurrences WHERE {_SCOPE_WHERE} AND state='claimed' "
            "ORDER BY created_at, occurrence_key",
            key,
        ).fetchall()
        for row in rows:
            if owner_alive(row["claimer"]):
                continue
            cur = conn.execute(
                "UPDATE work_occurrences SET state='unknown', detail=?, updated_at=? "
                f"WHERE {_SCOPE_WHERE} AND workstream_id=? AND occurrence_key=? "
                "AND state='claimed' AND claimer=?",
                ("claimer gone after claim; effect unverified", at, *key,
                 row["workstream_id"], row["occurrence_key"], row["claimer"]),
            )
            if cur.rowcount == 1:
                changed.append(_occ(conn, key, row["workstream_id"], row["occurrence_key"]))
    return changed


# -- authority link (runtime-written; carries references, not authority) --

def _authority(conn, key, workstream_id) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        f"SELECT * FROM work_authority WHERE {_SCOPE_WHERE} AND workstream_id=?",
        (*key, str(workstream_id)),
    ).fetchone()
    if row is None:
        return None
    return {
        "workstream_id": row["workstream_id"],
        "plan_ref": json.loads(row["plan_ref"]),
        "budget": json.loads(row["budget"]),
        "linked_at": row["linked_at"],
    }


def link_authority(
    scope: Any, workstream_id: str, *, plan_ref: Mapping[str, Any],
    budget: Optional[Mapping[str, int]] = None, now: Optional[datetime] = None,
) -> Dict[str, Any]:
    key = _scope_key(scope)
    ref = dict(plan_ref)
    if not ref:
        raise ValueError("plan_ref must be non-empty")
    limits = {}
    for name, value in dict(budget or {}).items():
        if name not in _BUDGET_KEYS:
            raise ValueError(f"unknown budget key: {name!r}")
        limits[name] = max(0, int(value))
    at = _utc_iso(now)
    with _ws_write() as conn:
        _require(conn, key, workstream_id)
        found = _authority(conn, key, workstream_id)
        if found is not None:
            if found["plan_ref"] != ref:
                raise WorkstreamError("authority already linked to a different plan")
            return found
        conn.execute(
            "INSERT INTO work_authority (profile, project, audience, root, "
            "workstream_id, plan_ref, budget, linked_at) VALUES (?,?,?,?,?,?,?,?)",
            (*key, str(workstream_id), json.dumps(ref, sort_keys=True),
             json.dumps(limits, sort_keys=True), at),
        )
        return _authority(conn, key, workstream_id)


def get_authority(scope: Any, workstream_id: str) -> Optional[Dict[str, Any]]:
    key = _scope_key(scope)
    with _ws_read() as conn:
        return None if conn is None else _authority(conn, key, workstream_id)


def spend_budget(
    scope: Any, workstream_id: str, *, turns: int = 0, tokens: int = 0,
    seconds: int = 0,
) -> Dict[str, int]:
    """Atomically subtract from the remaining budget or spend nothing."""
    key = _scope_key(scope)
    spend = {"turns": int(turns), "tokens": int(tokens), "seconds": int(seconds)}
    if any(v < 0 for v in spend.values()):
        raise ValueError("budget spend must be non-negative")
    with _ws_write() as conn:
        found = _authority(conn, key, workstream_id)
        if found is None:
            raise BudgetExhausted("no linked authority budget")
        remaining = dict(found["budget"])
        for name, amount in spend.items():
            if amount == 0:
                continue
            if name not in remaining or remaining[name] < amount:
                raise BudgetExhausted(f"{name} budget cannot cover {amount}")
        for name, amount in spend.items():
            if amount:
                remaining[name] -= amount
        conn.execute(
            f"UPDATE work_authority SET budget=? WHERE {_SCOPE_WHERE} "
            "AND workstream_id=?",
            (json.dumps(remaining, sort_keys=True), *key, str(workstream_id)),
        )
        return remaining
