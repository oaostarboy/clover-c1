#!/usr/bin/env python3
"""
Async (background) delegation registry.

Backs ``delegate_task(background=true)``: the parent agent dispatches a
subagent that runs on a module-level daemon executor and returns a handle
immediately, so the user and the model can keep working while the child runs.

When the child finishes, a completion event is pushed onto the SHARED
``process_registry.completion_queue`` with ``type="async_delegation"``. The
CLI (``cli.py`` process_loop) and gateway (``_run_process_watcher`` /
``completion_queue`` drain) already poll that queue while the agent is idle
and forge a fresh user/internal turn from each event. We deliberately reuse
that rail rather than reaching into a running agent loop:

  - completions surface as a NEW turn when the agent is idle, never spliced
    between a tool result and an assistant message. That keeps strict
    message-role alternation legal and the prompt cache intact (hard
    invariant: never mutate past context).
  - we inherit the queue's de-dup, crash-recovery checkpoint, and the
    existing CLI + gateway drain wiring for free — no new drain loops in the
    two largest files in the repo.

The completion payload carries a RICH, self-contained task-source block (the
original goal, the context the parent supplied, toolsets, model, dispatch
time, status, and the full result summary). When the result re-enters the
conversation the parent may be deep in unrelated context and won't remember
why the subagent existed; the block lets it either use the result or
re-dispatch if the world has moved on.

This module owns ONLY the async lifecycle. The actual child build + run is
delegated back to ``delegate_tool._run_single_child`` via an injected
runner, so all the credential leasing, heartbeat, timeout, and result-shaping
logic stays in one place.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from clover_constants import get_clover_home
from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.thread_context import propagate_context_to_thread

logger = logging.getLogger(__name__)

# Back-compat alias — the daemon executor now lives in tools.daemon_pool so
# other subsystems (tool_executor, memory_manager, delegate_tool, skills_hub)
# can share it. Existing imports of ``_DaemonThreadPoolExecutor`` keep working.
_DaemonThreadPoolExecutor = DaemonThreadPoolExecutor


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
# A persistent daemon executor (NOT a `with ThreadPoolExecutor()` block, which
# would join on exit and defeat the whole point of async). Workers are daemon
# threads so a hard process exit doesn't hang on an in-flight child.
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_max_workers: int = 0

_records_lock = threading.Lock()
# delegation_id -> record dict. Kept for the lifetime of the run plus a short
# tail after completion so `list_async_delegations()` can show recent results.
_records: Dict[str, Dict[str, Any]] = {}

_DEFAULT_MAX_ASYNC_CHILDREN = 3
# How many completed records to retain for status queries before pruning.
_MAX_RETAINED_COMPLETED = 50
_DURABLE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DURABLE_PENDING = 1000
# A pending completion whose delivery keeps failing is retried across claim
# cycles (and across restarts via restore_undelivered_completions). Cap the
# attempts so an unroutable row converges to a terminal 'dropped' state
# instead of replaying on every restart forever.
_MAX_DELIVERY_ATTEMPTS = 8
# Staleness cap for restart replay: a pending completion older than this is
# terminally dropped instead of re-run as a fresh full-context turn (see
# restore_undelivered_completions). 48h keeps overnight/weekend results
# deliverable while stopping weeks-old sessions from replaying after upgrades.
_MAX_COMPLETION_REPLAY_AGE_S = 48 * 3600.0
_DB_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Stale-delegation detection (progress-based, on by default)
# ---------------------------------------------------------------------------
# A detached runner that wedges before returning (e.g. stuck inside its first
# model API call — #60203) never reaches its ``finally`` finalizer, so no
# completion event is ever published: the delegation shows "dispatched"
# forever and the owning session looks silent until a process restart. We do
# NOT fix this with a wall-clock timeout — legitimate heavy subagent work
# (deep reviews, research fan-outs, slow reasoning models) must never be
# killed for taking long (see delegate_tool.DEFAULT_CHILD_TIMEOUT rationale).
# Instead a single monitor thread watches per-dispatch PROGRESS (api-call
# count + current tool, via an injected ``progress_fn``): a child that is
# advancing is left alone forever; a child with NO progress past the stale
# threshold is interrupted, given a grace window to unwind and deliver its
# partial results through the normal finalize path, and only force-finalized
# with a terminal ``stalled`` event if it never returns.
#
# Thresholds mirror the sync-path heartbeat staleness monitor in
# delegate_tool: idle (not inside a tool) stays tight so a wedged first API
# call is caught quickly; in-tool is much higher so legitimately slow tools
# (long terminal commands, big fetches) get time to finish.
_STALE_CHECK_INTERVAL = 30.0  # seconds between monitor sweeps
_STALE_IDLE_SECONDS = 450.0  # no progress, no current tool → stalled
_STALE_IN_TOOL_SECONDS = 1200.0  # no progress while inside a tool → stalled
_STALL_GRACE_SECONDS = 120.0  # after interrupt, time for the runner to return

# Joined batches whose runner returned AFTER another path (stall force-finalize,
# straggler join) finalized the record, with results that still need a durable
# late receipt. Retried by the monitor tick until they land; guarded by
# ``_records_lock``.
_pending_rescues: Dict[str, List[Dict[str, Any]]] = {}

_monitor_lock = threading.Lock()
_monitor_thread: Optional[threading.Thread] = None
_monitor_stop = threading.Event()

# ---------------------------------------------------------------------------
# Fan-out delivery (delegation.fanout_delivery)
# ---------------------------------------------------------------------------
# ``each``   every child re-enters the root on its own, then the batch join
#            carries only what was not published (the pre-C1.5 behaviour).
# ``joined`` a child's terminal result is committed as a durable receipt and
#            parked (delivery_state ``joined``); ONE join event re-enters the
#            root with every result in task order. A child that never
#            published is an explicit ``incomplete`` slot, and a child that
#            finishes after the join is delivered once as a ``late_result``
#            bound to the same batch id — never a second join.
FANOUT_EACH = "each"
FANOUT_JOINED = "joined"
_JOINED_STATE = "joined"
# Request identity persisted at dispatch (task_json) and stamped on join/late
# events: the root turn, the checkpoint request, and its generation.
_REQUEST_KEYS = ("request_turn_id", "request_id", "request_generation")


# How long a ledger write waits for another writer (SessionDB shares the same
# state.db) before failing. A failure is always handled as "nothing committed,
# retry": publish falls back to the batch aggregate, finalize/stall/straggler
# are re-issued by the next monitor tick, restore on the next start.
_BUSY_TIMEOUT_SECONDS = 10.0


def _db_path():
    return get_clover_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_SECONDS)
    try:
        _initialize_schema(conn)
    except Exception:
        # A PRAGMA/DDL failure after a successful connect() must not leak the
        # just-opened connection back to the caller.
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from clover_state import apply_durability_barriers

    # state.db's owning SessionDB connection establishes the configured journal
    # mode. This secondary durability ledger must preserve that mode: applying
    # WAL here on every short-lived connection requires an exclusive lock when
    # the file is not already WAL and can collide with live transcript/FTS
    # writers. The ledger works in either WAL or DELETE mode; if it opens a new
    # file first, the default rollback journal remains valid until SessionDB
    # establishes the configured mode. sqlite3.connect(timeout=10) above also
    # gives its small transactions a busy handler for ordinary contention.
    apply_durability_barriers(conn)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            event_json TEXT,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            task_json TEXT,
            delivery_claim TEXT,
            delivery_claimed_at REAL,
            origin_session_id TEXT NOT NULL DEFAULT ''
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(async_delegations)")}
    for name, sql_type in (
        ("owner_pid", "INTEGER"),
        ("owner_started_at", "INTEGER"),
        ("task_json", "TEXT"),
        ("delivery_claim", "TEXT"),
        ("delivery_claimed_at", "REAL"),
        # Raw api_server session id (X-Clover-Session-Id) of the ORIGINATING
        # request — the wake self-post target. Without persisting it,
        # completions recovered after a process restart are unroutable on
        # api_server (the in-memory record that carried it is gone).
        ("origin_session_id", "TEXT"),
        ("continuation_json", "TEXT"),
        # Retired: the short verification window this column metered is gone
        # (a completion turn now starts its own full foreground clock). Kept
        # so older ledgers keep opening; nothing reads or writes it.
        ("verify_consumed_at", "REAL"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE async_delegations ADD COLUMN {name} {sql_type}")
    _ensure_mirror_marker(conn)


# Rollout boundary for mirroring terminal drops into the session inbox: the
# moment code that mirrors them first opened THIS profile's delegation ledger.
# Written once (never moved). Rows dropped before it predate the mirroring and
# are the reviewed Ops backfill; rows dropped at or after it must always end up
# with an inbox record, however old they get before a repair pass runs.
_MIRROR_MARKER_KEY = "delegation_inbox_mirror_since"


def _ensure_mirror_marker(conn: sqlite3.Connection) -> None:
    # Same DDL as the SessionDB schema's state_meta; harmless if it exists.
    conn.execute("CREATE TABLE IF NOT EXISTS state_meta (key TEXT PRIMARY KEY, value TEXT)")
    if conn.execute(
        "SELECT 1 FROM state_meta WHERE key = ? LIMIT 1", (_MIRROR_MARKER_KEY,)
    ).fetchone() is None:
        conn.execute(
            "INSERT OR IGNORE INTO state_meta (key, value) VALUES (?, ?)",
            (_MIRROR_MARKER_KEY, repr(time.time())),
        )


def _now() -> float:
    """Wall clock for straggler windows (patched with a fake clock in tests)."""
    return time.time()


@contextmanager
def _write_transaction() -> Iterator[sqlite3.Connection]:
    """A real SQLite write transaction, taken BEFORE any state is read.

    ``_DB_LOCK`` only serialises threads of this process. A read-then-write
    decision (is the batch still open? which children are committed?) must
    also be atomic against OTHER processes sharing ``state.db``: a plain
    ``SELECT`` does not start a transaction, so another writer could commit a
    join between the read and the write. ``BEGIN IMMEDIATE`` takes the
    database write lock first, so the read and the write see one state.
    """
    conn = _connect()
    try:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
    finally:
        conn.close()


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, and ALWAYS close it.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back the
    transaction; they do not close the connection. Using ``with _connect()``
    alone therefore leaks a connection — and its WAL/SHM file descriptors — on
    every durable dispatch, completion, and delivery-claim, deferring the close
    to the garbage collector. On a long-running gateway that exhausts
    ``RLIMIT_NOFILE`` (the cron-ledger sibling of this bug was #69567 / PR #69594).
    """
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# Routing origin persisted with the durable record: tenant/user (relay egress)
# plus the dispatching turn's route (platform / chat / thread / profile), so a
# result that is dropped later can be recorded against the route it belongs to
# without ever guessing it from the positional layout of a session key.
_ROUTING_ORIGIN_KEYS = (
    "scope_id", "user_id", "user_name",
    "origin_platform", "origin_chat_type", "origin_chat_id",
    "origin_thread_id", "origin_profile",
)


def _capture_routing_origin() -> Dict[str, Any]:
    """Snapshot the dispatching turn's routing origin for the completion event.

    Captured on the PARENT thread at dispatch time (the daemon worker doesn't
    carry the contextvars) and persisted with the durable record, so a
    completion replayed after a restart can reconstruct a full SessionSource
    even when the session-store origin and in-memory source cache are gone.
    scope_id matters most: on a relay-fronted deployment the connector's
    fail-closed egress guard needs the tenant discriminator (or a user
    binding) to route a scoped reply; without it, post-restart scoped
    completions bounce with "target not routed to an onboarded tenant"
    (staging 2026-08-09 defect #4). The ``origin_*`` route fields identify the
    conversation the result belongs to when it must be recorded rather than
    delivered. Best-effort — empty values are simply omitted so
    CLI/contextvar-unaware paths persist nothing new.
    """
    origin: Dict[str, Any] = {}
    try:
        from gateway.session_context import get_session_env

        for evt_key, env_name in (
            ("scope_id", "CLOVER_SESSION_SCOPE_ID"),
            ("user_id", "CLOVER_SESSION_USER_ID"),
            ("user_name", "CLOVER_SESSION_USER_NAME"),
            ("origin_platform", "CLOVER_SESSION_PLATFORM"),
            ("origin_chat_type", "CLOVER_SESSION_CHAT_TYPE"),
            ("origin_chat_id", "CLOVER_SESSION_CHAT_ID"),
            ("origin_thread_id", "CLOVER_SESSION_THREAD_ID"),
            ("origin_profile", "CLOVER_SESSION_PROFILE"),
        ):
            value = get_session_env(env_name, "")
            if value:
                origin[evt_key] = value
    except Exception:  # noqa: BLE001 - routing origin is additive, never fatal
        pass
    return origin


def _persist_dispatch(record: Dict[str, Any]) -> None:
    now = time.time()
    try:
        from gateway.status import get_process_start_time
        owner_started_at = get_process_start_time(__import__("os").getpid())
    except Exception:
        owner_started_at = None
    task_payload = {
        key: record.get(key)
        for key in (
            "goal", "goals", "context", "toolsets", "role", "model", "is_batch", "requested_routes",
            "owner_scope", "job_epoch",
            # Routing origin (scope_id/user_id/user_name): persisted so a
            # restart-recovered completion can reconstruct a full
            # SessionSource — see _capture_routing_origin.
            *_ROUTING_ORIGIN_KEYS,
            # Delivery mode + originating request: a join rebuilt after a
            # restart must deliver the way it was dispatched, to its origin.
            "fanout_delivery", *_REQUEST_KEYS,
        )
        if key in record
    }
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, updated_at,
                delivery_state, delivery_attempts, owner_pid,
                owner_started_at, task_json, origin_session_id)
               VALUES (?, ?, ?, ?, 'running', ?, ?, 'pending', 0, ?, ?, ?, ?)""",
            (record["delegation_id"], record.get("session_key", ""),
             record.get("origin_ui_session_id", ""), record.get("parent_session_id"),
             record["dispatched_at"], now, __import__("os").getpid(),
             owner_started_at, json.dumps(task_payload),
             record.get("origin_session_id", "")),
        )
    _prune_durable_records()


def bind_continuation_plan(delegation_id: str, plan: Dict[str, Any]) -> bool:
    """Persist a plan only after its real async dispatch row exists.

    This is called only by ``DispatchTicket.accept_handoff`` (and the router's
    stage binder) after the dispatch path reports ``dispatched``. A running row
    takes a plan; so does one that already finished cleanly but whose
    completion nobody claimed yet (a fast helper), because the runtime starts
    the next stage only when that completion is delivered. Existing rows cannot
    be re-bound or have a plan overwritten by callbacks.

    A completion the gateway already *claimed* (but did not yet deliver) takes
    the plan too: the gateway claims before the adapter injects, so a claim is
    not proof that the root integrated the result, and the continuation is only
    ever consumed by the completion turn, which runs after the dispatching
    turn that binds.  A delivered or dropped row takes nothing -- no turn would
    ever consume it.  ``continuation_json IS NULL`` keeps it once-only: a row
    that holds a plan, spent or not, is never re-bound.
    """
    if not delegation_id or not isinstance(plan, dict):
        return False
    if plan.get("delegation_id") != delegation_id:
        return False
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 32_000:
        return False
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET continuation_json=?, updated_at=?
               WHERE delegation_id=? AND continuation_json IS NULL
                 AND (state='running'
                      OR (state='completed' AND delivery_state='pending'))""",
            (encoded, time.time(), delegation_id),
        )
        return cur.rowcount == 1


