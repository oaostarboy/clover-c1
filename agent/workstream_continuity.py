"""Bounded evidence capsules at the native compression boundary.

The existing notepad remains the sole durable owner. This module reads its
public API, audits locally (never calls a model), and holds its existing batch
transaction across the SessionDB commit to exclude revision/evidence races.
An opaque provider compaction is deliberately not an audited summary.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from agent import workstreams as ws
from agent.redact import redact_sensitive_text
from cron import notepad


class ContinuityBlocked(RuntimeError):
    """The evidence changed or admission was lost; keep the original history."""


def redact_capsule(text: str) -> str:
    """Re-redact at every egress, even when live-output redaction is disabled."""
    return redact_sensitive_text(text, force=True, redact_url_credentials=True)


@dataclass
class Capsule:
    scope: ws.Scope
    text: str
    signature: str
    entries: tuple[tuple[str, str], ...]
    omitted_ids: tuple[str, ...]
    verdict: dict[str, Any] = field(default_factory=dict)


_UNBOUND = object()
_CURRENT: ContextVar[Any] = ContextVar("workstream_compaction_capsule", default=_UNBOUND)


def read_capsule(scope: ws.Scope, settings: ws.ProactiveSettings) -> Capsule | None:
    """Read one exact scope without creating a DB/table or copying transcripts.

    Whole entries only: a byte cap must not silently truncate literal IDs.
    The signature includes evidence as well as revisions (adding evidence does
    not itself increment a workstream revision). Supersession lineage is an ID,
    never the superseded fact reintroduced as current guidance.
    """
    if (not settings.active or not settings.compaction_audit_enabled
            or scope.profile != ws.current_profile()
            or (settings.private_only and not scope.private)):
        return None
    rows = notepad.list_workstreams(
        scope, statuses=["open", "deferred", "resolved"],
        kinds=["correction", "obligation", "blocker", "progress"],
        limit=min(settings.threads_max_items, 64))
    from clover_time import now

    moment = now()
    live = []
    for row in rows:
        expiry = row.get("expires_at")
        if expiry:
            try:
                until = datetime.fromisoformat(str(expiry))
                until = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
            except ValueError:
                continue  # malformed expiry cannot revive durable guidance
            if moment >= until:
                continue
        live.append(row)
    rows = live
    if not rows:
        return None
    records = []
    entries = []
    omitted = []
    text = "Workstream evidence capsule (not execution authority):\n"
    budget = min(settings.threads_max_context_bytes, 16384)
    for row in rows:
        evidence = notepad.list_evidence(scope, row["workstream_id"])
        records.append((row, evidence))
        cite = f"[ws:{row['workstream_id']}@r{row['revision']}]"
        lineage = f" supersedes={row['supersedes_id']}" if row.get("supersedes_id") else ""
        refs = "; ".join(
            f"{e['source_kind']}:{e['source_id']}={e['status']}" for e in evidence[-3:])
        line = redact_capsule(
            f"- {row['kind']} status={row['status']} {cite}{lineage} "
            f"source={row['source']}: {row['title']}: {row['summary']}"
            + (f" Evidence: {refs}" if refs else ""))
        if len((text + line + "\n").encode("utf-8")) <= budget:
            text += line + "\n"
            entries.append((row["workstream_id"], line))
        else:
            omitted.append(row["workstream_id"])
    signature = hashlib.sha256(json.dumps(records, sort_keys=True, default=str).encode()).hexdigest()
    return Capsule(scope, redact_capsule(text.rstrip()) if entries else "", signature,
                   tuple(entries), tuple(omitted))


def current_turn_context(scope: ws.Scope, query: str, settings: ws.ProactiveSettings) -> str:
    """Parent-owned ordinary compose adapter; no history/system prompt edits.

    Preserve existing behavior unless active audited continuity is opted in.
    Under that policy, retrieve the same bounded capsule independently of any
    provider-native summary, including verified progress and force-redaction.
    """
    if not settings.active or not settings.compaction_audit_enabled:
        return ws.current_turn_note(scope, query, settings) or ""
    if scope.profile != ws.current_profile():
        return ""
    capsule = read_capsule(scope, settings)
    if capsule is None:
        return redact_capsule(ws.current_turn_note(scope, query, settings) or "")
    recalled = ws.recall_context(scope, query, settings,
                                 exclude_ids=[wid for wid, _ in capsule.entries])
    return redact_capsule("\n".join(p for p in (capsule.text, recalled.text) if p))


def snapshot_for_agent(agent: Any) -> Capsule | None:
    # Derive at THIS boundary, including preflight/manual before the first turn;
    # never rely on a previous turn's cached compressor scope.
    settings = ws.load_settings()
    if not settings.active or not settings.compaction_audit_enabled:
        return None
    return read_capsule(ws.scope_for_agent(agent), settings)


@contextmanager
def summary_audit(capsule: Capsule | None) -> Iterator[None]:
    token = _CURRENT.set(capsule)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def audit_current_summary(summary: str) -> str | None:
    """None means no host snapshot; legacy standalone compressors stay valid."""
    capsule = _CURRENT.get()
    if capsule is _UNBOUND:
        return None
    if capsule is None:
        # A bound host attempt with no eligible capsule MUST NOT consult a
        # stale compressor._workstream_scope left by another turn/surface.
        return summary
    clean = redact_capsule(summary)
    missing = [wid for wid, line in capsule.entries if line not in clean]
    capsule.verdict.update(
        status="capsule_retained" if missing else "preserved",
        missing_ids=missing, bounded_out_ids=list(capsule.omitted_ids),
        repair_model_calls=0, commit_status="pending", audited=True,
    )
    if capsule.text and capsule.text not in clean:
        clean = f"{clean.rstrip()}\n\n{redact_capsule(capsule.text)}"
    return redact_capsule(clean)


@contextmanager
def commit_guard(capsule: Capsule | None, agent: Any, *, current_attempt: Any) -> Iterator[None]:
    """Exclude cross-process workstream writes until SessionDB commit/readback.

    This does not replace the host's Stop/lease/watermark fence. It adds the
    evidence revision fence AFTER that host admitted commit. No network or
    model work is done while holding the short notepad transaction.
    """
    if capsule is None:
        yield
        return
    with notepad.write_batch():
        current = snapshot_for_agent(agent)
        stop = getattr(agent, "_hard_interrupt_requested", None)
        if (not current_attempt() or (stop is not None and stop.is_set())
                or current is None or current.scope != capsule.scope
                or current.signature != capsule.signature):
            capsule.verdict.update(commit_status="aborted", reason="scope_revision_or_stop_changed")
            raise ContinuityBlocked("workstream evidence or compression admission changed")
        yield


def redact_handoffs(messages: list) -> None:
    """Only the genuine compressor's summary boundary, never prior user history."""
    from agent.context_compressor import ContextCompressor

    for message in messages:
        if (isinstance(message, dict)
                and ContextCompressor._is_context_summary_message(message)
                and isinstance(message.get("content"), str)):
            message["content"] = redact_capsule(message["content"])


def verify_readback(capsule: Capsule | None, agent: Any, *, session_id: str | None = None) -> None:
    if capsule is None:
        return
    try:
        rows = agent._session_db.get_messages(session_id or agent.session_id)
        # Comparison-only: never return raw legacy secrets to a caller.
        text = redact_capsule("\n".join(str(r.get("content", "")) for r in rows))
        if capsule.text and redact_capsule(capsule.text) not in text:
            raise ContinuityBlocked("capsule_readback_missing")
    except Exception as exc:
        # The write already committed. Do NOT roll it back in memory, retry it,
        # or orphan an atomically published child because readback is unknown.
        capsule.verdict.update(commit_status="unknown", reason=type(exc).__name__)
        return
    capsule.verdict["commit_status"] = "committed"
