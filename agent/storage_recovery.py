"""Bounded, same-turn storage recovery; never an action replay or spawn right.

A locked result flush gets one durable checkpoint and one short persistence-only
retry. A process restart does NOT consume these files: uncertain effects require
owner reconciliation, not automatic tool replay. Canonical SQLite remains the
only source of model/UI-visible progress.
"""
from __future__ import annotations

import copy
from collections import Counter
import hashlib
import json
import logging
import os
from pathlib import Path
import stat
import tempfile
import uuid

logger = logging.getLogger(__name__)
_RECOVERY_PATIENCE_S = .25


def _job_identity(agent):
    """Observe an accepted background owner's memory tombstone, without DB IO."""
    did = getattr(agent, "_delegation_id", None)
    if not isinstance(did, str) or not did:
        return None  # inline child: authority stays with its current turn
    from tools import async_delegation as ad

    with ad._records_lock:
        record = ad._records.get(did)
        if record is None:
            return (did, None, None, None, True, None)
        return (did, id(record), record.get("job_epoch"), record.get("status"),
                bool(record.get("_ownership_revoked")), record.get("_interrupted_at"))


def _identity(agent):
    db = getattr(agent, "_session_db", None)
    path = Path(db.db_path)
    info = path.stat()
    ref = getattr(agent, "_delegate_parent_ref", None)
    parent = ref() if callable(ref) else None
    return (db, (info.st_dev, info.st_ino), str(path.resolve()),
            getattr(agent, "session_id", None), getattr(agent, "_current_turn_id", None),
            getattr(agent, "_orchestration_stop_epoch", 0),
            getattr(agent, "model", None), getattr(agent, "provider", None),
            getattr(agent, "base_url", None), parent,
            getattr(parent, "session_id", None),
            getattr(parent, "_orchestration_stop_epoch", 0),
            getattr(agent, "model_pinned", None),
            getattr(agent, "_active_session_turn_lease_holder", None),
            getattr(agent, "_active_compression_lock_holder", None),
            _job_identity(agent))


def _live(agent, identity):
    try:
        parent = identity[9]
        job = identity[-1]
        hard = getattr(agent, "_hard_interrupt_requested", None)
        return (not getattr(agent, "_interrupt_requested", False)
                and not (hard is not None and hard.is_set())
                and not getattr(parent, "_interrupt_requested", False)
                and (job is None or (job[3] == "running" and not job[4] and not job[5]))
                and _identity(agent) == identity)
    except Exception:
        return False