def claim_continuation_stage(
    *, delegation_id: str, request_id: str, generation: int, receipt_id: str,
    stage: Dict[str, Any],
) -> bool:
    """Atomically spend one exact stage on a matching claimed terminal receipt."""
    from agent.delegation_followthrough import plan_is_live, stage_digest
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            """SELECT state, delivery_state, delivery_claim, continuation_json
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,),
        ).fetchone()
        if row is None:
            return False
        state, delivery_state, claim, raw = row
        if state != "completed" or delivery_state == "dropped" or not (claim or delivery_state == "delivered"):
            # Unknown/interrupted/error effects need inspection, not automatic
            # next-stage execution. A terminal-looking row is not success.
            return False
        try:
            plan = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            return False
        if (
            not isinstance(plan, dict) or not plan_is_live(plan)
            or plan.get("request_id") != request_id
            or plan.get("generation") != generation
            or receipt_id not in (plan.get("receipt_ids") or ())
            or plan.get("consumed") != 0
            or not isinstance(plan.get("stages"), list) or not plan["stages"]
            or not isinstance(plan.get("digests"), list) or not plan["digests"]
            or stage_digest(plan["stages"][0]) != plan["digests"][0]
            or stage_digest(stage) != plan["digests"][0]
        ):
            return False
        plan["consumed"] = 1
        encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cur = conn.execute(
            """UPDATE async_delegations SET continuation_json=?, updated_at=?
               WHERE delegation_id=? AND continuation_json=? AND state NOT IN ('running','finalizing')""",
            (encoded, time.time(), delegation_id, raw),
        )
        return cur.rowcount == 1


def revoke_continuation_plan(delegation_id: str) -> bool:
    """Cancel remaining stages after an explicit owner stop."""
    if not delegation_id:
        return False
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT continuation_json FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if not row or not row[0]:
            return False
        try:
            plan = json.loads(row[0])
        except (TypeError, ValueError):
            return False
        if not isinstance(plan, dict):
            return False
        plan["cancelled"] = True
        plan["stages"] = []
        encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "UPDATE async_delegations SET continuation_json=?, updated_at=? WHERE delegation_id=?",
            (encoded, time.time(), delegation_id),
        )
        return True


def get_continuation_plan(delegation_id: str) -> Optional[Dict[str, Any]]:
    """Read the plan bound to a durable dispatch row (diagnostic/recovery only)."""
    if not delegation_id:
        return None
    try:
        with _DB_LOCK, _transaction() as conn:
            row = conn.execute(
                "SELECT continuation_json FROM async_delegations WHERE delegation_id=?",
                (delegation_id,),
            ).fetchone()
        value = json.loads(row[0]) if row and row[0] else None
        return value if isinstance(value, dict) else None
    except Exception:
        logger.debug("continuation plan read failed closed", exc_info=True)
        return None


def _delete_durable_delegation(delegation_id: str) -> None:
    with _DB_LOCK, _transaction() as conn:
        conn.execute("DELETE FROM async_delegations WHERE delegation_id=?", (delegation_id,))


def _prune_durable_records() -> None:
    """Bound terminal history, preferring delivered records for deletion."""
    now = time.time()
    cutoff = now - _DURABLE_RETENTION_SECONDS
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            "DELETE FROM async_delegations WHERE delivery_state='delivered' AND updated_at < ?",
            (cutoff,),
        )
        # A parked child receipt is settled together with its join; one whose
        # join is long gone (aged out, pruned) must not live forever.
        conn.execute(
            "DELETE FROM async_delegations WHERE delivery_state=? AND updated_at < ?",
            (_JOINED_STATE, cutoff),
        )
        terminal_count = conn.execute(
            "SELECT COUNT(*) FROM async_delegations WHERE delivery_state='delivered'"
        ).fetchone()[0]
        excess = max(0, terminal_count - _MAX_RETAINED_COMPLETED)
        if excess:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE delivery_state='delivered'
                     ORDER BY updated_at ASC LIMIT ?
                   )""",
                (excess,),
            )
        pending_count = conn.execute(
            """SELECT COUNT(*) FROM async_delegations
               WHERE state NOT IN ('running','finalizing') AND delivery_state='pending'"""
        ).fetchone()[0]
        overflow = max(0, pending_count - _MAX_DURABLE_PENDING)
        if overflow:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing') AND delivery_state='pending'
                     ORDER BY updated_at ASC LIMIT ?
                   )""",
                (overflow,),
            )


def _persist_completion(event: Dict[str, Any], result: Dict[str, Any]) -> None:
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
               event_json=?, result_json=?, delivery_state='pending'
               WHERE delegation_id=?""",
            (event.get("status", "completed"), event.get("completed_at", now), now,
             json.dumps(event), json.dumps(result), event["delegation_id"]),
        )


def _note_delivery_attempt(delegation_id: str) -> None:
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET delivery_attempts=delivery_attempts+1, updated_at=? WHERE delegation_id=?",
            (time.time(), delegation_id),
        )


def _leave_to_restart_resume(conn, delegation_id: str, now: float) -> bool:
    """A gateway chat's subagent cut off by a restart is started again by the
    restart (``gateway.delegation_resume``), not reported as an unknown outcome.
    Its ledger row is closed silently (state ``resumed``); the inflight record
    is what the restart works from -- it resumes the job once, or reports in
    plain words why it could not.  False: report it the usual way."""
    try:
        from tools import delegation_inflight

        if delegation_inflight.read_record(delegation_id) is None:
            return False
        if not delegation_inflight.resume_enabled():
            return False
    except Exception:
        return False
    conn.execute(
        """UPDATE async_delegations SET state='resumed', completed_at=?,
           updated_at=?, delivery_state='delivered', delivered_at=?
           WHERE delegation_id=? AND state IN ('running','finalizing')""",
        (now, now, now, delegation_id),
    )
    return True


def close_resumed(delegation_id: str) -> bool:
    """The restart started this delegation again under a new id: close its
    ledger row silently (no 'unknown outcome' report for it)."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET state='resumed', completed_at=?,
               updated_at=?, delivery_state='delivered', delivered_at=?
               WHERE delegation_id=? AND state IN ('running','finalizing')""",
            (now, now, now, delegation_id),
        )
        return cur.rowcount == 1


def release_parked_children() -> int:
    """After a restart closed a batch silently: hand the parent the results of
    helpers that had finished before it (parked in the ledger), as late
    results, once.  Returns how many were queued."""
    now = time.time()
    with _DB_LOCK, _write_transaction() as conn:
        promoted = _promote_orphaned_receipts(conn, now)
    try:
        from tools.process_registry import process_registry

        for evt in promoted:
            process_registry.completion_queue.put(evt)
    except Exception:
        logger.warning("could not queue parked helper results", exc_info=True)
    return len(promoted)


def finished_child_indexes(delegation_id: str) -> set:
    """Task indexes of a batch whose own result is already recorded."""
    with _DB_LOCK, _transaction() as conn:
        ids = _committed_child_ids(conn, delegation_id)
    return {int(c.rsplit(":", 1)[-1]) for c in ids}


