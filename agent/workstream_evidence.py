"""Bounded retrieval of factual evidence from existing local Clover owners.

This module has no model, network, or state-writing path. Owner rows are
included only when their identifiers can be correlated to the supplied Scope.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from agent import workstreams

logger = logging.getLogger(__name__)
_DEFAULT_MAX_BYTES = 1200
_MAX_MAX_BYTES = 4096
_LIVE_TTL = timedelta(minutes=5)


@dataclass(frozen=True)
class TurnEvidence:
    """Immutable evidence result."""

    status: str
    text: str
    source_ids: tuple[str, ...]
    missing_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status not in {"complete", "partial", "unknown", "disabled"}:
            raise ValueError(f"invalid evidence status: {self.status!r}")
        if not isinstance(self.source_ids, tuple) or not isinstance(self.missing_keys, tuple):
            raise TypeError("source_ids and missing_keys must be tuples")


def _moment(now: Optional[datetime]) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _clip(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    marker = "…".encode("utf-8")
    if limit < len(marker):
        return raw[:limit].decode("utf-8", "ignore")
    return raw[: limit - len(marker)].decode("utf-8", "ignore") + "…"


def _safe_text(value: Any) -> str:
    try:
        text = workstreams._redact(str(value or ""))
        # The shared redactor may shorten long credentials but retain a suffix.
        text = re.sub(r"\bsk-[A-Za-z0-9._…-]{8,}", "[redacted]", text, flags=re.IGNORECASE)
        return text.replace("\x00", " ").strip()
    except Exception:
        logger.debug("evidence redaction failed closed", exc_info=True)
        return "[text withheld]"


def _settings(agent: Any) -> workstreams.ProactiveSettings:
    config = getattr(agent, "_workstream_settings", None)
    return workstreams.load_settings(config if isinstance(config, Mapping) else None)


def _scope_matches_agent(agent: Any, scope: workstreams.Scope) -> bool:
    if not isinstance(scope, workstreams.Scope) or scope.profile != workstreams.current_profile():
        return False
    agent_scope = getattr(agent, "_workstream_scope", None)
    return agent_scope is None or (isinstance(agent_scope, workstreams.Scope) and agent_scope == scope)


def _path_matches(value: Any, project: str) -> bool:
    if not value:
        return False
    try:
        return Path(str(value)).expanduser().resolve() == Path(project).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _session_identity_matches(row: Mapping[str, Any], scope: workstreams.Scope) -> bool:
    if not _path_matches(row.get("cwd"), scope.project):
        return False
    if scope.audience == "private" and scope.root == "local":
        return str(row.get("source") or "").lower() in {"cli", "tui"}
    if scope.audience.startswith("private:"):
        return (
            str(row.get("chat_type") or "").lower() in {"dm", "private"}
            and str(row.get("user_id") or "") == scope.root
        )
    return False


def _session_candidates(
    scope: workstreams.Scope, query: str, moment: datetime
) -> tuple[list[tuple[str, str]], set[str], bool, bool]:
    """Read transcript rows via the profile-local SessionDB owner."""
    from clover_constants import get_clover_home
    from clover_state import SessionDB

    found: list[tuple[str, str]] = []
    session_ids: set[str] = set()
    stale = False
    try:
        db = SessionDB(db_path=get_clover_home() / "state.db", read_only=True)
        try:
            for listed in db.search_sessions(limit=30):
                sid = str(listed.get("id") or "")
                if not sid:
                    continue
                row = db.get_session(sid)
                if not row or not _session_identity_matches(row, scope):
                    continue
                session_ids.add(sid)
                for message in db.get_messages(sid, limit=80, latest=True):
                    if str(message.get("role") or "") != "user":
                        continue
                    body = _clip(_safe_text(message.get("content")), 2048)
                    if not body or workstreams.relevance(query, body) <= 0:
                        continue
                    if workstreams.classify_question(query) == "live":
                        try:
                            seen = datetime.fromtimestamp(float(str(message.get("timestamp"))), timezone.utc)
                        except (TypeError, ValueError, OSError):
                            stale = True
                            continue
                        if moment - seen > _LIVE_TTL or seen > moment:
                            stale = True
                            continue
                    source_id = f"session:{sid}:message:{message.get('id')}"
                    found.append((source_id, f"Unverified user transcript statement: {body}"))
                    if len(found) >= 12:
                        return found, session_ids, stale, False
        finally:
            db.close()
        return found, session_ids, stale, False
    except Exception:
        logger.warning("session evidence owner unavailable", exc_info=True)
        return found, session_ids, stale, True


def _cron_candidates(
    scope: workstreams.Scope, query: str, moment: datetime
) -> tuple[list[tuple[str, str]], bool, bool, bool]:
    """Read job, execution and monitor state through existing cron owners."""
    from cron import executions, jobs

    found: list[tuple[str, str]] = []
    stale = conflict = False
    try:
        for job in jobs.list_jobs(include_disabled=True)[:100]:
            if not _path_matches(job.get("workdir"), scope.project):
                continue
            job_id = str(job.get("id") or "")
            prompt = _safe_text(job.get("prompt") or job.get("name") or "")
            if not job_id or workstreams.relevance(query, prompt) <= 0:
                continue
            statuses: set[str] = set()
            for execution in executions.list_executions(job_id=job_id, limit=20):
                execution_id = str(execution.get("id") or "")
                status = str(execution.get("status") or "").lower()
                seen_text = execution.get("finished_at") or execution.get("claimed_at")
                if not execution_id or status not in {"completed", "failed", "running", "interrupted", "unknown"}:
                    continue
                statuses.add(status)
                if workstreams.classify_question(query) == "live":
                    try:
                        seen = datetime.fromisoformat(str(seen_text).replace("Z", "+00:00"))
                        if seen.tzinfo is None:
                            seen = seen.replace(tzinfo=timezone.utc)
                    except (TypeError, ValueError):
                        stale = True
                        continue
                    if moment - seen > _LIVE_TTL or seen > moment:
                        stale = True
                        continue
                found.append((
                    f"cron-execution:{job_id}:{execution_id}",
                    f"Cron execution record status={status} for job {job_id}; "
                    "this does not establish the requested deliverable outcome.",
                ))
            if "completed" in statuses and "failed" in statuses:
                conflict = True
            state = job.get("monitor_state")
            if isinstance(state, Mapping) and state.get("last_output_hash"):
                changed_at = str(state.get("last_changed_at") or "")
                try:
                    seen = datetime.fromisoformat(changed_at.replace("Z", "+00:00"))
                    if seen.tzinfo is None:
                        seen = seen.replace(tzinfo=timezone.utc)
                    if moment - seen > _LIVE_TTL or seen > moment:
                        stale = True
                    else:
                        found.append((
                            f"cron-monitor:{job_id}",
                            f"Monitor output changed at {changed_at}; execution outcome is not established.",
                        ))
                except (TypeError, ValueError):
                    stale = True
        return found, stale, conflict, False
    except Exception:
        logger.warning("cron evidence owner unavailable", exc_info=True)
        return found, stale, conflict, True


def _runtime_candidates(
    scoped_session_ids: set[str], query: str
) -> tuple[list[tuple[str, str]], bool, bool]:
    """Use runtime owners only when their origin session is in the scoped DB."""
    found: list[tuple[str, str]] = []
    process_failed = delegation_failed = False
    try:
        from tools.process_registry import process_registry

        for row in process_registry.list_sessions()[:64]:
            process_id = str(row.get("session_id") or "")
            process = process_registry.get(process_id) if process_id else None
            if process is None or process.parent_session_id not in scoped_session_ids:
                continue
            if workstreams.relevance(query, _safe_text(process.command)) <= 0:
                continue
            status = "running" if not process.exited else f"exited with code {process.exit_code}"
            found.append((f"process:{process.id}", f"Process owner reports {status}; no task success inferred."))
    except Exception:
        logger.warning("process evidence owner unavailable", exc_info=True)
        process_failed = True
    try:
        from tools.async_delegation import get_durable_delegation, list_async_delegations

        for row in list_async_delegations()[:64]:
            delegation_id = str(row.get("delegation_id") or "")
            if not delegation_id:
                continue
            durable = get_durable_delegation(delegation_id)
            if not durable or durable.get("origin_session_id") not in scoped_session_ids:
                continue
            state = str(durable.get("state") or "unknown")
            found.append((f"delegation:{delegation_id}", f"Delegation owner state={state}; result meaning is not inferred."))
    except Exception:
        logger.warning("delegation evidence owner unavailable", exc_info=True)
        delegation_failed = True
    return found, process_failed, delegation_failed


def _fit_lines(lines: Sequence[str], budget: int) -> str:
    body = ""
    for line in lines:
        candidate = (body + "\n" + line).strip()
        if len(candidate.encode("utf-8")) <= budget:
            body = candidate
            continue
        if not body:
            body = _clip(line, budget)
        break
    return body


def retrieve_turn_evidence(
    agent: Any,
    *,
    scope: workstreams.Scope,
    query: str,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    now: Optional[datetime] = None,
) -> TurnEvidence:
    """Retrieve cited local evidence without turning absence into success."""
    try:
        settings = _settings(agent)
    except Exception:
        logger.warning("workstream settings unavailable", exc_info=True)
        return TurnEvidence("unknown", "Evidence unknown: settings unavailable.", (), ("evidence",))
    if not settings.enabled or not settings.recall_enabled:
        return TurnEvidence("disabled", "", (), ())
    if not _scope_matches_agent(agent, scope) or (settings.private_only and not scope.private):
        return TurnEvidence("unknown", "", (), ("scope",))
    question = _safe_text(query)
    if not question:
        return TurnEvidence("unknown", "Evidence unknown: empty query.", (), ("evidence",))

    moment = _moment(now)
    sessions, scoped_session_ids, session_stale, session_failed = _session_candidates(scope, question, moment)
    cron_rows, stale, conflict, cron_failed = _cron_candidates(scope, question, moment)
    runtime_rows, process_failed, delegation_failed = _runtime_candidates(scoped_session_ids, question)
    candidates = sessions + cron_rows + runtime_rows
    source_ids = tuple(dict.fromkeys(source_id for source_id, _ in candidates))
    lines = [f"[{source_id}] {text}" for source_id, text in candidates]
    budget = max(0, min(int(max_bytes), settings.recall_max_bytes, _MAX_MAX_BYTES))
    missing: list[str] = []
    if stale or session_stale:
        missing.append("fresh_evidence")
    if conflict:
        missing.append("conflicting_evidence")
    if session_failed:
        missing.append("session_search")
    if cron_failed:
        missing.append("cron_evidence")
    if process_failed:
        missing.append("process_state")
    if delegation_failed:
        missing.append("delegation_receipt")
    if not candidates:
        missing.append("matching_evidence")
        lines = ["Evidence unknown: no matching, scope-verified local record was found."]
        status = "unknown"
    else:
        # Historical user text and owner execution states are citations, not
        # verified deliverable facts. Never report them as complete.
        missing.append("verified_outcome")
        status = "partial"
    text = _fit_lines(lines, budget) if budget else ""
    if len(text.encode("utf-8")) > budget:
        text = _clip(text, budget)
    cited_ids = tuple(source_id for source_id in source_ids if source_id in text)
    if source_ids and not cited_ids:
        missing.append("citation_byte_budget")
        return TurnEvidence("unknown", "", (), tuple(dict.fromkeys(missing)))
    if len(cited_ids) != len(source_ids):
        missing.append("truncated_evidence")
    return TurnEvidence(status, text, cited_ids, tuple(dict.fromkeys(missing)))


# ── host demand path ───────────────────────────────────────────────────────

_ASKS_OUTCOME = re.compile(
    r"\b(did|does|do|has|have|had|was|were|is|are|will)\b", re.IGNORECASE)
_OUTCOME_WORD = re.compile(
    r"\b(run|ran|running|pass|passed|passing|fail|failed|failing|finish|finished|"
    r"complete|completed|succeed|succeeded|done|deployed|merged|ready|status|"
    r"result|outcome|stuck|working|worked|healthy|finishing|started|stopped)\b",
    re.IGNORECASE,
)
_MAX_QUERY_CHARS = 512


def is_evidence_request(query: Any) -> bool:
    """Deterministic, local test for a question about whether/what happened.

    Needs a question mark, an auxiliary/aspect verb and an outcome word.
    Ordinary statements and lookups ("which target is configured?") never
    trigger retrieval, so they carry no extra context bytes.
    """
    if not isinstance(query, str):
        return False
    text = query.strip()
    if not text or len(text) > _MAX_QUERY_CHARS or "?" not in text:
        return False
    return bool(_ASKS_OUTCOME.search(text) and _OUTCOME_WORD.search(text))


def evidence_context(
    agent: Any, scope: workstreams.Scope, query: Any, *, now: Optional[datetime] = None,
) -> str:
    """Bounded current-turn evidence block for an evidence-gap question, else "".

    Cited local owners only; absence is reported as explicitly unknown and
    never as success. Read-only: no network, no model, no state write.
    """
    if not is_evidence_request(query):
        return ""
    result = retrieve_turn_evidence(agent, scope=scope, query=str(query), now=now)
    if result.status == "disabled" or not result.text:
        return ""
    lines = [
        "Local evidence for this question (read-only, cited; a record is not "
        "proof of an outcome unless it says so):",
        result.text,
    ]
    if result.missing_keys:
        lines.append("Not established: " + ", ".join(result.missing_keys) + ".")
    return "\n".join(lines)