def _write_checkpoint(path, payload):
    """Atomic private file; POSIX barriers cover both directory entries."""
    directory = path.parent
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("storage recovery directory is not a real directory")
    if os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):  # windows-footgun: ok — short-circuit POSIX gate
        raise OSError("storage recovery directory is not private")
    directory_fd = parent_fd = None
    temporary = None
    try:
        if os.name == "posix":
            # Pin the validated directory through creation, rename and cleanup.
            # A pathname swap must not redirect the sensitive temporary file.
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            pinned = os.fstat(directory_fd)
            if ((info.st_dev, info.st_ino) != (pinned.st_dev, pinned.st_ino)
                    or pinned.st_uid != os.getuid() or stat.S_IMODE(pinned.st_mode) & 0o077):  # windows-footgun: ok — inside POSIX-only branch
                raise OSError("storage recovery directory changed or is not private")
            parent_fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            linked = os.stat(directory.name, dir_fd=parent_fd, follow_symlinks=False)
            if (linked.st_dev, linked.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise OSError("storage recovery directory changed")
            # Also sync an existing directory's parent: another creator may have
            # crashed or failed its barrier after mkdir. Existence is not durability.
            os.fsync(parent_fd)
            candidate = ".pending-" + uuid.uuid4().hex
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            temporary = candidate  # cleanup only a file this call actually created
        else:
            fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        if directory_fd is not None:
            os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
            linked = os.stat(directory.name, dir_fd=parent_fd, follow_symlinks=False)
            if (linked.st_dev, linked.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise OSError("storage recovery directory changed")
        else:
            os.replace(temporary, path)
    finally:
        try:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
            if directory_fd is not None:
                os.close(directory_fd)


def _progress_fingerprint(message):
    # Persisted markers and row IDs are assigned only after COMMIT. Hash the
    # public message fields instead; retain no second in-memory transcript.
    public = {key: value for key, value in message.items() if not key.startswith("_")}
    encoded = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _retire_checkpoint(agent, identity, messages):
    """Retire only this turn's known-committed payload, never replay it."""
    scope = (identity[1], identity[2], identity[3], identity[4])
    metadata = getattr(agent, "_storage_recovery_metadata", None)
    path = getattr(agent, "_storage_recovery_checkpoint", None)
    if (getattr(agent, "_storage_recovery_identity", None) != scope
            or not isinstance(metadata, dict) or not isinstance(path, str)
            or getattr(agent, "_storage_recovery_receipt_failed", False)
            or metadata.get("disposition") == "canonical"):
        return
    try:
        from agent.context_compressor import _DB_PERSISTED_MARKER
        required = getattr(agent, "_storage_recovery_pending_fingerprints", None)
        if not isinstance(required, Counter) or not required:
            return
        saved = Counter(_progress_fingerprint(message) for message in messages
                        if isinstance(message, dict) and message.get(_DB_PERSISTED_MARKER))
        if required - saved:
            return  # a successful prefix flush did not save checkpointed work
        current = Path(identity[2]).stat()
        if (current.st_dev, current.st_ino) != identity[1]:
            return  # the known commit belongs to a different database file
        canonical = {**metadata, "disposition": "canonical"}
        _write_checkpoint(Path(path), canonical)
        agent._storage_recovery_metadata = canonical
        agent._storage_recovery_pending_fingerprints = None
    except Exception:
        # A failed receipt barrier/replacement leaves uncertain evidence. A
        # later ordinary flush is not new authority to alter that failed file.
        agent._storage_recovery_receipt_failed = True
        logger.warning("Could not mark storage checkpoint canonical", exc_info=True)


def _same_database(identity):
    try:
        current = Path(identity[2]).stat()
        return (current.st_dev, current.st_ino) == identity[1]
    except OSError:
        return False


def flush_with_recovery(agent, messages, history=None):
    """Called under the agent's existing persistence lock, without a new turn."""
    # Capture authority before the failed attempt, not after Stop/reset/model
    # changes that could happen while SQLite's busy handler was waiting.
    eligible = (getattr(agent, "_delegate_depth", 0) > 0
                and bool(getattr(agent, "_current_turn_id", None))
                and not getattr(agent, "_persist_disabled", False)
                and getattr(agent, "_session_db", None) is not None)
    try:
        identity = _identity(agent) if eligible else None
    except Exception:
        identity = None
    result = agent._flush_messages_to_session_db_unlocked(messages, history)
    if result is True and identity is not None:
        if not _same_database(identity):
            agent._last_session_persist_confirmed = False
            agent._last_persistence_error_cause = "unknown"
            return False
        _retire_checkpoint(agent, identity, messages)
        agent._last_persistence_error_cause = None
    if result is not False or identity is None:
        return result
    if getattr(agent, "_last_persistence_error_cause", None) != "locked":
        return False
    # Only already-produced tool results qualify. A failed pre-execution
    # assistant append does not authorize execution, and a missing result is
    # never synthesized. Each turn gets at most one recovery attempt.
    from agent.context_compressor import _DB_PERSISTED_MARKER
    if (not any(m.get("role") == "tool" and not m.get(_DB_PERSISTED_MARKER)
                for m in messages if isinstance(m, dict))
            or getattr(agent, "_storage_recovery_turn", None) == identity[4]):
        return False
    agent._storage_recovery_turn = identity[4]
    agent._storage_recovery_checkpoint = None
    agent._storage_recovery_metadata = None
    agent._storage_recovery_identity = None
    agent._storage_recovery_receipt_failed = False
    agent._storage_recovery_pending_fingerprints = None
    path = Path(identity[2]).parent / "storage-recovery" / (uuid.uuid4().hex + ".json")
    try:
        payload = {"schema": 1, "session_id": identity[3], "turn_id": identity[4],
                   "model": identity[6], "provider": identity[7],
                   "database_identity": list(identity[1]), "disposition": "pending",
                   "automatic_replay_allowed": False, "messages": copy.deepcopy(messages)}
        from run_agent import _is_ephemeral_scaffolding
        # Include saved history too: an older identical result must not stand
        # in for a new pending result when providers reuse tool-call IDs.
        pending = Counter(_progress_fingerprint(message) for message in messages
                          if isinstance(message, dict) and not _is_ephemeral_scaffolding(message))
        _write_checkpoint(path, payload)
        agent._storage_recovery_pending_fingerprints = pending
        agent._storage_recovery_checkpoint = str(path)
        agent._storage_recovery_identity = (identity[1], identity[2], identity[3], identity[4])
        agent._storage_recovery_metadata = {key: value for key, value in payload.items() if key != "messages"}
    except Exception:
        logger.warning("Could not durably checkpoint blocked tool progress", exc_info=True)
        return False
    if not _live(agent, identity):
        return False
    # Do not rerun a provider call, dispatch a tool, refresh a budget, clear
    # Stop, change model, or create any parent callback/spawn authority here.
    result = agent._flush_messages_to_session_db_unlocked(
        messages, history, _adoption_budget=0,
        _recovery_patience_s=_RECOVERY_PATIENCE_S)
    if result is not True:
        return False
    if not _same_database(identity):
        # COMMIT belongs to the original file, not an unrelated replacement
        # now at its path. Keep the pending evidence for owner reconciliation.
        agent._last_session_persist_confirmed = False
        agent._last_persistence_error_cause = "unknown"
        return False
    # SQLite now owns the transcript. Keep only diagnostic identity; this
    # same retirement also runs if an ordinary later flush saves the result.
    _retire_checkpoint(agent, identity, messages)
    agent._last_persistence_error_cause = None
    # Revocation still forbids continuation, but cannot undo a known commit.
    # Retire its sensitive payload even when Stop won immediately after COMMIT.
    return _live(agent, identity)