def report_unresumed(delegation_id: str, reason: str) -> bool:
    """A restart could not start this delegation again: tell its parent, as a
    normal ``unknown`` helper-status completion, in plain words.  Reopens the
    ledger row the restart closed silently and queues the event once."""
    now = time.time()
    with _DB_LOCK, _write_transaction() as conn:
        row = conn.execute(
            """SELECT origin_session, origin_ui_session_id, parent_session_id,
                      dispatched_at, task_json, origin_session_id, state
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,),
        ).fetchone()
        if row is None or row[6] not in ("resumed", "running", "finalizing"):
            return False
        session_key, origin_ui, parent_id, dispatched_at, task_json, origin_sid, _ = row
        task = json.loads(task_json or "{}")
        goals = task.get("goals") or []
        error = (
            "This background job was cut off when the system restarted, and it "
            f"could not be started again: {reason} Tell the user plainly that "
            "this job stopped, what it was, and that it needs to be started again."
        )
        evt = {
            "type": "async_delegation", "delegation_id": delegation_id,
            "session_key": session_key, "origin_ui_session_id": origin_ui,
            "origin_session_id": origin_sid or "", "parent_session_id": parent_id,
            "goal": task.get("goal", ""), "goals": goals, "context": task.get("context"),
            "toolsets": task.get("toolsets"), "role": task.get("role"),
            "model": task.get("model"), "is_batch": bool(task.get("is_batch")),
            "status": "unknown", "summary": None, "error": error,
            "dispatched_at": dispatched_at, "completed_at": now,
        }
        for key in _ROUTING_ORIGIN_KEYS:
            if task.get(key):
                evt[key] = task[key]
        conn.execute(
            """UPDATE async_delegations SET state='unknown', completed_at=?,
               updated_at=?, event_json=?, result_json=?, delivery_state='pending',
               delivered_at=NULL WHERE delegation_id=?""",
            (now, now, json.dumps(evt),
             json.dumps({"status": "unknown", "summary": None, "error": error}),
             delegation_id),
        )
    try:
        from tools.process_registry import process_registry

        evt["restored"] = True
        process_registry.completion_queue.put(evt)
    except Exception:
        logger.warning("could not queue the restart report for %s", delegation_id, exc_info=True)
    return True


def recover_abandoned_delegations() -> int:
    """Classify records whose owning process disappeared as outcome unknown."""
    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        return 0
    now = time.time()
    recovered = 0
    with _DB_LOCK, _write_transaction() as conn:
        rows = conn.execute(
            """SELECT delegation_id, origin_session, origin_ui_session_id,
                      parent_session_id, dispatched_at, owner_pid,
                      owner_started_at, task_json, origin_session_id
               FROM async_delegations WHERE state IN ('running','finalizing')"""
        ).fetchall()
        for row in rows:
            # One unreadable or unwritable row must not abort the recovery
            # of the others: each is its own savepoint inside the write
            # transaction and is simply retried on the next restore.
            conn.execute("SAVEPOINT recover_row")
            try:
                (delegation_id, session_key, origin_ui, parent_id, dispatched_at,
                 pid, started, task_json, origin_session_id) = row
                live = False
                if pid:
                    live = _pid_exists(int(pid))
                    if live and started is not None:
                        live = get_process_start_time(int(pid)) == int(started)
                if live:
                    continue
                task = json.loads(task_json or "{}")
                if _leave_to_restart_resume(conn, delegation_id, now):
                    recovered += 1
                    continue
                goals = task.get("goals") or []
                committed = (
                    _committed_child_ids(conn, delegation_id)
                    if task.get("is_batch") else set()
                )
                expected = {f"{delegation_id}:child:{i}" for i in range(len(goals))}
                if (
                    goals and task.get("is_batch")
                    and task.get("fanout_delivery") == FANOUT_JOINED
                ):
                    # Joined delivery: the root has heard nothing yet, so the join
                    # is rebuilt from the durable child set — saved results plus
                    # explicit incomplete slots — and must fire even when no
                    # child ever published.
                    if _recover_joined_batch(
                        conn, delegation_id, task, session_key=session_key,
                        origin_ui=origin_ui, origin_session_id=origin_session_id,
                        parent_id=parent_id, dispatched_at=dispatched_at, now=now,
                    ):
                        recovered += 1
                    continue
                if goals and expected <= committed:
                    # Every child already has its own claimable row. Do not wake
                    # the session with a generic unknown outcome for work that
                    # was recorded before the owner died.
                    conn.execute(
                        """UPDATE async_delegations SET state='completed', completed_at=?,
                           updated_at=?, delivery_state='delivered', delivered_at=?,
                           result_json=?
                           WHERE delegation_id=? AND state IN ('running','finalizing')""",
                        (now, now, now, json.dumps({
                            "status": "completed",
                            "children_already_published": len(expected),
                        }), delegation_id),
                    )
                    recovered += 1
                    continue
                published_indexes = sorted(
                    int(child_id.rsplit(":", 1)[-1]) for child_id in committed
                )
                event = {
                    "type": "async_delegation", "delegation_id": delegation_id,
                    "session_key": session_key, "origin_ui_session_id": origin_ui,
                    # Restore the durable wake target so completions recovered
                    # after a restart remain routable to api_server sessions.
                    "origin_session_id": origin_session_id or "",
                    "parent_session_id": parent_id, "goal": task.get("goal", ""),
                    "goals": task.get("goals"), "context": task.get("context"),
                    "toolsets": task.get("toolsets"), "role": task.get("role"),
                    "model": task.get("model"), "is_batch": bool(task.get("is_batch")),
                    "status": "unknown", "summary": None,
                    "error": "Delegation owner exited before recording a terminal result; outcome unknown.",
                    "dispatched_at": dispatched_at, "completed_at": now,
                }
                if published_indexes:
                    event["already_published_task_indexes"] = published_indexes
                    event["error"] += (
                        f" {len(published_indexes)} of {len(goals)} children already "
                        "recorded their own results and are not unknown."
                    )
                # Routing origin persisted at dispatch (see _capture_routing_origin):
                # restores scope_id/user_id for the reconstructed SessionSource so
                # relay egress priming works after a restart.
                for _k in _ROUTING_ORIGIN_KEYS:
                    if task.get(_k):
                        event[_k] = task[_k]
                result = {"status": "unknown", "summary": None, "error": event["error"]}
                conn.execute(
                    """UPDATE async_delegations SET state='unknown', completed_at=?,
                       updated_at=?, event_json=?, result_json=?, delivery_state='pending'
                       WHERE delegation_id=?""",
                    (now, now, json.dumps(event), json.dumps(result), delegation_id),
                )
                recovered += 1
            except Exception:
                conn.execute("ROLLBACK TO recover_row")
                logger.warning(
                    "Could not recover abandoned delegation %s; will retry "
                    "on the next restore", row[0], exc_info=True,
                )
            finally:
                conn.execute("RELEASE recover_row")
    return recovered


def restore_undelivered_completions(target_queue) -> int:
    """Enqueue durable pending completions as fresh turns after process start.

    Every restored event is stamped ``restored=True`` (in-memory only — the
    stamp is added after the durable payload is deserialized and is never
    persisted). Restored events originate from a *previous* process, so no
    consumer in THIS process implicitly owns them: drain paths that run
    without an ownership filter (the legacy single-session behavior) must
    leave them queued for a consumer that can positively prove ownership,
    otherwise a brand-new session adopts a dead session's delegation
    results seconds after boot (#64484).

    Staleness cap: a pending completion older than
    ``_MAX_COMPLETION_REPLAY_AGE_S`` is terminally dropped instead of
    replayed. Replaying a weeks-old completion re-runs its parent session as
    a full-context turn (a July session replayed in August burned a
    102K-token context on the staging fleet) for a result nobody is waiting
    on anymore; the payload stays queryable on the dropped row.
    """
    try:
        recover_abandoned_delegations()
    except Exception:
        # A busy ledger must not also hide the completions that ARE pending;
        # the dead owner's batch is recovered on the next restore.
        logger.warning(
            "Could not recover abandoned delegations; restoring pending "
            "completions anyway", exc_info=True,
        )
    now = time.time()
    restored = 0
    aged_out = []
    promoted: list = []
    with _DB_LOCK, _write_transaction() as conn:
        rows = conn.execute(
            """SELECT delegation_id, event_json, completed_at, dispatched_at
               FROM async_delegations
               WHERE state != 'running' AND delivery_state='pending' AND event_json IS NOT NULL
               ORDER BY completed_at, delegation_id"""
        ).fetchall()
        for delegation_id, payload, completed_at, dispatched_at in rows:
            age_basis = completed_at or dispatched_at
            if age_basis and (now - age_basis) > _MAX_COMPLETION_REPLAY_AGE_S:
                conn.execute(
                    """UPDATE async_delegations SET delivery_state='dropped',
                              delivery_claim=NULL, delivery_claimed_at=NULL,
                              updated_at=?
                       WHERE delegation_id=? AND delivery_state='pending'""",
                    (now, delegation_id),
                )
                _settle_joined_children(conn, delegation_id, "inboxed", now)
                logger.warning(
                    "Async delegation %s: pending completion is %.1fh old "
                    "(cap %.1fh); terminally dropping the replay (result "
                    "remains queryable).",
                    delegation_id, (now - age_basis) / 3600.0,
                    _MAX_COMPLETION_REPLAY_AGE_S / 3600.0,
                )
                aged_out.append((delegation_id, "stale_replay"))
                continue
            evt = json.loads(payload)
            if isinstance(evt, dict):
                evt["restored"] = True
            target_queue.put(evt)
            restored += 1
        promoted = _promote_orphaned_receipts(conn, now)
    # Enqueued only once the promotion is committed: a rolled-back promotion
    # must not leave an event for a row that is still parked.
    for evt in promoted:
        target_queue.put(evt)
    restored += len(promoted)
    _record_drops_in_inbox(aged_out)
    return restored


_CHILD_ID_MARK = ":child:"


def child_delegation_id(delegation_id: str, index: int) -> str:
    """The ledger id of one batch child (``<dispatch id>:child:<index>``)."""
    return f"{delegation_id}{_CHILD_ID_MARK}{index}"


def dispatch_id_for(delegation_id: str) -> str:
    """The dispatch a completion id belongs to (a batch child maps to its batch)."""
    return str(delegation_id or "").split(_CHILD_ID_MARK, 1)[0]


def mark_completion_delivered(delegation_id: str) -> bool:
    """Atomically acknowledge successful injection of a durable completion."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered', delivered_at=?, updated_at=?
               WHERE delegation_id=? AND delivery_state!='delivered'""",
            (now, now, delegation_id),
        )
        if cur.rowcount == 1:
            _settle_joined_children(conn, delegation_id, "delivered", now)
        return cur.rowcount == 1


def claim_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Claim one pending completion across competing consumers/processes."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            return True  # legacy event created before durable dispatch
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=?, delivery_claimed_at=?,
                      delivery_attempts=delivery_attempts+1, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
            (claim_id, now, now, delegation_id, now - 300),
        )
        if cur.rowcount == 1:
            # The join's claim is the claim on every receipt parked behind it.
            _cascade_to_joined_children(
                conn, delegation_id,
                "delivery_claim=?, delivery_claimed_at=?, updated_at=?",
                (claim_id, now, now),
            )
        return cur.rowcount == 1


def claim_event_delivery(evt: Dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable delegation event; non-durable events need no token."""
    if evt.get("type") != "async_delegation":
        return ""
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{__import__('os').getpid()}:{uuid.uuid4().hex}"
    return claim_id if claim_completion_delivery(delegation_id, claim_id) else None


def _record_drops_in_inbox(drops) -> list:
    """Mirror terminal ``dropped`` delegations into ``session_inbox`` (+ notice).

    Every path that sets ``delivery_state='dropped'`` calls this AFTER its own
    transaction commits (both tables live in one SQLite file, and a second
    writer inside the open transaction would wait on it). ``drops`` is
    ``[(delegation_id, reason), ...]``; returns the ids that could NOT be
    recorded. Best effort by design: the row is
    already terminal and its result stays queryable, so a failure here is a
    WARNING, never an exception into the delivery path. ``inbox_put`` is
    idempotent on the key, so a record the gateway already wrote (with the
    resolved owner) is left as it is.
    """
    failed: list = []
    if not drops:
        return failed
    try:
        from clover_state import SessionDB
        from gateway.completion_ownership import (
            canonical_profile,
            resolve_root_sync,
            route_from_session_row,
        )

        with _DB_LOCK, _transaction() as conn:
            rows = {
                did: conn.execute(
                    "SELECT event_json, result_json, task_json FROM async_delegations "
                    "WHERE delegation_id=?", (did,),
                ).fetchone()
                for did, _reason in drops
            }
        db = SessionDB()
        try:
            for did, reason in drops:
                try:
                    row = rows.get(did)
                    evt = json.loads(row[0]) if row and row[0] else {}
                    if not isinstance(evt, dict):
                        evt = {}
                    if not evt and row and row[1]:
                        evt = {"result": json.loads(row[1])}
                    task = json.loads(row[2]) if row and row[2] else {}
                    if not isinstance(task, dict):
                        task = {}

                    def origin(name, _evt=evt, _task=task):
                        return _evt.get(name) or _task.get(name) or None

                    platform, chat_id = origin("origin_platform"), origin("origin_chat_id")
                    pin = evt.get("parent_session_id") or None
                    # Store the resolved owner ROOT (what resolve_owner resolves
                    # the pin to), not a delegated child's raw id; keep the raw
                    # pin if it cannot be resolved.
                    owner_root = (resolve_root_sync(db, pin) if pin else None) or pin
                    route = None
                    session_key = str(evt.get("session_key") or "") or None
                    if platform and chat_id:
                        route = {
                            "profile": canonical_profile(origin("origin_profile")),
                            "platform": platform,
                            "chat_id": str(chat_id),
                            "thread_id": origin("origin_thread_id"),
                        }
                    else:
                        # No route on the row: recover it from the owner
                        # session's persisted origin in the sessions table.
                        for sid in (owner_root, pin):
                            route = route_from_session_row(db.get_session(sid)) if sid else None
                            if route:
                                session_key = session_key or route.get("session_key")
                                break
                        if route is None:
                            logger.warning(
                                "Dropped delegation %s has no durable route origin; "
                                "recording it against its owner only (found by /results "
                                "in the owner's conversation, no notice can be sent)",
                                did,
                            )
                    db.inbox_put({
                        "key": f"deleg:{did}",
                        "profile": route["profile"] if route else None,
                        "platform": route["platform"] if route else None,
                        "chat_id": route["chat_id"] if route else None,
                        "thread_id": route["thread_id"] if route else None,
                        "session_key": session_key,
                        "owner_root_id": owner_root,
                        "kind": "delegation",
                        "wake": 0,
                        "title": " ".join(str(evt.get("goal") or "").split())[:120],
                        "payload_json": json.dumps(evt, default=str),
                        "shown_to_user": 0,
                    })
                    db.inbox_drop(f"deleg:{did}", reason)
                except Exception:
                    failed.append(did)
                    logger.warning(
                        "Could not record dropped delegation %s in the session inbox",
                        did, exc_info=True,
                    )
        finally:
            db.close()
    except Exception:
        failed = [did for did, _reason in drops]
        logger.warning(
            "Could not record dropped delegations in the session inbox",
            exc_info=True,
        )
    return failed


# A drop is mirrored into the inbox right after its transaction commits; a
# crash in that gap, or an inbox write that failed at runtime, leaves a
# ``dropped`` row with no inbox record. Repair covers every such row dropped at
# or after the rollout marker (see ``_ensure_mirror_marker``) whatever its age.
def repair_dropped_without_inbox(since: float = 0.0) -> int:
    """Create the inbox ``dropped`` record (+ pending notice) for ``dropped``
    delegations at/after the rollout marker that have none. Idempotent.

    Run at gateway startup and on every periodic notice sweep, per profile
    scope. *since* is a caller-held high-water mark (only rows updated at or
    after it are scanned) that keeps the periodic pass cheap; ``0.0`` scans
    from the marker. Returns the number repaired.
    """
    return repair_dropped_without_inbox_scan(since)[1]


def repair_dropped_without_inbox_scan(since: float = 0.0):
    """Like :func:`repair_dropped_without_inbox` but returns ``(found, repaired, scanned_at)``
    so a caller can advance its high-water mark only after a fully clean pass."""
    scanned_at = time.time()
    try:
        from clover_state import SessionDB

        SessionDB().close()  # make sure session_inbox exists in this state.db
        with _DB_LOCK, _transaction() as conn:
            marker_row = conn.execute(
                "SELECT value FROM state_meta WHERE key = ?", (_MIRROR_MARKER_KEY,)
            ).fetchone()
            floor = max(float(marker_row[0]) if marker_row else 0.0, float(since))
            ids = [
                row[0] for row in conn.execute(
                    """SELECT d.delegation_id FROM async_delegations d
                       WHERE d.delivery_state='dropped' AND d.updated_at >= ?
                         AND NOT EXISTS (SELECT 1 FROM session_inbox i
                                         WHERE i.key = 'deleg:' || d.delegation_id
                                           AND i.state != 'pending')
                       ORDER BY d.updated_at""",
                    (floor,),
                ).fetchall()
            ]
    except Exception:
        logger.warning("Could not scan for dropped delegations without an inbox record", exc_info=True)
        return 0, 0, scanned_at
    if not ids:
        return 0, 0, scanned_at
    logger.warning(
        "Repairing %d dropped delegation(s) that had no session-inbox record", len(ids),
    )
    failed = _record_drops_in_inbox([(did, "repaired_after_drop") for did in ids])
    return len(ids), len(ids) - len(failed), scanned_at


def release_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Release a failed delivery claim so another consumer may retry.

    Attempts are counted at claim time, so a row that keeps being claimed and
    released has burned real delivery attempts. Once the budget is exhausted
    the row converges to a terminal ``dropped`` state instead of returning to
    ``pending`` — otherwise an undeliverable completion replays on every
    gateway restart forever (restore_undelivered_completions only restores
    pending rows).
    """
    now = time.time()
    was_capped = False
    with _DB_LOCK, _transaction() as conn:
        capped = conn.execute(
            """UPDATE async_delegations SET delivery_state='dropped',
                      delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=? AND delivery_attempts>=?""",
            (now, delegation_id, claim_id, _MAX_DELIVERY_ATTEMPTS),
        )
        if capped.rowcount == 1:
            logger.warning(
                "Async delegation %s exhausted its %d delivery attempts; "
                "marking terminally dropped (result remains queryable).",
                delegation_id, _MAX_DELIVERY_ATTEMPTS,
            )
            _settle_joined_children(conn, delegation_id, "inboxed", now)
            was_capped = True
        else:
            cur = conn.execute(
                """UPDATE async_delegations SET delivery_claim=NULL,
                          delivery_claimed_at=NULL, updated_at=?
                   WHERE delegation_id=? AND delivery_state='pending'
                     AND delivery_claim=?""",
                (now, delegation_id, claim_id),
            )
            released = cur.rowcount == 1
            if released:
                _cascade_to_joined_children(
                    conn, delegation_id,
                    "delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?",
                    (now,), only_claim=claim_id,
                )
    if was_capped:
        _record_drops_in_inbox([(delegation_id, "delivery_attempts_exhausted")])
        return True
    return released


def drop_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Terminally drop a claimed completion that can never be delivered.

    Used when the delivery target is permanently gone — the spawning session
    ended at an explicit user boundary (/new, reset) rather than a compression
    rotation. Marking the row ``dropped`` (not ``delivered``) keeps the ack
    honest, and (not ``pending``) keeps restart recovery from replaying a
    completion that will be fail-closed dropped again every time.
    """
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='dropped',
                      updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, delegation_id, claim_id),
        )
        dropped = cur.rowcount == 1
        if dropped:
            _settle_joined_children(conn, delegation_id, "inboxed", now)
    if dropped:
        _record_drops_in_inbox([(delegation_id, "undeliverable")])
    return dropped


def complete_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Acknowledge acceptance for the consumer holding this claim."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered',
                      delivered_at=?, updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, now, delegation_id, claim_id),
        )
        if cur.rowcount == 1:
            _settle_joined_children(conn, delegation_id, "delivered", now)
        return cur.rowcount == 1


def complete_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        complete_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def release_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        release_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def get_durable_delegation(delegation_id: str) -> Optional[Dict[str, Any]]:
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            """SELECT origin_session, state, dispatched_at, completed_at,
                      result_json, delivery_state, delivery_attempts,
                      origin_session_id
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "delegation_id": delegation_id, "origin_session": row[0], "state": row[1],
        "dispatched_at": row[2], "completed_at": row[3],
        "result": json.loads(row[4]) if row[4] else None,
        "delivery_state": row[5], "delivery_attempts": row[6],
        "origin_session_id": row[7] or "",
    }


def get_delegation_route(delegation_id: str) -> Dict[str, Any]:
    """Durable routing identity of a dispatch row (session key, origin session,
    parent session and the dispatching turn's platform/chat origin), read only
    from the persisted row -- the same fields a replayed completion event
    carries. ``{}`` when the row is absent."""
    if not delegation_id:
        return {}
    try:
        with _DB_LOCK, _transaction() as conn:
            row = conn.execute(
                """SELECT origin_session, origin_ui_session_id, parent_session_id,
                          task_json, origin_session_id
                   FROM async_delegations WHERE delegation_id=?""", (delegation_id,),
            ).fetchone()
        if row is None:
            return {}
        session_key, origin_ui, parent_id, task_json, origin_sid = row
        route: Dict[str, Any] = {
            "session_key": session_key or "", "origin_ui_session_id": origin_ui or "",
            "origin_session_id": origin_sid or "", "parent_session_id": parent_id or "",
        }
        task = json.loads(task_json or "{}")
        for key in _ROUTING_ORIGIN_KEYS:
            if isinstance(task, dict) and task.get(key):
                route[key] = task[key]
        return route
    except Exception:
        logger.debug("delegation route read failed closed", exc_info=True)
        return {}


def owned_receipts_ready(receipt_ids: Iterable[str]) -> tuple:
    """The subset of ``receipt_ids`` whose durable row is a claimed terminal result.

    Reads the durable table only, never the in-memory record: a job flips to
    ``finalizing`` in memory before its result is committed, so only a terminal
    row proves a committed result. The delivery claim (or a completed delivery)
    proves the text was handed to a consumer. Missing rows, unknown states,
    unclaimed or dropped rows, and any database error yield nothing (fail
    closed).
    """
    ids = [str(r) for r in receipt_ids if r]
    if not ids:
        return ()
    try:
        marks = ",".join("?" for _ in ids)
        with _DB_LOCK, _transaction() as conn:
            rows = conn.execute(
                f"""SELECT delegation_id FROM async_delegations
                    WHERE delegation_id IN ({marks})
                      AND state NOT IN ('running', 'finalizing')
                      AND (delivery_claim IS NOT NULL OR delivery_state='delivered')""",
                ids,
            ).fetchall()
    except Exception:
        logger.debug("owned_receipts_ready failed closed", exc_info=True)
        return ()
    return tuple(row[0] for row in rows)


def _get_executor(max_workers: int) -> ThreadPoolExecutor:
    """Lazily create (or grow) the shared daemon executor.

    We never shrink — ThreadPoolExecutor can't resize — but if the configured
    cap grows between calls we rebuild a larger pool. Existing in-flight
    futures keep running on the old pool until it's garbage collected.
    """
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is None or max_workers > _executor_max_workers:
            # Daemon threads: thread_name_prefix aids debugging in stack dumps.
            _executor = _DaemonThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="async-delegate",
            )
            _executor_max_workers = max_workers
        return _executor


def active_count() -> int:
    """Number of async delegation UNITS currently running.

    A unit is one dispatch: a single subagent OR a whole fan-out batch. A batch
    counts as ONE here because it occupies one async-pool slot (the capacity
    semantics ``dispatch_async_delegation_batch`` relies on). For the count of
    actual concurrent child subagents (batch expanded), use
    ``active_task_count()``.
    """
    with _records_lock:
        return sum(
            1 for r in _records.values()
            if r.get("status") in {"running", "stalling", "finalizing"}
        )


def active_for_session(origin_ui_session_id: str) -> int:
    """Number of live async delegations owned by one UI session."""
    if not origin_ui_session_id:
        return 0
    with _records_lock:
        return sum(
            1
            for r in _records.values()
            if r.get("status") in {"running", "stalling", "finalizing"}
            and str(r.get("origin_ui_session_id") or "")
            == origin_ui_session_id
        )


def active_task_count() -> int:
    """Number of async delegation TASKS (child subagents) currently running.

    Unlike ``active_count()`` (units/slots), this expands a batch to its child
    count: a running batch of N tasks contributes N, a single subagent
    contributes 1. This is the truthful "how many subagents are actually
    working right now" figure for observability, where a 3-task batch shown as
    "1" undercounts real concurrent work. Falls back to counting a batch as 1
    if its goal list is missing.
    """
    with _records_lock:
        total = 0
        for r in _records.values():
            if r.get("status") not in {"running", "finalizing"}:
                continue
            if r.get("is_batch"):
                goals = r.get("goals")
                total += len(goals) if isinstance(goals, (list, tuple)) and goals else 1
            else:
                total += 1
        return total


def _matches_session_selectors(
    record: Dict[str, Any],
    *,
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
) -> bool:
    return (
        (origin_ui_session_id and str(record.get("origin_ui_session_id") or "") == origin_ui_session_id)
        or (session_key and str(record.get("session_key") or "") == session_key)
        or (parent_session_id and str(record.get("parent_session_id") or "") == parent_session_id)
    )


def has_live_for_session(
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
) -> bool:
    """Whether a session still owns any live async delegation.

    Live = running / stalling / finalizing — the same states the reapers'
    keepalive treats as active work.
    """
    if not session_key and not origin_ui_session_id and not parent_session_id:
        return False
    with _records_lock:
        return any(
            r.get("status") in {"running", "stalling", "finalizing"}
            and _matches_session_selectors(
                r,
                session_key=session_key,
                origin_ui_session_id=origin_ui_session_id,
                parent_session_id=parent_session_id,
            )
            for r in _records.values()
        )


def _new_delegation_id() -> str:
    return f"deleg_{uuid.uuid4().hex[:8]}"


def _prune_completed_locked() -> None:
    """Drop the oldest completed records beyond the retention cap.

    Caller must hold ``_records_lock``.
    """
    completed = [
        (rid, r)
        for rid, r in _records.items()
        if r.get("status") != "running"
    ]
    if len(completed) <= _MAX_RETAINED_COMPLETED:
        return
    # Oldest-first by completion time (fall back to dispatch time).
    completed.sort(key=lambda kv: kv[1].get("completed_at") or kv[1].get("dispatched_at") or 0)
    for rid, _ in completed[: len(completed) - _MAX_RETAINED_COMPLETED]:
        _records.pop(rid, None)


def _current_origin_session_id() -> str:
    """Raw session id of the ORIGINATING api_server request, or ``""``.

    The obvious source — ``CLOVER_SESSION_ID`` via ``get_session_env`` — is
    NOT safe to read at dispatch time: constructing a child agent
    (``agent/agent_init.py``) calls ``set_current_session_id(child.session_id)``,
    clobbering that ContextVar *and* ``os.environ`` with the subagent's
    internal ``{timestamp}_{uuid}`` id moments before the dispatch code reads
    it, so the completion wake would self-post into the subagent's own
    (unread) session instead of the spawner's.

    The request-scoped ``CLOVER_SESSION_CHAT_ID`` binding survives child
    construction: ``_bind_api_server_session`` binds ``chat_id`` to the raw
    ``X-Clover-Session-Id``, and its only writer is ``set_session_vars`` —
    ``set_current_session_id`` never touches it. Gate on the platform: on
    push platforms ``chat_id`` is a chat, not a session, so yield ``""``
    there.
    """
    try:
        from gateway.session_context import get_session_env

        if get_session_env("CLOVER_SESSION_PLATFORM", "") != "api_server":
            return ""
        return get_session_env("CLOVER_SESSION_CHAT_ID", "") or ""
    except Exception:
        return ""


def dispatch_async_delegation(
    *,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    origin_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    progress_fn: Optional[Callable[[], tuple]] = None,
) -> Dict[str, Any]:
    """Spawn ``runner`` on the daemon executor and return a handle immediately.

    Parameters
    ----------
    goal, context, toolsets, role, model
        The dispatch-time task spec, captured verbatim for the rich
        completion block.
    session_key
        The gateway session_key (from ``tools.approval.get_current_session_key``)
        captured on the parent thread BEFORE dispatch, because the daemon
        worker thread won't carry the contextvar. Used to route the
        completion back to the originating session.
    parent_session_id
        The durable ``state.db`` session id of the parent agent that spawned
        the delegation. Carried on the completion event so the gateway can
        pin routing to the spawning session instead of recovering the latest
        ``ended_at IS NULL`` row for the peer tuple (#57498).
    runner
        Zero-arg callable that builds + runs the child and returns the same
        result dict ``_run_single_child`` produces. Runs on the worker thread.
    interrupt_fn
        Optional callable to signal the child to stop (used on shutdown /
        explicit cancel).
    progress_fn
        Optional zero-arg callable returning ``(token, in_tool)`` where
        ``token`` is any comparable snapshot of the child's progress (api
        call count + current tool) and ``in_tool`` says whether the child is
        currently inside a tool call. Sampled by the stale monitor; a frozen
        token past the stale threshold marks the delegation stuck (see the
        stale-detection block at the top of this module). When omitted, the
        delegation is not monitored.
    max_async_children
        Concurrency cap. When at capacity the dispatch is REJECTED (the caller
        should fall back to sync or tell the user) rather than queued, so a
        runaway model can't pile up unbounded background work.

    Returns
    -------
    dict
        ``{"status": "dispatched", "delegation_id": ...}`` on success, or
        ``{"status": "rejected", "error": ...}`` when at capacity.
    """
    delegation_id = _new_delegation_id()
    dispatched_at = time.time()
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": goal,
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "origin_session_id": origin_session_id,
        "parent_session_id": parent_session_id,
        **_capture_routing_origin(),
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "progress_fn": progress_fn,
        # Stale-monitor bookkeeping (see _stale_monitor_loop).
        "_progress_token": None,
        "_progress_ts": dispatched_at,
        "_interrupted_at": None,
    }
    # Capacity check and record insert under ONE lock hold — checking
    # active_count() separately would let two concurrent dispatches (e.g.
    # from different gateway sessions) both pass the check and exceed the cap.
    with _records_lock:
        running = sum(
            1 for r in _records.values()
            if r.get("status") in ("running", "stalling")
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or run this task synchronously "
                    f"(background=false). Raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background subagents."
                ),
            }
        _records[delegation_id] = record

    _persist_dispatch(record)
    executor = _get_executor(max_async_children)

    def _worker() -> None:
        result: Dict[str, Any] = {}
        status = "error"
        try:
            result = runner() or {}
            status = result.get("status") or "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation %s crashed", delegation_id)
            result = {
                "status": "error",
                "summary": None,
                "error": f"{type(exc).__name__}: {exc}",
                "api_calls": 0,
                "duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize(delegation_id, result, status)

    try:
        # Propagate the dispatching profile so the detached child resolves
        # get_clover_home() under the right profile.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover — pool submit failure is rare
        with _records_lock:
            _records.pop(delegation_id, None)
        _delete_durable_delegation(delegation_id)
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation: {exc}",
        }
    if progress_fn is not None:
        _ensure_stale_monitor_best_effort(delegation_id)

    logger.info(
        "Dispatched async delegation %s (session_key=%s): %s",
        delegation_id, session_key or "<cli>", (goal or "")[:80],
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize(delegation_id: str, result: Dict[str, Any], status: str) -> None:
    """Mark a record complete and push the completion event onto the queue."""
    if status not in ("completed", "success") and _held_for_resume(delegation_id):
        return  # cut off by a gateway restart: the restart starts it again
    claimed = _begin_finalization(delegation_id)
    if claimed is None:
        return
    event_record, _interrupt_fn = claimed

    try:
        _push_completion_event(event_record, result, status)
    except Exception:
        logger.warning(
            "Async delegation %s: could not record its completion; keeping "
            "it and retrying", delegation_id, exc_info=True,
        )
        _abort_finalization(delegation_id, (False, result, status))
        return
    _finish_finalization(delegation_id, status)


def _begin_finalization(
    delegation_id: str,
) -> Optional[tuple[Dict[str, Any], Optional[Callable[[], None]]]]:
    """Atomically claim terminal delivery while keeping the record active."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("status") not in ("running", "stalling"):
            return
        # Stay active until durable persistence and queue publication finish;
        # otherwise process shutdown can kill this daemon worker in the narrow
        # gap after status flips but before SQLite is committed.
        # What an aborted attempt must put back (see _abort_finalization).
        record["_pre_finalize"] = {
            "status": record.get("status"),
            "interrupt_fn": record.get("interrupt_fn"),
            "progress_fn": record.get("progress_fn"),
        }
        record["status"] = "finalizing"
        record["completed_at"] = time.time()
        interrupt_fn = record.get("interrupt_fn")
        record["interrupt_fn"] = None  # drop the closure; child is done
        record["progress_fn"] = None  # stop stale-monitor sampling
        event_record = dict(record)

    return event_record, interrupt_fn


def _abort_finalization(
    delegation_id: str, pending: Optional[tuple] = None
) -> None:
    """A finalization could not be committed: leave the record as it was.

    The in-memory record only changes once the durable write is in, so a
    database that is busy for longer than its timeout costs a retry, not the
    result. ``pending`` is ``(is_batch, result, status)`` of a runner that has
    already returned: the monitor re-issues it on its next tick (a stall
    force-finalize needs no pending entry, the record is simply ``stalling``
    again and the next sweep repeats it).
    """
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("status") != "finalizing":
            return
        prior = record.pop("_pre_finalize", None) or {}
        record["status"] = prior.get("status") or "running"
        record["completed_at"] = None
        record["interrupt_fn"] = prior.get("interrupt_fn")
        if pending is None:
            # A stall force-finalize that did not commit: the runner is still
            # out there, so the stall detector keeps watching it.
            record["progress_fn"] = prior.get("progress_fn")
        else:
            # The runner has already returned: there is nothing left to
            # stall-detect, and a stall force-finalize of it would strand the
            # pending completion on a record the retry path skips. Only the
            # retry path owns this record now.
            record["progress_fn"] = None
            record["_pending_finalize"] = pending
    if pending is not None:
        _ensure_stale_monitor_best_effort(delegation_id)


def _retry_pending_finalizations() -> None:
    """Re-issue finalizations whose durable commit failed (monitor tick)."""
    with _records_lock:
        due = [
            (rid, rec.pop("_pending_finalize"))
            for rid, rec in _records.items()
            if rec.get("_pending_finalize") is not None
            and rec.get("status") in ("running", "stalling")
        ]
    for delegation_id, (is_batch, result, status) in due:
        try:
            if is_batch:
                _finalize_batch(delegation_id, result, status)
            else:
                _finalize(delegation_id, result, status)
        except Exception:
            logger.warning(
                "Async delegation %s: retrying its completion failed; "
                "will try again on the next tick", delegation_id, exc_info=True,
            )


def _finish_finalization(delegation_id: str, status: str) -> None:
    with _records_lock:
        record = _records.get(delegation_id)
        if record is not None:
            record["status"] = status
        _prune_completed_locked()
    # Done (completed, failed, stopped, stalled): nothing left to resume.
    try:
        from tools import delegation_inflight

        delegation_inflight.remove_record(delegation_id)
    except Exception:
        logger.debug("could not drop inflight record %s", delegation_id, exc_info=True)


def _held_for_resume(delegation_id: str) -> bool:
    """The gateway is shutting down and a restart will resume this delegation:
    its interrupted completion is neither reported nor recorded."""
    try:
        from tools import delegation_inflight

        return delegation_inflight.hold_for_resume(delegation_id)
    except Exception:
        return False


def _push_completion_event(
    record: Dict[str, Any], result: Dict[str, Any], status: str
) -> None:
    """Push a type='async_delegation' event onto the shared completion queue.

    Best-effort: a failure here must not crash the worker, but it WOULD mean a
    silently-lost result, so we log loudly.
    """
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s finished but process_registry import failed; "
            "result lost: %s",
            record.get("delegation_id"), exc,
        )
        return

    summary = result.get("summary")
    error = result.get("error")
    dispatched_at = record.get("dispatched_at") or time.time()
    completed_at = record.get("completed_at") or time.time()

    evt = {
        "type": "async_delegation",
        "delegation_id": record.get("delegation_id"),
        # session_key routes the completion back to the originating gateway
        # session; empty string => CLI (single-session) path.
        "session_key": record.get("session_key", ""),
        "origin_ui_session_id": record.get("origin_ui_session_id", ""),
        "origin_session_id": record.get("origin_session_id", ""),
        "parent_session_id": record.get("parent_session_id"),
        "goal": record.get("goal", ""),
        "context": record.get("context"),
        "toolsets": record.get("toolsets"),
        "role": record.get("role"),
        "model": result.get("model") or record.get("model"),
        "status": status,
        "summary": summary,
        "error": error,
        "api_calls": result.get("api_calls", 0),
        "duration_seconds": result.get(
            "duration_seconds", round(completed_at - dispatched_at, 2)
        ),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
        "exit_reason": result.get("exit_reason"),
    }
    # Routing origin captured at dispatch (see _capture_routing_origin):
    # additive, lets the gateway reconstruct a full SessionSource (incl.
    # scope_id for relay tenant egress) when its own caches are cold.
    for _k in _ROUTING_ORIGIN_KEYS:
        if record.get(_k):
            evt[_k] = record[_k]
    # Structured stall metadata (#51690) — additive, present only on
    # stall-monitor finalizations.
    for _k in (
        "stalled_after_quiet_seconds",
        "stall_threshold_seconds",
        "stall_phase",
        "stall_grace_seconds",
    ):
        if _k in result:
            evt[_k] = result[_k]
    _persist_completion(evt, result)
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s: failed to enqueue completion event; "
            "result lost: %s",
            record.get("delegation_id"), exc,
        )


# ---------------------------------------------------------------------------
# Joined fan-out: parked child receipts, the single join, the late child
# ---------------------------------------------------------------------------

def _included_receipts(conn, batch_id: str) -> List[str]:
    """Receipt ids the committed join of ``batch_id`` actually carries.

    Read from the join's own payload, never inferred from "every parked row":
    only a result that is IN the join may be settled by the join's
    acknowledgement. Anything else stays undelivered until its own late
    delivery is acknowledged.
    """
    row = conn.execute(
        "SELECT event_json FROM async_delegations WHERE delegation_id=?",
        (batch_id,),
    ).fetchone()
    try:
        evt = json.loads(row[0]) if row and row[0] else {}
    except (TypeError, ValueError):
        evt = {}
    if not isinstance(evt, dict):
        return []
    ids = evt.get("included_receipts")
    if isinstance(ids, list):
        return [str(i) for i in ids]
    return [
        f"{batch_id}:child:{r['task_index']}"
        for r in evt.get("results") or ()
        if isinstance(r, dict) and isinstance(r.get("task_index"), int)
        and not r.get("incomplete")
    ]


def _cascade_to_joined_children(
    conn, batch_id: str, assignments: str, params: tuple, *,
    only_claim: Optional[str] = None,
) -> None:
    """Apply ``assignments`` to the parked receipts the join carries.

    Only rows still in the parked ``joined`` state AND listed in the join's
    payload are touched, so an ``each``-mode batch (independent rows) and a
    result that missed the join are both unaffected. ``only_claim`` restricts
    the change to receipts carrying that claim.
    """
    ids = _included_receipts(conn, batch_id)
    for offset in range(0, len(ids), 400):
        chunk = ids[offset:offset + 400]
        marks = ",".join("?" for _ in chunk)
        sql = (
            f"UPDATE async_delegations SET {assignments} "
            f"WHERE delegation_id IN ({marks}) AND delivery_state=?"
        )
        values = [*params, *chunk, _JOINED_STATE]
        if only_claim is not None:
            sql += " AND delivery_claim=?"
            values.append(only_claim)
        conn.execute(sql, values)


def _promote_orphaned_receipts(conn, now: float) -> List[Dict[str, Any]]:
    """Deliver, as late results, parked receipts their join did not carry.

    A receipt parked ``joined`` whose batch has already joined without it can
    only be a result that missed the join; leaving it parked would lose it.
    It becomes an ordinary pending late result and is enqueued once.
    """
    promoted: List[Dict[str, Any]] = []
    rows = conn.execute(
        "SELECT delegation_id, event_json FROM async_delegations "
        "WHERE delivery_state=?", (_JOINED_STATE,),
    ).fetchall()
    for child_id, payload in rows:
        batch_id, sep, suffix = str(child_id).rpartition(":child:")
        if not sep or not suffix.isdigit() or not batch_id:
            continue
        parent = conn.execute(
            "SELECT state FROM async_delegations WHERE delegation_id=?", (batch_id,)
        ).fetchone()
        if parent is None or parent[0] in ("running", "finalizing"):
            continue
        if child_id in _included_receipts(conn, batch_id):
            continue
        try:
            evt = json.loads(payload) if payload else None
        except (TypeError, ValueError):
            evt = None
        if not isinstance(evt, dict):
            continue
        evt["late_result"] = True
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='pending', event_json=?,
                      updated_at=? WHERE delegation_id=? AND delivery_state=?""",
            (json.dumps(evt), now, child_id, _JOINED_STATE),
        )
        if cur.rowcount == 1:
            evt["restored"] = True
            promoted.append(evt)
    return promoted


def _settle_joined_children(conn, batch_id: str, state: str, now: float) -> None:
    """The join reached a terminal disposition: settle its parked receipts."""
    if state == "delivered":
        _cascade_to_joined_children(
            conn, batch_id,
            "delivery_state='delivered', delivered_at=?, updated_at=?, "
            "delivery_claim=NULL, delivery_claimed_at=NULL",
            (now, now),
        )
    else:
        _cascade_to_joined_children(
            conn, batch_id,
            "delivery_state=?, updated_at=?, delivery_claim=NULL, "
            "delivery_claimed_at=NULL",
            (state, now),
        )


def _joined_child_results(conn, batch_id: str) -> Dict[int, Dict[str, Any]]:
    """``task_index -> result`` for every committed child receipt of a batch."""
    prefix = f"{batch_id}:child:"
    rows = conn.execute(
        "SELECT delegation_id, result_json, event_json FROM async_delegations "
        "WHERE instr(delegation_id, ?) = 1",
        (prefix,),
    ).fetchall()
    out: Dict[int, Dict[str, Any]] = {}
    for child_id, result_json, event_json in rows:
        suffix = child_id[len(prefix):] if isinstance(child_id, str) else ""
        if not suffix.isdigit():
            continue
        entry = None
        for raw in (result_json, event_json):
            try:
                value = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                value = None
            if isinstance(value, dict):
                entry = dict(value)
                break
        if entry is None:
            continue
        index = int(suffix)
        entry["task_index"] = index
        entry.setdefault("status", "error")
        out[index] = entry
    return out


def _compose_joined_results(
    goals: List[str],
    aggregate: Optional[List[Dict[str, Any]]],
    durable: Dict[int, Dict[str, Any]],
) -> tuple:
    """Every task slot in task order, plus the indexes with no result.

    The runner's aggregate is preferred (it is the richest copy); the durable
    child receipts fill whatever it lacks (a child whose publish failed shows
    up in the aggregate, a stalled or dead owner leaves only receipts).
    A slot with neither is an explicit ``incomplete`` entry, never omitted.
    """
    count = len(goals)
    by_index: Dict[int, Dict[str, Any]] = {}
    for entry in aggregate or ():
        index = entry.get("task_index") if isinstance(entry, dict) else None
        if isinstance(index, int) and 0 <= index < count:
            by_index.setdefault(index, entry)
    for index, entry in durable.items():
        if 0 <= index < count:
            by_index.setdefault(index, entry)
    results: List[Dict[str, Any]] = []
    incomplete: List[int] = []
    for index in range(count):
        entry = by_index.get(index)
        if entry is None:
            incomplete.append(index)
            entry = {
                "task_index": index, "status": "incomplete", "incomplete": True,
                "summary": None, "error": None, "goal": goals[index],
            }
        results.append(entry)
    return results, incomplete


def _publish_unrecorded_results(
    batch_id: str, results: Optional[List[Dict[str, Any]]]
) -> None:
    """Deliver aggregate results the committed join does not carry.

    Goes through the one child-publish path, so the outcome for each entry is
    decided atomically against the committed join: a result the join lacks
    becomes a late result (durable row + one event), a result it carries or
    that already has a receipt is a no-op. A failure raises: the finalize that
    called this is rolled back and re-issued by the monitor tick, and the
    already-landed entries are duplicates on the retry (never twice).
    """
    for entry in results or ():
        if not isinstance(entry, dict) or not isinstance(entry.get("task_index"), int):
            continue
        publish_batch_child_completion(entry, delegation_id=batch_id)


def _rescue_after_finalized(
    batch_id: str, results: Optional[List[Dict[str, Any]]]
) -> None:
    """The runner returned after the record was finalized by another path.

    ``_begin_finalization`` is then a no-op, which used to drop whatever the
    aggregate still held. For a joined batch those results (a straggler whose
    publish failed on a busy ledger) are published as late results; if that
    fails too they are kept and re-issued by every monitor tick, never
    dropped and never duplicated (receipts are idempotent).
    """
    if not results:
        return
    with _records_lock:
        record = _records.get(batch_id)
        if record is not None and (
            record.get("fanout_delivery") != FANOUT_JOINED
            or record.get("status") in ("running", "stalling", "finalizing")
        ):
            return
    if record is None:
        # The finalized record was pruned (or the process restarted its
        # in-memory registry) before the runner returned: the durable ledger
        # row carries the same routing and mode.
        try:
            parent = _batch_parent_snapshot(batch_id)
            durable = get_durable_delegation(batch_id)
        except Exception:
            logger.debug("rescue: no durable batch %s", batch_id, exc_info=True)
            return
        if (
            parent.get("fanout_delivery") != FANOUT_JOINED
            or durable is None
            or durable.get("state") in ("running", "finalizing")
        ):
            return
    try:
        _publish_unrecorded_results(batch_id, results)
    except Exception:
        logger.warning(
            "Async delegation %s: late result(s) could not be recorded yet; "
            "keeping them and retrying on the next monitor tick",
            batch_id, exc_info=True,
        )
        with _records_lock:
            _pending_rescues[batch_id] = list(results)
        _ensure_stale_monitor_best_effort(batch_id)


def _retry_pending_rescues() -> None:
    """Re-issue late-result publishes that failed (monitor tick)."""
    with _records_lock:
        due = list(_pending_rescues.items())
    for batch_id, results in due:
        try:
            _publish_unrecorded_results(batch_id, results)
        except Exception:
            logger.warning(
                "Async delegation %s: late result(s) still not recordable; "
                "will retry", batch_id, exc_info=True,
            )
            continue
        with _records_lock:
            if _pending_rescues.get(batch_id) is not None:
                del _pending_rescues[batch_id]


def _build_joined_event(
    record: Dict[str, Any], combined: Dict[str, Any], status: str,
    results: List[Dict[str, Any]], incomplete: List[int], completed_at: float,
) -> Dict[str, Any]:
    evt = {
        "type": "async_delegation",
        "delegation_id": record.get("delegation_id"),
        "session_key": record.get("session_key", ""),
        "origin_ui_session_id": record.get("origin_ui_session_id", ""),
        "origin_session_id": record.get("origin_session_id", ""),
        "parent_session_id": record.get("parent_session_id"),
        "goal": record.get("goal", ""),
        "goals": record.get("goals"),
        "context": record.get("context"),
        "toolsets": record.get("toolsets"),
        "role": record.get("role"),
        "model": record.get("model"),
        "status": status,
        "is_batch": True,
        "results": results,
        "live_transcripts": combined.get("live_transcripts"),
        "error": combined.get("error"),
        "total_duration_seconds": combined.get("total_duration_seconds"),
        "dispatched_at": record.get("dispatched_at") or time.time(),
        "completed_at": completed_at,
        "fanout_delivery": FANOUT_JOINED,
        "incomplete_indexes": list(incomplete),
        # Exactly the child receipts whose results are in this payload. The
        # acknowledgement of the join settles these and nothing else.
        "included_receipts": [
            f"{record.get('delegation_id')}:child:{r['task_index']}"
            for r in results if not r.get("incomplete")
        ],
    }
    for key in (*_ROUTING_ORIGIN_KEYS, *_REQUEST_KEYS):
        if record.get(key) not in (None, ""):
            evt[key] = record[key]
    for key in (
        "stalled_after_quiet_seconds", "stall_threshold_seconds",
        "stall_phase", "stall_grace_seconds",
    ):
        if key in combined:
            evt[key] = combined[key]
    return evt


def _push_joined_batch_completion(
    event_record: Dict[str, Any], combined: Dict[str, Any],
    status: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Create and publish the ONE join of a joined fan-out.

    The batch row and the committed children are read under a SQLite write
    transaction (``BEGIN IMMEDIATE``), so the join and any concurrent child
    publish — from this or another process — are strictly ordered: a child
    either made it into this join or sees the committed join and becomes a
    late result; it cannot fall between. Returns the join event, or ``None``
    when the batch had already joined.

    ``status`` ``None`` means "derive": ``unknown`` while any slot is
    incomplete, else ``completed``. A stall finalize whose every child had
    finished is reported ``completed`` too, with no stall error: nothing
    stalled in effect.
    """
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s finished but process_registry import "
            "failed; result lost: %s", event_record.get("delegation_id"), exc,
        )
        return None
    batch_id = str(event_record.get("delegation_id") or "")
    goals = list(event_record.get("goals") or [])
    completed_at = event_record.get("completed_at") or time.time()
    now = time.time()
    with _DB_LOCK, _write_transaction() as conn:
        row = conn.execute(
            "SELECT state FROM async_delegations WHERE delegation_id=?", (batch_id,)
        ).fetchone()
        if row is not None and row[0] not in ("running", "finalizing"):
            return None  # already joined (another process or the early path)
        durable = _joined_child_results(conn, batch_id)
        results, incomplete = _compose_joined_results(
            goals, combined.get("results"), durable
        )
        if not incomplete and status in (None, "stalled"):
            # Every helper had already finished: nothing stalled in effect.
            status = "completed"
            combined = {
                k: v for k, v in combined.items()
                if k not in (
                    "error", "stalled_after_quiet_seconds",
                    "stall_threshold_seconds", "stall_phase",
                    "stall_grace_seconds",
                )
            }
        elif status is None:
            status = "unknown" if incomplete else "completed"
        evt = _build_joined_event(
            event_record, combined, status, results, incomplete, completed_at
        )
        if row is not None:
            cur = conn.execute(
                """UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
                   event_json=?, result_json=?, delivery_state='pending'
                   WHERE delegation_id=? AND state IN ('running','finalizing')""",
                (status, completed_at, now, json.dumps(evt), json.dumps(combined),
                 batch_id),
            )
            if cur.rowcount != 1:
                return None
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s: failed to enqueue join event; "
            "result lost: %s", batch_id, exc,
        )
    return evt


def _recover_joined_batch(
    conn, batch_id: str, task: Dict[str, Any], *, session_key: str,
    origin_ui: str, origin_session_id: str, parent_id: Optional[str],
    dispatched_at: float, now: float,
) -> bool:
    """Rebuild the join of a joined batch whose owner died before it fired."""
    goals = list(task.get("goals") or [])
    record = {
        **task,
        "delegation_id": batch_id, "session_key": session_key,
        "origin_ui_session_id": origin_ui,
        "origin_session_id": origin_session_id or "",
        "parent_session_id": parent_id, "dispatched_at": dispatched_at,
    }
    durable = _joined_child_results(conn, batch_id)
    results, incomplete = _compose_joined_results(goals, None, durable)
    status = "unknown" if incomplete else "completed"
    combined = {
        "results": results, "error": None,
        "total_duration_seconds": round(now - (dispatched_at or now), 2),
    }
    evt = _build_joined_event(record, combined, status, results, incomplete, now)
    cur = conn.execute(
        """UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
           event_json=?, result_json=?, delivery_state='pending'
           WHERE delegation_id=? AND state IN ('running','finalizing')""",
        (status, now, now, json.dumps(evt),
         json.dumps({"status": status, "incomplete_indexes": incomplete}),
         batch_id),
    )
    return cur.rowcount == 1


def _batch_parent_snapshot(delegation_id: str) -> Dict[str, Any]:
    """The batch's dispatch-time record: in memory, else from the durable row.

    The in-memory record is pruned (and gone after a restart), but a straggling
    child must still bind to its own batch.
    """
    with _records_lock:
        record = _records.get(delegation_id)
        if record is not None:
            return dict(record)
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            """SELECT origin_session, origin_ui_session_id, parent_session_id,
                      dispatched_at, task_json, origin_session_id
               FROM async_delegations WHERE delegation_id=?""",
            (delegation_id,),
        ).fetchone()
    if row is None:
        raise KeyError(delegation_id)
    try:
        task = json.loads(row[4] or "{}")
    except (TypeError, ValueError):
        task = {}
    if not isinstance(task, dict) or not task.get("goals"):
        raise KeyError(delegation_id)
    return {
        **task,
        "delegation_id": delegation_id, "session_key": row[0] or "",
        "origin_ui_session_id": row[1] or "", "parent_session_id": row[2],
        "dispatched_at": row[3], "origin_session_id": row[5] or "",
    }


def _join_covers_index(event_json: Optional[str], index: int) -> bool:
    """Whether a published join already carries a real result for ``index``."""
    try:
        join = json.loads(event_json) if event_json else {}
    except (TypeError, ValueError):
        return False
    for entry in (join.get("results") or ()) if isinstance(join, dict) else ():
        if isinstance(entry, dict) and entry.get("task_index") == index:
            return not entry.get("incomplete")
    return False


def _committed_child_ids(conn, delegation_id: str) -> set:
    """Return exact ``{id}:child:{index}`` rows.

    Do not use ``LIKE``. Delegation ids contain ``_``, which ``LIKE`` treats
    as a single-character wildcard.
    """
    prefix = f"{delegation_id}:child:"
    rows = conn.execute(
        "SELECT delegation_id FROM async_delegations "
        "WHERE instr(delegation_id, ?) = 1",
        (prefix,),
    ).fetchall()
    found = set()
    for (child_id,) in rows:
        if not isinstance(child_id, str) or not child_id.startswith(prefix):
            continue
        if child_id[len(prefix):].isdigit():
            found.add(child_id)
    return found


def publish_batch_child_completion(
    result: Dict[str, Any], *, delegation_id: str
) -> str:
    """Commit an independent child terminal outcome before publishing its wake.

    Each child has its own claimable ledger row. A dead owner cannot replace a
    completed sibling with the batch's generic unknown-outcome error.

    In ``joined`` fan-out mode the row is committed as a parked receipt and
    nothing is enqueued: the batch join carries it. A child that finishes
    after the join already went out is delivered once, as a ``late_result``
    bound to the same batch id. Returns ``"published"`` (each mode),
    ``"parked"``, ``"late"`` or ``"duplicate"``.
    """
    index = result["task_index"]
    if (
        (result.get("status") or "error") not in ("completed", "success")
        and _held_for_resume(delegation_id)
    ):
        return "held"  # cut off by a gateway restart: it runs again afterwards
    parent = _batch_parent_snapshot(delegation_id)
    goals = parent["goals"]
    if not isinstance(index, int) or not 0 <= index < len(goals):
        raise ValueError("Invalid batch child index")
    child_id = child_delegation_id(delegation_id, index)
    now = time.time()
    status = result.get("status") or "error"
    evt = {
        "type": "async_delegation", "delegation_id": child_id,
        "batch_delegation_id": delegation_id, "task_index": index,
        "session_key": parent.get("session_key", ""),
        "origin_ui_session_id": parent.get("origin_ui_session_id", ""),
        "origin_session_id": parent.get("origin_session_id", ""),
        "parent_session_id": parent.get("parent_session_id"),
        "goal": goals[index], "context": parent.get("context"),
        "toolsets": parent.get("toolsets"), "role": parent.get("role"),
        "model": result.get("model") or parent.get("model"),
        "status": status, "summary": result.get("summary"),
        "error": result.get("error"), "api_calls": result.get("api_calls", 0),
        "duration_seconds": result.get("duration_seconds"),
        "exit_reason": result.get("exit_reason"),
        "dispatched_at": parent["dispatched_at"], "completed_at": now,
    }
    for key in _ROUTING_ORIGIN_KEYS:
        if parent.get(key):
            evt[key] = parent[key]
    if parent.get("fanout_delivery") != FANOUT_JOINED:
        with _DB_LOCK, _transaction() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO async_delegations
                   (delegation_id, origin_session, origin_ui_session_id,
                    parent_session_id, state, dispatched_at, completed_at, updated_at,
                    event_json, result_json, delivery_state, origin_session_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (child_id, parent.get("session_key", ""),
                 parent.get("origin_ui_session_id", ""), parent.get("parent_session_id"),
                 status, parent["dispatched_at"], now, now, json.dumps(evt),
                 json.dumps(result), parent.get("origin_session_id", "")),
            )
        if cursor.rowcount:
            from tools.process_registry import process_registry
            process_registry.completion_queue.put(evt)
            return "published"
        return "duplicate"

    # Joined fan-out. Look at the batch row and commit the receipt under one
    # lock hold: the join is created under the same lock, so this child is
    # either read by the join or sees that the join already went out.
    evt["fanout_delivery"] = FANOUT_JOINED
    evt["task_count"] = len(goals)
    for key in _REQUEST_KEYS:
        if parent.get(key) not in (None, ""):
            evt[key] = parent[key]
    delivery_state = _JOINED_STATE
    late = False
    unfinished: List[int] = []
    with _DB_LOCK, _write_transaction() as conn:
        row = conn.execute(
            "SELECT state, event_json FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if row is not None and row[0] not in ("running", "finalizing"):
            if _join_covers_index(row[1], index):
                delivery_state = "delivered"  # already reported inside the join
            else:
                delivery_state = "pending"
                late = True
                evt["late_result"] = True
        cursor = conn.execute(
            """INSERT OR IGNORE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, completed_at, updated_at,
                event_json, result_json, delivery_state, delivered_at,
                origin_session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (child_id, parent.get("session_key", ""),
             parent.get("origin_ui_session_id", ""), parent.get("parent_session_id"),
             status, parent["dispatched_at"], now, now, json.dumps(evt),
             json.dumps(result), delivery_state,
             now if delivery_state == "delivered" else None,
             parent.get("origin_session_id", "")),
        )
        if cursor.rowcount and delivery_state == _JOINED_STATE:
            done = _joined_child_results(conn, delegation_id)
            unfinished = [i for i in range(len(goals)) if i not in done]
    if not cursor.rowcount:
        return "duplicate"
    if late:
        from tools.process_registry import process_registry
        process_registry.completion_queue.put(evt)
        return "late"
    if len(unfinished) == 1:
        _open_straggler_window(delegation_id, unfinished)
    return "duplicate" if delivery_state == "delivered" else "parked"


def _open_straggler_window(batch_id: str, unfinished: List[int]) -> None:
    """Every other helper has finished: start the straggler clock (once)."""
    with _records_lock:
        record = _records.get(batch_id)
        if (
            record is None or not record.get("_straggler_seconds")
            or record.get("_straggler_since") is not None
            or record.get("_straggler_joined")
        ):
            return
        record["_straggler_since"] = _now()
        notify = record.get("_straggler_notify")
    _notify_straggler(notify, unfinished, True)


def _notify_straggler(notify, indexes: List[int], waiting: bool) -> None:
    """Best-effort card note: the root is waiting on (or has stopped waiting for)
    these helpers. Never raises into the delivery path."""
    if not callable(notify):
        return
    try:
        notify(list(indexes), waiting)
    except Exception:
        logger.debug("straggler card note failed", exc_info=True)


def _sweep_stragglers(now: float) -> List[str]:
    """Join fan-outs whose last helper has outlasted its straggler window.

    Joined mode only. The runner keeps running the straggler; its result,
    when it lands, arrives once through the late-result path. The in-memory
    record is deliberately left ``running`` (capacity stays honest and the
    stall monitor still watches the straggler): the guarded join write makes
    the runner's own finalize a no-op.
    """
    due: List[Dict[str, Any]] = []
    with _records_lock:
        for record in _records.values():
            since = record.get("_straggler_since")
            seconds = record.get("_straggler_seconds") or 0
            if (
                since is None or seconds <= 0 or record.get("_straggler_joined")
                or record.get("status") not in ("running", "stalling")
                or now - since < seconds
            ):
                continue
            due.append(dict(record))
    joined: List[str] = []
    for record in due:
        batch_id = str(record.get("delegation_id") or "")
        try:
            record["completed_at"] = now
            combined = {
                "results": [], "error": None,
                "total_duration_seconds": round(
                    now - (record.get("dispatched_at") or now), 2
                ),
            }
            evt = _push_joined_batch_completion(record, combined, None)
        except Exception:
            # Busy/locked ledger (SessionDB shares state.db) or a bad row:
            # nothing was committed and nothing was flagged, so the next tick
            # retries this batch; one batch never stops the others or the
            # monitor.
            logger.warning(
                "Async delegation %s: straggler join could not be recorded; "
                "will retry on the next monitor tick", batch_id, exc_info=True,
            )
            continue
        # Committed (or already joined by someone else): only now is it done.
        with _records_lock:
            live = _records.get(batch_id)
            if live is not None:
                live["_straggler_joined"] = True
        if evt is not None:
            joined.append(batch_id)
            logger.info(
                "Async delegation %s: joining without helper(s) %s after the "
                "straggler window", batch_id, evt.get("incomplete_indexes"),
            )
            _notify_straggler(
                record.get("_straggler_notify"),
                evt.get("incomplete_indexes") or [], False,
            )
    return joined


def has_async_capacity(max_async_children: int) -> bool:
    """Whether one more async unit would currently fit.

    Advisory only: the authoritative check is the atomic one inside
    ``dispatch_async_delegation_batch``. Callers use this to refuse before
    building child agents they would only have to tear down again.
    """
    with _records_lock:
        running = sum(
            1 for r in _records.values()
            if r.get("status") in ("running", "stalling")
        )
    return running < max_async_children


class _AdmissionGate:
    """Execution fence between ``executor.submit`` and the dispatch decision.

    ``ThreadPoolExecutor.submit`` enqueues the work item *before* it starts or
    wakes a worker, so a ``submit`` that raises does not prove the item cannot
    run: a later (or already-busy) worker will still pick it up.  The queued
    wrapper therefore waits here for the dispatcher's verdict and only a
    verdict of "accepted" lets the runner start.  The first verdict wins.
    """

    def __init__(self) -> None:
        self._decided = threading.Event()
        self._lock = threading.Lock()
        self._accepted = False

    def _decide(self, accepted: bool) -> None:
        with self._lock:
            if not self._decided.is_set():
                self._accepted = accepted
                self._decided.set()

    def accept(self) -> None:
        self._decide(True)

    def abort(self) -> None:
        self._decide(False)

    def wait_accepted(self) -> bool:
        self._decided.wait()
        return self._accepted


def _withdraw_unscheduled_batch(delegation_id: str) -> None:
    """Undo a dispatch whose worker was never scheduled."""
    with _records_lock:
        _records.pop(delegation_id, None)
    try:
        _delete_durable_delegation(delegation_id)
    except Exception:
        logger.debug(
            "Could not delete durable row for unscheduled batch %s",
            delegation_id, exc_info=True,
        )


def dispatch_async_delegation_batch(
    *,
    goals: List[str],
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    origin_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    delegation_id: Optional[str] = None,
    progress_fn: Optional[Callable[[], tuple]] = None,
    fanout_delivery: str = FANOUT_EACH,
    request_identity: Optional[Dict[str, Any]] = None,
    requested_routes: Optional[List[Dict[str, Any]]] = None,
    owner_scope: Optional[Dict[str, str]] = None,
    accept_guard: Optional[Callable[[], bool]] = None,
    straggler_seconds: float = 0,
    straggler_notify: Optional[Callable[[List[int], bool], None]] = None,
) -> Dict[str, Any]:
    """Dispatch a fan-out batch as one background execution unit.

    Child terminal results may be published independently by the runner while
    siblings are still running. In ``each`` mode the final batch event carries
    only results that were not already published; an empty successful join is
    acknowledged without a second notification. Legacy runners without child
    publication still receive the consolidated batch event.

    In ``joined`` mode (``delegation.fanout_delivery``) children only commit
    durable receipts and the batch join re-enters the root once with every
    result in task order, unfinished children marked incomplete.
    ``request_identity`` (``turn_id`` / ``request_id`` / ``generation``) names
    the request the batch answers; it is persisted with the batch and stamped
    on the join and on any late result.

    ``straggler_seconds`` (joined mode; 0 = off): once every helper but one
    has finished, wait at most this long for the last one, then deliver the
    join with it marked incomplete. Its result, if it lands later, arrives
    once as a late result. ``straggler_notify(indexes, waiting)`` is a
    best-effort card hook fired when the wait opens and when it ends.
    """
    delegation_id = delegation_id or _new_delegation_id()
    dispatched_at = time.time()
    n = len(goals)
    # A combined goal label for status listings / the completion header.
    combined_goal = (
        goals[0] if n == 1 else f"{n} parallel subagents: " + "; ".join(g[:40] for g in goals)
    )
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": combined_goal,
        "goals": list(goals),
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "origin_session_id": origin_session_id,
        "parent_session_id": parent_session_id,
        **_capture_routing_origin(),
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "is_batch": True,
        "fanout_delivery": (
            FANOUT_JOINED if fanout_delivery == FANOUT_JOINED else FANOUT_EACH
        ),
        # Straggler window (joined mode only; in-memory, the durable join
        # recovery covers a dead owner).
        "_straggler_seconds": (
            float(straggler_seconds)
            if fanout_delivery == FANOUT_JOINED and straggler_seconds
            and float(straggler_seconds) > 0 else 0
        ),
        "_straggler_since": None,
        "_straggler_joined": False,
        "_straggler_notify": straggler_notify,
        "progress_fn": progress_fn,
        "_progress_token": None,
        "_progress_ts": dispatched_at,
        "_interrupted_at": None,
    }
    identity = request_identity or {}
    if requested_routes is not None:
        record['requested_routes'] = requested_routes
    if owner_scope:
        record['owner_scope'] = dict(owner_scope)
        record['job_epoch'] = __import__('uuid').uuid4().hex
    for _key, _src in (
        ("request_turn_id", "turn_id"),
        ("request_id", "request_id"),
        ("request_generation", "generation"),
    ):
        if identity.get(_src) not in (None, ""):
            record[_key] = identity[_src]
    with _records_lock:
        running = sum(
            1 for r in _records.values()
            if r.get("status") in ("running", "stalling")
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "reason": "capacity",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background units."
                ),
            }
        _records[delegation_id] = record

    # The record is visible (and counts against capacity) from here on, so any
    # failure before the worker is scheduled must withdraw it again; otherwise
    # a phantom "running" unit would hold a slot until the process exits.
    try:
        _persist_dispatch(record)
    except Exception as exc:
        _withdraw_unscheduled_batch(delegation_id)
        return {
            "status": "rejected",
            "reason": "persistence",
            "error": f"Failed to persist async delegation batch: {exc}",
        }
    try:
        executor = _get_executor(max_async_children)
    except Exception as exc:
        _withdraw_unscheduled_batch(delegation_id)
        return {
            "status": "rejected",
            "reason": "schedule",
            "error": f"Failed to start the async delegation executor: {exc}",
        }

    def _worker() -> None:
        combined: Dict[str, Any] = {}
        status = "error"
        try:
            combined = runner() or {}
            # Batch status: completed unless every child errored/was interrupted.
            child_results = combined.get("results") or []
            if child_results and all(
                (r.get("status") not in ("completed", "success"))
                for r in child_results
            ):
                status = "error"
            else:
                status = "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation batch %s crashed", delegation_id)
            combined = {
                "results": [],
                "error": f"{type(exc).__name__}: {exc}",
                "total_duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize_batch(delegation_id, combined, status)

    # A raising ``submit`` can leave the item queued (see _AdmissionGate), so
    # the queued wrapper may only start the runner once the dispatch is
    # accepted; every exit below resolves the verdict.
    gate = _AdmissionGate()

    def _admitted_worker() -> None:
        if gate.wait_accepted():
            _worker()

    submitted = False
    admitted = False
    try:
        # Propagate the dispatching profile to the detached batch children.
        executor.submit(propagate_context_to_thread(_admitted_worker))
        submitted = True
    except Exception as exc:
        _withdraw_unscheduled_batch(delegation_id)
        return {
            "status": "rejected",
            "reason": "schedule",
            "error": f"Failed to schedule async delegation batch: {exc}",
        }
    finally:
        if submitted:
            # Order actual child acceptance against scoped Stop's epoch fence.
            # The guard is runtime-only, memory-only, and never model supplied.
            with _records_lock:
                try:
                    admitted = accept_guard is None or bool(accept_guard())
                except Exception:
                    admitted = False
                if admitted:
                    gate.accept()
                else:
                    gate.abort()
        else:
            gate.abort()
    if not admitted:
        _withdraw_unscheduled_batch(delegation_id)
        return {'status': 'rejected', 'reason': 'revoked',
                'error': 'The admitted request was stopped before child acceptance; no child was started.'}
    if progress_fn is not None or record["_straggler_seconds"]:
        _ensure_stale_monitor_best_effort(delegation_id)

    logger.info(
        "Dispatched async delegation batch %s (%d task(s), session_key=%s)",
        delegation_id, n, session_key or "<cli>",
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize_batch(
    delegation_id: str, combined: Dict[str, Any], status: str
) -> None:
    """Mark a batch record complete and push ONE combined completion event."""
    if _held_for_resume(delegation_id) and (
        status not in ("completed", "success")
        or any(
            (r.get("status") or "error") not in ("completed", "success")
            for r in (combined.get("results") or []) if isinstance(r, dict)
        )
    ):
        return  # a helper was cut off by a gateway restart: the restart starts it again
    claimed = _begin_finalization(delegation_id)
    if claimed is None:
        _rescue_after_finalized(delegation_id, combined.get("results"))
        return
    event_record, _interrupt_fn = claimed

    try:
        _push_batch_completion_event(event_record, combined, status)
    except Exception:
        logger.warning(
            "Async delegation batch %s: could not record its join; keeping "
            "it and retrying", delegation_id, exc_info=True,
        )
        _abort_finalization(delegation_id, (True, combined, status))
        return
    _finish_finalization(delegation_id, status)


def _push_batch_completion_event(
    event_record: Dict[str, Any], combined: Dict[str, Any], status: str
) -> None:
    """Push a combined async-delegation batch completion event."""
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s finished but process_registry import "
            "failed; result lost: %s",
            event_record.get("delegation_id"), exc,
        )
        return

    if event_record.get("fanout_delivery") == FANOUT_JOINED:
        if _push_joined_batch_completion(event_record, combined, status) is None:
            # The join is already committed (straggler window, stall
            # recovery, another process). The runner's aggregate may still
            # hold results that never got a durable receipt — a publish that
            # failed on a busy ledger — and nothing else will ever deliver
            # them. Publish each as a late result; receipts that exist are
            # duplicates and change nothing.
            _publish_unrecorded_results(
                str(event_record.get("delegation_id") or ""),
                combined.get("results"),
            )
        return

    dispatched_at = event_record.get("dispatched_at") or time.time()
    completed_at = event_record.get("completed_at") or time.time()
    evt = {
        "type": "async_delegation",
        "delegation_id": event_record.get("delegation_id"),
        "session_key": event_record.get("session_key", ""),
        "origin_ui_session_id": event_record.get("origin_ui_session_id", ""),
        "origin_session_id": event_record.get("origin_session_id", ""),
        "parent_session_id": event_record.get("parent_session_id"),
        "goal": event_record.get("goal", ""),
        "goals": event_record.get("goals"),
        "context": event_record.get("context"),
        "toolsets": event_record.get("toolsets"),
        "role": event_record.get("role"),
        "model": event_record.get("model"),
        "status": status,
        "is_batch": True,
        # The full per-task results list — the formatter renders a
        # consolidated multi-task block from this.
        "results": combined.get("results") or [],
        # Per-task live transcript log paths (cache/delegation/live/...).
        # They persist after completion and double as the full-fidelity
        # operational record of each child's run.
        "live_transcripts": combined.get("live_transcripts"),
        "error": combined.get("error"),
        "total_duration_seconds": combined.get("total_duration_seconds"),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
    }
    # Routing origin captured at dispatch (see _capture_routing_origin).
    for _k in _ROUTING_ORIGIN_KEYS:
        if event_record.get(_k):
            evt[_k] = event_record[_k]
    # Structured stall metadata (#51690) — additive, present only on
    # stall-monitor finalizations.
    for _k in (
        "stalled_after_quiet_seconds",
        "stall_threshold_seconds",
        "stall_phase",
        "stall_grace_seconds",
    ):
        if _k in combined:
            evt[_k] = combined[_k]
    # A batch launched through delegate_tool publishes each terminal child
    # independently. Never replay those results again at join time. Preserve
    # the legacy consolidated block for callers without child publishing and
    # rescue any result whose child publish failed before the batch joined.
    with _DB_LOCK, _transaction() as conn:
        child_ids = _committed_child_ids(
            conn, str(event_record.get("delegation_id") or "")
        )
    evt["results"] = [
        entry for entry in evt["results"]
        if f"{event_record['delegation_id']}:child:{entry.get('task_index')}" not in child_ids
    ]
    if child_ids and not evt["results"] and evt.get("error"):
        evt["already_published_children"] = len(child_ids)
        err = str(evt.get("error") or "")
        err = err.replace(
            "and never produced a completion event.",
            f"and {len(child_ids)} child result(s) were already published separately.",
        )
        if "already published separately" not in err:
            err += (
                f" {len(child_ids)} child result(s) were already published "
                "separately and are not unknown."
            )
        evt["error"] = err
    if not evt["results"] and not evt.get("error"):
        # Atomic terminal ack: no empty join event can replay after owner loss.
        now = time.time()
        with _DB_LOCK, _transaction() as conn:
            conn.execute(
                """UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
                   result_json=?, delivery_state='delivered', delivered_at=?
                   WHERE delegation_id=?""",
                (status, completed_at, now, json.dumps(combined), now,
                 event_record["delegation_id"]),
            )
        return
    if len(evt["results"]) != len(combined.get("results") or []):
        evt["partial_child_delivery"] = True
    _persist_completion(evt, combined)
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s: failed to enqueue completion event; "
            "result lost: %s",
            event_record.get("delegation_id"), exc,
        )


def _ensure_stale_monitor_best_effort(delegation_id: str) -> None:
    """Start the stale monitor for a job that is already scheduled.

    Once ``submit`` has succeeded the job is running and owned, so a failure to
    start the monitoring thread (thread exhaustion) must not escape the
    dispatcher: the caller would see an exception for a live job and never
    record the handoff. The job simply runs without stall detection.
    """
    try:
        _ensure_stale_monitor()
    except Exception:
        logger.warning(
            "Async delegation %s is running but its stale monitor could not "
            "be started; stall detection is off for it.",
            delegation_id, exc_info=True,
        )


def _record_needs_monitor(record: Dict[str, Any]) -> bool:
    """Whether the monitor thread still has work for this in-memory record."""
    status = record.get("status")
    if status == "stalling":
        return True
    if status != "running":
        return False
    if record.get("progress_fn") is not None or record.get("_pending_finalize"):
        return True
    # A joined batch whose straggler window may still open or expire.
    return bool(record.get("_straggler_seconds")) and not record.get(
        "_straggler_joined"
    )


def _monitor_try_exit() -> bool:
    """Retire the calling monitor thread unless a record needs it.

    The exit decision is made under ``_monitor_lock`` against the records as
    they are NOW, and the thread's slot is cleared in the same hold.
    ``_ensure_stale_monitor`` takes the same lock, so a dispatch either
    registered its record before this check (the thread stays) or ensures
    after the slot is cleared (a fresh thread starts): a dispatch can never
    land between "nothing to monitor" and "thread gone" with no monitor.
    """
    global _monitor_thread
    with _monitor_lock:
        with _records_lock:
            if _pending_rescues or any(
                _record_needs_monitor(r) for r in _records.values()
            ):
                return False
        if _monitor_thread is threading.current_thread():
            _monitor_thread = None
        return True


def _ensure_stale_monitor() -> None:
    """Start (once) the module-level stale-delegation monitor thread.

    One daemon thread serves every dispatch; it exits on its own when no
    monitorable records remain (see ``_monitor_try_exit``), and is restarted
    by the next dispatch that carries a ``progress_fn`` or a straggler window.
    """
    global _monitor_thread
    with _monitor_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return
        _monitor_stop.clear()
        _monitor_thread = threading.Thread(
            target=_stale_monitor_loop,
            name="async-delegate-stale-monitor",
            daemon=True,
        )
        _monitor_thread.start()


def _stale_monitor_loop() -> None:
    """Sweep running delegations for stalled progress.

    Per sweep, for every running record with a ``progress_fn``:

    - Sample ``(token, in_tool)``. A changed token refreshes the record's
      progress timestamp — a child that keeps advancing is never touched, no
      matter how long it runs.
    - A frozen token past the idle/in-tool threshold marks the record
      ``stalling``: we call ``interrupt_fn`` so a responsive-but-slow child
      can unwind and deliver its (partial) result through the normal
      ``_finalize`` path with full fidelity.
    - A ``stalling`` record whose runner still hasn't returned after the
      grace window is force-finalized with one terminal ``stalled`` event so
      the owning session hears an outcome and the async slot frees. A late
      runner return after that is ignored by ``_begin_finalization``.
    """
    while not _monitor_stop.wait(_STALE_CHECK_INTERVAL):
        try:
            any_monitorable = _monitor_tick()
        except Exception:
            # The monitor is the only thing that frees a hung batch and the
            # only thing that ends a straggler wait: one bad sweep (a busy
            # shared database, a malformed row) must never end the thread.
            logger.warning(
                "Async delegation monitor sweep failed; continuing", exc_info=True,
            )
            any_monitorable = True
        if not any_monitorable and _monitor_try_exit():
            return


def _check_record_for_stall(
    record: Dict[str, Any], now: float,
    stalled: List[tuple], expired: List[str],
) -> bool:
    """One record's stall check (under ``_records_lock``). Returns whether the
    monitor still has work for it."""
    status = record.get("status")
    monitorable = False
    if (
        status in ("running", "stalling")
        and record.get("_straggler_seconds")
        and not record.get("_straggler_joined")
    ):
        monitorable = True  # a straggler window may still open or expire
    if status in ("running", "stalling") and record.get("_pending_finalize"):
        monitorable = True  # a completion still has to be recorded
    if status == "stalling":
        interrupted_at = record.get("_interrupted_at") or now
        if now - interrupted_at >= _STALL_GRACE_SECONDS:
            expired.append(record["delegation_id"])
        return True
    if status != "running":
        return monitorable
    progress_fn = record.get("progress_fn")
    if progress_fn is None:
        return monitorable
    try:
        token, in_tool = progress_fn()
    except Exception:
        # An unreadable child must not look permanently healthy — keep the
        # last timestamp running instead of refreshing it.
        token, in_tool = record.get("_progress_token"), False
    if token != record.get("_progress_token"):
        record["_progress_token"] = token
        record["_progress_ts"] = now
        return True
    quiet_for = now - (record.get("_progress_ts") or now)
    limit = _STALE_IN_TOOL_SECONDS if in_tool else _STALE_IDLE_SECONDS
    if quiet_for >= limit:
        record["status"] = "stalling"
        record["_interrupted_at"] = now
        # Structured stall context for the terminal event and status
        # listings (#51690): how long progress was frozen, which threshold
        # applied, and whether the child was inside a tool when it went quiet.
        record["_stall_quiet_seconds"] = round(quiet_for, 2)
        record["_stall_threshold_seconds"] = limit
        record["_stall_in_tool"] = bool(in_tool)
        stalled.append(
            (
                record["delegation_id"],
                bool(record.get("is_batch")),
                quiet_for,
                in_tool,
            )
        )
    return True


def _monitor_tick() -> bool:
    """One monitor sweep. Returns whether anything is still monitorable.

    Every step is isolated per record: a failure is logged against that
    batch and the rest of the sweep (and the next tick) carries on. Ledger
    writes here either commit or leave the record exactly as it was, so the
    next tick is the retry.
    """
    now = time.time()
    stalled: List[tuple] = []  # (delegation_id, is_batch, quiet_for, in_tool)
    expired: List[str] = []  # stalling past grace → force-finalize
    any_monitorable = False
    with _records_lock:
        for record in _records.values():
            try:
                if _check_record_for_stall(record, now, stalled, expired):
                    any_monitorable = True
            except Exception:
                logger.warning(
                    "Async delegation %s: stall check failed; will re-check",
                    record.get("delegation_id"), exc_info=True,
                )
                any_monitorable = True
    for delegation_id, _is_batch, quiet_for, in_tool in stalled:
        logger.warning(
            "Async delegation %s made no progress for %.0fs "
            "(in_tool=%s) — interrupting; grace window %.0fs",
            delegation_id, quiet_for, in_tool, _STALL_GRACE_SECONDS,
        )
        try:
            with _records_lock:
                record = _records.get(delegation_id)
                fn = record.get("interrupt_fn") if record else None
            if callable(fn):
                fn()
        except Exception as exc:
            logger.debug(
                "Async delegation %s stall interrupt failed: %s",
                delegation_id, exc,
            )
    for delegation_id in expired:
        try:
            _finalize_stalled(delegation_id)
        except Exception:
            logger.warning(
                "Async delegation %s: stall force-finalize failed; will "
                "retry on the next tick", delegation_id, exc_info=True,
            )
    try:
        _sweep_stragglers(_now())
    except Exception:
        logger.warning("Straggler sweep failed; will retry", exc_info=True)
        any_monitorable = True
    try:
        _retry_pending_finalizations()
    except Exception:
        logger.warning("Pending completion retry failed", exc_info=True)
        any_monitorable = True
    try:
        _retry_pending_rescues()
    except Exception:
        logger.warning("Pending late-result retry failed", exc_info=True)
    with _records_lock:
        if _pending_rescues:
            any_monitorable = True
    return any_monitorable


def _finalize_stalled(delegation_id: str) -> None:
    """Force-finalize a stalling delegation whose runner never returned."""
    claimed = _begin_finalization(delegation_id)
    if claimed is None:
        return
    event_record, _interrupt_fn = claimed

    completed_at = event_record.get("completed_at") or time.time()
    duration = round(
        completed_at - (event_record.get("dispatched_at") or completed_at),
        2,
    )
    quiet_seconds = event_record.get("_stall_quiet_seconds")
    threshold_seconds = event_record.get("_stall_threshold_seconds")
    stall_in_tool = event_record.get("_stall_in_tool")
    error = (
        f"Async delegation {delegation_id} stalled: the detached subagent "
        "stopped making progress (no new API calls, tool activity, or "
        "streamed tokens), did not respond to interruption, and never "
        "produced a completion event. The worker may be wedged inside a "
        "model API call — this is a known failure mode of long-lived "
        "gateway processes (#60203). Re-dispatch the task if it is still "
        "needed."
    )
    logger.error(
        "Async delegation %s force-finalized as stalled after %.0fs",
        delegation_id, duration,
    )
    # Structured stall metadata (#51690): lets parents and UIs distinguish
    # a stall-monitor kill from other failures without parsing the error
    # string, mirroring the sync path's timeout_seconds/timed_out_after_
    # seconds/timeout_phase fields.
    stall_meta = {
        "stalled_after_quiet_seconds": quiet_seconds,
        "stall_threshold_seconds": threshold_seconds,
        "stall_phase": (
            "in_tool" if stall_in_tool
            else "idle" if stall_in_tool is not None
            else None
        ),
        "stall_grace_seconds": _STALL_GRACE_SECONDS,
    }
    try:
        if event_record.get("is_batch"):
            _push_batch_completion_event(
                event_record,
                {
                    "results": [],
                    "error": error,
                    "total_duration_seconds": duration,
                    **stall_meta,
                },
                "stalled",
            )
        else:
            _push_completion_event(
                event_record,
                {
                    "status": "stalled",
                    "summary": None,
                    "error": error,
                    "api_calls": 0,
                    "duration_seconds": duration,
                    "exit_reason": "stalled",
                    **stall_meta,
                },
                "stalled",
            )
    except Exception:
        # Not committed: the record goes back to ``stalling`` exactly as it
        # was, so the next monitor tick repeats the force-finalize.
        logger.warning(
            "Async delegation %s: stall force-finalize could not be "
            "recorded; will retry on the next monitor tick",
            delegation_id, exc_info=True,
        )
        _abort_finalization(delegation_id)
        return
    _finish_finalization(delegation_id, "stalled")


def _children_activity_from_token(token: Any, now: float) -> Optional[List]:
    """Parse a progress token into per-child activity dicts (best-effort).

    delegate_tool's ``_batch_progress`` emits one ``(api_call_count,
    current_tool, last_activity_ts)`` tuple per child. Foreign token shapes
    (custom dispatchers) degrade to ``None`` entries rather than raising —
    the token contract is intentionally opaque to the registry.
    """
    try:
        parts = list(token)
    except TypeError:
        return None
    out: List[Optional[Dict[str, Any]]] = []
    for part in parts:
        if isinstance(part, (list, tuple)) and len(part) >= 2:
            entry: Dict[str, Any] = {
                "api_calls": part[0],
                "current_tool": part[1],
            }
            if len(part) >= 3 and isinstance(part[2], (int, float)):
                entry["seconds_since_activity"] = round(
                    max(0.0, now - float(part[2])), 1
                )
            out.append(entry)
        else:
            out.append(None)
    return out


def list_async_delegations() -> List[Dict[str, Any]]:
    """Snapshot of async delegations (running + recently completed).

    Safe to call from any thread. Excludes the non-serialisable callables
    and private monitor bookkeeping, but exposes computed live-status
    fields for UIs (#51690):

    - ``seconds_since_progress``: how long the stale monitor has seen a
      frozen progress token (running/stalling records).
    - ``children_activity``: per-child ``{api_calls, current_tool,
      seconds_since_activity}`` sampled live from the dispatch's
      ``progress_fn``.
    - ``stalled_after_quiet_seconds`` / ``stall_threshold_seconds`` /
      ``stall_in_tool``: stall context once the monitor has tripped.
    """
    now = time.time()
    samplers: Dict[str, Callable] = {}
    with _records_lock:
        items = []
        for r in _records.values():
            item = {
                k: v
                for k, v in r.items()
                if k not in {"interrupt_fn", "progress_fn"}
                and not k.startswith("_")
            }
            status = r.get("status")
            if status in ("running", "stalling"):
                ts = r.get("_progress_ts")
                if ts:
                    item["seconds_since_progress"] = round(now - ts, 1)
                fn = r.get("progress_fn")
                if callable(fn):
                    samplers[r["delegation_id"]] = fn
            if status in ("stalling", "stalled"):
                for src, dst in (
                    ("_stall_quiet_seconds", "stalled_after_quiet_seconds"),
                    ("_stall_threshold_seconds", "stall_threshold_seconds"),
                    ("_stall_in_tool", "stall_in_tool"),
                ):
                    if r.get(src) is not None:
                        item[dst] = r.get(src)
            items.append(item)

    # Sample live activity OUTSIDE the lock — progress_fn reads child-agent
    # attributes and must never run under _records_lock (a slow or broken
    # sampler would block every dispatch/finalize in the process).
    for item in items:
        fn = samplers.get(item.get("delegation_id"))
        if fn is None:
            continue
        try:
            token, in_tool = fn()
        except Exception:
            continue
        activity = _children_activity_from_token(token, now)
        if activity is not None:
            item["children_activity"] = activity
        item["in_tool"] = bool(in_tool)
    return items


def interrupt_all(reason: str = "shutdown") -> int:
    """Signal every running async delegation to stop. Returns how many.

    Used on ``/stop`` and gateway shutdown so a dangling background subagent
    can't keep burning tokens with no one listening. The child still emits a
    completion event (status='interrupted') via the normal finalize path.
    """
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values()
            if r.get("status") in ("running", "stalling")
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_all: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info("Interrupted %d async delegation(s) (%s)", count, reason)
    return count


def interrupt_for_session(
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
    reason: str = "session_end",
) -> int:
    """Signal running async delegations owned by ONE session to stop.

    A delegation's lifecycle is bound to the session that spawned it: when
    that session ends, its in-flight background subagents must end with it —
    a completed orphan would otherwise sit on the shared completion queue
    with no live owner, either leaking into another chat or burning tokens
    with no one listening (#55578).

    Selectors (any matching field claims the record):
    - ``origin_ui_session_id``: the live TUI tab/window that commissioned it.
    - ``session_key``: the durable routing key captured at dispatch.
    - ``parent_session_id``: the spawning agent's durable session-db id —
      the right selector for gateway chats, whose ``session_key`` (the
      platform conversation key) SURVIVES a ``/new`` reset while the
      session id rotates.

    Returns how many were interrupted.
    """
    if not session_key and not origin_ui_session_id and not parent_session_id:
        return 0
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values()
            if r.get("status") in ("running", "stalling")
            and _matches_session_selectors(
                r,
                session_key=session_key,
                origin_ui_session_id=origin_ui_session_id,
                parent_session_id=parent_session_id,
            )
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_for_session: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info(
            "Interrupted %d async delegation(s) for ending session (%s)",
            count, reason,
        )
    return count


def _reset_for_tests(drain_timeout: float = 0.0) -> None:
    """Test-only: clear all state and tear down the executor + monitor.

    ``drain_timeout`` > 0 first waits (bounded) for in-flight runner threads
    to return, so a runner released at the end of one test cannot finalize a
    same-id record registered by the next test.
    """
    global _executor, _executor_max_workers, _monitor_thread
    with _executor_lock:
        executor = _executor
        if executor is not None:
            executor.shutdown(wait=False)
        _executor = None
        _executor_max_workers = 0
    if executor is not None and drain_timeout > 0:
        deadline = time.monotonic() + drain_timeout
        for worker in list(getattr(executor, "_threads", ())):
            worker.join(max(0.0, deadline - time.monotonic()))
    _monitor_stop.set()
    with _monitor_lock:
        thread = _monitor_thread
        _monitor_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)
    with _records_lock:
        _records.clear()
        _pending_rescues.clear()
