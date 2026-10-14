"""Opt-in workstream ledger policy (PLAN-v1): scope, settings, receipts,
feedback, novelty, evidence sufficiency and bounded context selection.

Storage lives in ``cron/notepad.py`` (the existing profile-local SQLite
owner). This module is pure policy plus a thin facade over it:

* Every entry point is empty/no-op when ``proactive.enabled`` is false —
  nothing is created, injected or called.
* Nothing here grants authority. Receipts are written only against a typed
  proof that the *owning* subsystem (executions ledger, durable delegation
  ledger, file readback, scheduler occurrence) confirms; model text is not a
  proof type. Feedback gates and judges can only suppress or defer.
* Silence is unknown, never rejection.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from clover_constants import get_clover_home

logger = logging.getLogger(__name__)

SHADOW = "shadow"
ACTIVE = "active"

KINDS = (
    "obligation", "blocker", "correction", "decision", "constraint",
    "progress", "procedure",
)
RECEIPT_KINDS = (
    "proposed", "scheduled", "authorized", "executed", "saved", "delivered",
    "deferred", "cancelled", "unknown",
    "feedback_accept", "feedback_reject", "feedback_correct", "feedback_defer",
)
FEEDBACK_KINDS = ("accept", "reject", "correct", "defer", "done", "stop")
OUTCOME_STATUSES = ("verified", "failed", "unverified", "not_run", "stopped")

MIN_RELEVANCE = 0.5
_LIVE_SOURCE_KINDS = frozenset({"process_state", "project_check", "live_lookup"})


# ── clock ──────────────────────────────────────────────────────────────────

Clock = Callable[[], datetime]


def _now(now: Optional[datetime] = None) -> datetime:
    if now is not None:
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    from clover_time import now as clover_now

    return clover_now()


def _parse(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ── settings ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProactiveSettings:
    enabled: bool = False
    mode: str = SHADOW
    eligible_jobs: Tuple[str, ...] = ()
    private_only: bool = True
    threads_max_items: int = 16
    threads_max_context_bytes: int = 4096
    threads_retention_days: int = 30
    recall_enabled: bool = False
    recall_max_items: int = 3
    recall_max_bytes: int = 1200
    suggestions_enabled: bool = False
    suggestions_cooldown_seconds: int = 3600
    suggestions_max_per_session: int = 1
    procedures_enabled: bool = False
    procedures_min_verified_runs: int = 3
    procedures_min_sessions: int = 2
    compaction_audit_enabled: bool = False
    compaction_audit_max_repairs: int = 0
    aux_max_calls_per_event: int = 1
    aux_max_calls_per_day: int = 4
    aux_max_output_tokens: int = 512

    @property
    def active(self) -> bool:
        return self.enabled and self.mode == ACTIVE


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "yes", "on", "1"}:
            return True
        if low in {"false", "no", "off", "0"}:
            return False
    return default


def _as_int(value: Any, default: int, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= minimum else default


def _section(root: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = root.get(name)
    return value if isinstance(value, Mapping) else {}


def load_settings(config: Optional[Mapping[str, Any]] = None) -> ProactiveSettings:
    """Parse ``proactive:`` from config. Never raises; any failure is disabled."""
    try:
        if config is None:
            from clover_cli.config import load_config

            config = load_config()
        root = _section(config or {}, "proactive")
        d = ProactiveSettings()
        threads, recall = _section(root, "threads"), _section(root, "recall")
        sugg, proc = _section(root, "suggestions"), _section(root, "procedures")
        audit, aux = _section(root, "compaction_audit"), _section(root, "aux")
        mode = str(root.get("mode", d.mode)).strip().lower()
        jobs = root.get("eligible_jobs", ())
        return ProactiveSettings(
            enabled=_as_bool(root.get("enabled"), d.enabled),
            mode=mode if mode in (SHADOW, ACTIVE) else SHADOW,
            eligible_jobs=tuple(str(j) for j in jobs if str(j))
            if isinstance(jobs, (list, tuple)) else (),
            private_only=_as_bool(root.get("private_only"), d.private_only),
            threads_max_items=_as_int(threads.get("max_items"), d.threads_max_items, minimum=1),
            threads_max_context_bytes=_as_int(
                threads.get("max_context_bytes"), d.threads_max_context_bytes, minimum=64),
            threads_retention_days=_as_int(
                threads.get("retention_days"), d.threads_retention_days, minimum=1),
            recall_enabled=_as_bool(recall.get("enabled"), d.recall_enabled),
            recall_max_items=_as_int(recall.get("max_items"), d.recall_max_items, minimum=1),
            recall_max_bytes=_as_int(recall.get("max_bytes"), d.recall_max_bytes, minimum=64),
            suggestions_enabled=_as_bool(sugg.get("enabled"), d.suggestions_enabled),
            suggestions_cooldown_seconds=_as_int(
                sugg.get("cooldown_seconds"), d.suggestions_cooldown_seconds),
            suggestions_max_per_session=_as_int(
                sugg.get("max_per_session"), d.suggestions_max_per_session),
            procedures_enabled=_as_bool(proc.get("enabled"), d.procedures_enabled),
            procedures_min_verified_runs=_as_int(
                proc.get("min_verified_runs"), d.procedures_min_verified_runs, minimum=1),
            procedures_min_sessions=_as_int(
                proc.get("min_sessions"), d.procedures_min_sessions, minimum=1),
            compaction_audit_enabled=_as_bool(audit.get("enabled"), d.compaction_audit_enabled),
            compaction_audit_max_repairs=_as_int(
                audit.get("max_repairs"), d.compaction_audit_max_repairs),
            aux_max_calls_per_event=_as_int(
                aux.get("max_calls_per_event"), d.aux_max_calls_per_event),
            aux_max_calls_per_day=_as_int(aux.get("max_calls_per_day"), d.aux_max_calls_per_day),
            aux_max_output_tokens=_as_int(
                aux.get("max_output_tokens"), d.aux_max_output_tokens),
        )
    except Exception:
        logger.debug("proactive settings unavailable; using disabled defaults", exc_info=True)
        return ProactiveSettings()


def job_eligible(settings: ProactiveSettings, job_id: str) -> bool:
    return bool(settings.enabled and str(job_id) in settings.eligible_jobs)


# ── scope ──────────────────────────────────────────────────────────────────

def current_profile() -> str:
    return str(get_clover_home().resolve())


@dataclass(frozen=True)
class Scope:
    """Isolation key. A row is visible only when all four fields match."""

    profile: str
    project: str
    audience: str
    root: str

    @property
    def private(self) -> bool:
        return self.audience == "private" or self.audience.startswith("private:")


def make_scope(
    project: str, *, audience: str = "private", root: str = "",
    profile: Optional[str] = None,
) -> Scope:
    if not str(project or "").strip():
        raise ValueError("project must be non-empty")
    if not str(audience or "").strip():
        raise ValueError("audience must be non-empty")
    return Scope(
        profile=str(profile) if profile else current_profile(),
        project=str(project).strip(),
        audience=str(audience).strip(),
        root=str(root or ""),
    )


def scope_for_surface(
    *, platform: str = "", chat_type: str = "", chat_id: str = "",
    user_id: str = "", project: Optional[str] = None,
) -> Scope:
    """The one canonical Scope for a host surface (turn, end-of-turn, CLI).

    Project identity is the terminal working directory (the gateway bridges
    ``terminal.cwd`` through ``TERMINAL_CWD``). A local CLI/TUI session is the
    private ``local`` root; a DM with a user id is ``private:<user>``; groups,
    channels, DMs without an identity and non-chat transports fail closed to a
    ``shared:`` audience (which ``private_only`` never records or injects).
    """
    plat = str(platform or "").lower()
    ctype = str(chat_type or "").lower()
    cid, uid = str(chat_id or ""), str(user_id or "")
    if plat in {"cli", "tui"} and not ctype:
        audience, root = "private", "local"
    elif ctype in {"dm", "private"} and uid:
        audience, root = f"private:{uid}", uid
    elif ctype in {"group", "forum", "channel", "thread"}:
        audience, root = f"shared:{ctype}:{cid}", cid
    else:
        audience, root = f"shared:{plat or 'unknown'}:{cid}", cid
    proj = project or str(Path(os.getenv("TERMINAL_CWD") or os.getcwd()).resolve())
    return make_scope(proj, audience=audience, root=root)


def scope_for_agent(agent: Any) -> Scope:
    """``scope_for_surface`` from an agent instance's transport attributes."""
    return scope_for_surface(
        platform=getattr(agent, "platform", ""),
        chat_type=getattr(agent, "_chat_type", ""),
        chat_id=getattr(agent, "_chat_id", ""),
        user_id=getattr(agent, "_user_id", ""),
    )


class LinkError(ValueError):
    """A monitor-job link request failed validation (nothing was written)."""


def job_scope(job: Mapping[str, Any]) -> Scope:
    """The Scope a cron job's linked workstream lives in.

    Mirrors the scheduler's occurrence writer: project is the job's workdir
    (falling back to ``TERMINAL_CWD``/cwd), audience ``private``, and root is
    ``job['workstream_root']`` (empty by default).
    """
    project = str(Path(job.get("workdir") or os.getenv("TERMINAL_CWD") or os.getcwd()).resolve())
    return make_scope(project, audience="private", root=str(job.get("workstream_root") or ""))


def link_job_to_workstream(
    scope: Scope, workstream_id: str, job_id: str, settings: "ProactiveSettings",
) -> Dict[str, Any]:
    """Validated writer of ``job['workstream_id']`` for a monitor job.

    The scheduler only records occurrences for an enrolled job whose linked
    record is approved and open in the job's own scope, so exactly that is
    verified here. Evidence only: linking grants no authority and starts no
    work. Raises ``LinkError`` (and writes nothing) on any mismatch.
    """
    from cron import jobs as cron_jobs
    from cron import notepad as np

    if not settings.enabled:
        raise LinkError("proactive is disabled")
    if not job_eligible(settings, job_id):
        raise LinkError(f"job {job_id!r} is not in proactive.eligible_jobs")
    job = cron_jobs.get_job(job_id)
    if not job:
        raise LinkError(f"no cron job {job_id!r}")
    if not (job.get("monitor_script") or job.get("monitor_url")):
        raise LinkError("only monitor jobs (monitor_script/monitor_url) can be linked")
    if not job.get("workdir"):
        raise LinkError("set the job's workdir to the project first; "
                        "the project must not depend on the scheduler's cwd")
    expected = job_scope({**job, "workstream_root": scope.root})
    if scope != expected:
        raise LinkError(
            f"scope mismatch: job resolves to project={expected.project!r} "
            f"audience={expected.audience!r} root={expected.root!r}"
        )
    record = np.get_workstream(scope, workstream_id)
    if record is None:
        raise LinkError(f"no workstream {workstream_id!r} in this scope")
    if not record.get("approved"):
        raise LinkError("workstream is not approved (approve it first)")
    if record.get("status") not in ("open", "deferred"):
        raise LinkError(f"workstream is {record.get('status')}, not open")
    current = job.get("workstream_id")
    if current and current != workstream_id:
        raise LinkError(f"job already linked to {current!r}; unlink it first")
    if current == workstream_id and str(job.get("workstream_root") or "") == scope.root:
        return job
    updated = cron_jobs.update_job(
        job_id, {"workstream_id": workstream_id, "workstream_root": scope.root}
    )
    if not updated or updated.get("workstream_id") != workstream_id:
        raise LinkError("job update was not persisted")
    return updated


def unlink_job(job_id: str) -> Dict[str, Any]:
    """Clear a job's workstream link (never touches the workstream record)."""
    from cron import jobs as cron_jobs

    job = cron_jobs.get_job(job_id)
    if not job:
        raise LinkError(f"no cron job {job_id!r}")
    updated = cron_jobs.update_job(job_id, {"workstream_id": None, "workstream_root": None})
    if not updated:
        raise LinkError("job update was not persisted")
    return updated


def _usable(scope: Scope, settings: ProactiveSettings) -> bool:
    """Feature on and scope allowed. The single gate every facade call uses."""
    if not settings.enabled:
        return False
    if settings.private_only and not scope.private:
        return False
    return True


# ── text helpers ───────────────────────────────────────────────────────────

def _redact(text: str) -> str:
    """Strip secrets; literal paths/ids/hashes are left intact."""
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(str(text), force=True, redact_url_credentials=True)
    except Exception:
        logger.debug("redaction unavailable", exc_info=True)
        return "[unredacted text withheld]"


def _clip(text: str, max_bytes: int) -> str:
    raw = str(text).encode("utf-8")
    if len(raw) <= max_bytes:
        return str(text)
    marker = "…".encode("utf-8")
    return raw[: max(0, max_bytes - len(marker))].decode("utf-8", "ignore") + "…"


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


def _fp(prefix: str, text: str) -> str:
    return prefix + ":" + hashlib.sha1(_norm(text).encode("utf-8")).hexdigest()[:20]


_STOP = frozenset(
    "the and for you can are was were with this that not but have has why how "
    "what when will would should could please into from your our any its it's "
    "does did can't there then than them they their about just also".split()
)
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")


def _tokens(text: str) -> frozenset:
    out = set()
    for word in re.findall(r"\w+", _norm(text)):
        if _CJK.search(word):
            run = "".join(ch for ch in word if _CJK.match(ch))
            if len(run) == 1:
                out.add(run)
            out.update(run[i:i + 2] for i in range(len(run) - 1))
            continue
        if len(word) >= 3 and word not in _STOP:
            out.add(word)
    return frozenset(out)


def relevance(query: str, text: str) -> float:
    """Deterministic lexical relevance 0..1 (overlap coefficient).

    Needs two shared tokens unless one side has a single token. Paraphrase
    quality is *not* claimed: a measured gate is required before relying on
    anything beyond lexical overlap.
    """
    q, t = _tokens(query), _tokens(text)
    if not q or not t:
        return 0.0
    shared = len(q & t)
    smaller = min(len(q), len(t))
    if shared == 0 or (smaller > 1 and shared < 2):
        return 0.0
    return shared / smaller


# ── typed owner receipts ───────────────────────────────────────────────────

@dataclass(frozen=True)
class ExecutedProof:
    job_id: str
    execution_id: str


@dataclass(frozen=True)
class DelegationProof:
    delegation_id: str


@dataclass(frozen=True)
class SavedProof:
    path: str
    sha256: str


@dataclass(frozen=True)
class DeliveredProof:
    delegation_id: str


@dataclass(frozen=True)
class SchedulerProof:
    job_id: str
    occurrence_key: str


@dataclass(frozen=True)
class PolicyProof:
    decision_id: str


def _default_execution_reader(job_id: str, execution_id: str) -> Optional[Mapping[str, Any]]:
    import cron.executions as ex

    for row in ex.list_executions(job_id=job_id, limit=500):
        if row.get("id") == execution_id:
            return row
    return None


def _default_delegation_reader(delegation_id: str) -> Optional[Mapping[str, Any]]:
    from tools.async_delegation import get_durable_delegation

    return get_durable_delegation(delegation_id)


def _default_file_reader(path: str) -> Optional[str]:
    target = Path(path)
    if not target.is_file():
        return None
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _default_occurrence_reader(scope: Scope, workstream_id: str, key: str) -> bool:
    from cron import notepad as np

    rows = np.list_occurrences(scope, workstream_id)
    return any(r.get("occurrence_key") == key for r in rows)


def _default_consumed_reader(delegation_id: str) -> bool:
    """A consumer claimed (or the delivery owner delivered) the finished result
    -- the lifecycle's own rule for ``verified_completion``."""
    from tools.async_delegation import owned_receipts_ready

    return str(delegation_id) in set(owned_receipts_ready([delegation_id]))


def _default_session_root_reader(session_id: str) -> Optional[str]:
    """Conversation root of an *existing* SessionDB session, else ``None``.

    Compression rotates a conversation across session ids linked by
    ``parent_session_id``; the root is the single authentic identity."""
    from clover_state import SessionDB

    if not str(session_id or ""):
        return None
    db = SessionDB(read_only=True)
    try:
        if db.get_session(session_id) is None:
            return None
        return db.get_conversation_root(session_id) or None
    finally:
        db.close()


@dataclass(frozen=True)
class OwnerReaders:
    """The owners' read paths. Overridable only to simulate an owner's state."""

    execution: Callable[[str, str], Optional[Mapping[str, Any]]] = _default_execution_reader
    delegation: Callable[[str], Optional[Mapping[str, Any]]] = _default_delegation_reader
    file_sha256: Callable[[str], Optional[str]] = _default_file_reader
    occurrence: Callable[[Scope, str, str], bool] = _default_occurrence_reader
    consumed: Callable[[str], bool] = _default_consumed_reader
    session_root: Callable[[str], Optional[str]] = _default_session_root_reader


def _delegation_clean(row: Optional[Mapping[str, Any]]) -> bool:
    if not row or row.get("state") != "completed":
        return False
    from agent.work_lifecycle import OK, classify_result

    result = row.get("result")
    return classify_result(result) == OK if isinstance(result, dict) else True


def _verify(scope: Scope, workstream_id: str, kind: str, proof: Any,
            readers: OwnerReaders) -> Optional[Tuple[str, str]]:
    """``(owner, source_id)`` when the owning subsystem confirms ``proof``."""
    try:
        if kind == "executed":
            if isinstance(proof, ExecutedProof):
                row = readers.execution(proof.job_id, proof.execution_id)
                if row and row.get("status") == "completed" \
                        and str(row.get("job_id")) == proof.job_id:
                    return "execution", proof.execution_id
            elif isinstance(proof, DelegationProof):
                if _delegation_clean(readers.delegation(proof.delegation_id)):
                    return "execution", proof.delegation_id
        elif kind == "saved" and isinstance(proof, SavedProof):
            actual = readers.file_sha256(proof.path)
            if actual and actual == proof.sha256:
                return "persistence", f"{proof.path}@{actual[:16]}"
        elif kind == "delivered" and isinstance(proof, DeliveredProof):
            row = readers.delegation(proof.delegation_id)
            if row and row.get("delivery_state") == "delivered":
                return "delivery", proof.delegation_id
        elif kind == "scheduled" and isinstance(proof, SchedulerProof):
            if readers.occurrence(scope, workstream_id, proof.occurrence_key):
                return "scheduler", f"{proof.job_id}:{proof.occurrence_key}"
        elif kind == "authorized" and isinstance(proof, DelegationProof):
            if readers.delegation(proof.delegation_id) is not None:
                return "continuation", proof.delegation_id
        elif kind in ("proposed", "deferred", "cancelled", "unknown"):
            if isinstance(proof, PolicyProof) and proof.decision_id:
                return "policy", proof.decision_id
            if isinstance(proof, SchedulerProof) and readers.occurrence(
                    scope, workstream_id, proof.occurrence_key):
                return "scheduler", f"{proof.job_id}:{proof.occurrence_key}"
    except Exception:
        logger.debug("owner proof check failed for %s", kind, exc_info=True)
    return None


def record_receipt(
    scope: Scope, workstream_id: str, *, kind: str, proof: Any,
    detail: str = "", now: Optional[datetime] = None,
    settings: Optional[ProactiveSettings] = None,
    readers: Optional[OwnerReaders] = None,
) -> Optional[Dict[str, Any]]:
    """Write one factual receipt after its owner confirms the typed proof."""
    settings = settings or load_settings()
    if not _usable(scope, settings) or kind not in RECEIPT_KINDS:
        return None
    confirmed = _verify(scope, workstream_id, kind, proof, readers or OwnerReaders())
    if confirmed is None:
        return None
    from cron import notepad as np

    owner, source_id = confirmed
    return np.add_receipt(scope, workstream_id, kind=kind, owner=owner,
                          source_id=source_id, detail=detail, now=now)


def outcome_label(receipts: Sequence[Mapping[str, Any]]) -> str:
    """Plain user-facing state from independent receipts. Executed, saved and
    delivered are separate facts; none implies another."""
    kinds = {str(r.get("kind")) for r in receipts}
    if "delivered" in kinds:
        return "delivered"
    if "unknown" in kinds:
        return "delivery unknown" if kinds & {"saved", "executed"} else "unknown"
    if "cancelled" in kinds:
        return "failed"
    if "saved" in kinds:
        return "saved locally, not delivered"
    if "executed" in kinds:
        return "draft ready"
    return "waiting"


# ── typed outcomes → open obligations; corrections ────────────────────────

@dataclass(frozen=True)
class TypedOutcome:
    deliverable: str
    status: str
    source_kind: str
    source_id: str
    summary: str = ""
    session_id: str = ""


_EVIDENCE_FOR = {
    "failed": "failed", "unverified": "unverified", "not_run": "unverified",
    "stopped": "unknown", "verified": "verified",
}


def record_outcomes(
    scope: Scope, outcomes: Sequence[TypedOutcome], settings: ProactiveSettings,
    *, now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Turn typed outcomes into open obligations / resolving evidence.

    Returns the obligation rows created (or found) for unfinished work.
    """
    if not _usable(scope, settings) or not outcomes:
        return []
    from cron import notepad as np

    moment = _now(now)
    result: List[Dict[str, Any]] = []
    for o in outcomes:
        if o.status not in OUTCOME_STATUSES or not o.deliverable.strip():
            continue
        fp = _fp("obligation", o.deliverable)
        evidence = dict(
            source_kind=o.source_kind, source_id=o.source_id,
            status=_EVIDENCE_FOR[o.status], session_id=o.session_id,
            summary=_redact(o.summary or f"{o.deliverable}: {o.status}"),
            key=_norm(o.deliverable), event_at=moment.isoformat(), now=moment,
        )
        try:
            if o.status == "verified":
                open_rows = [
                    r for r in np.list_workstreams(scope, statuses=["open", "deferred"],
                                                   kinds=["obligation"], limit=500)
                    if r["fingerprint"] == fp
                ]
                if open_rows:
                    row = open_rows[0]
                    np.add_evidence(scope, row["workstream_id"], **evidence)
                    np.update_workstream(scope, row["workstream_id"],
                                         expected_revision=row["revision"],
                                         status="resolved", actor="runtime", now=moment)
                else:
                    row = np.create_workstream(
                        scope, kind="progress", title=f"Done: {o.deliverable}",
                        summary=_clip(_redact(o.summary or f"{o.deliverable} verified"), 512),
                        fingerprint=_fp("progress", f"{o.deliverable}|{o.source_id}"),
                        session_id=o.session_id, source="runtime",
                        max_items=settings.threads_max_items, now=moment,
                    )
                    np.add_evidence(scope, row["workstream_id"], **evidence)
                    np.update_workstream(scope, row["workstream_id"],
                                         expected_revision=row["revision"],
                                         status="resolved", actor="runtime", now=moment)
                continue
            row = np.create_workstream(
                scope, kind="obligation", title=f"Unfinished: {o.deliverable}",
                summary=_clip(_redact(o.summary or f"{o.deliverable}: {o.status}"), 512),
                fingerprint=fp, session_id=o.session_id, source="runtime",
                max_items=settings.threads_max_items, now=moment,
            )
            np.add_evidence(scope, row["workstream_id"], **evidence)
            result.append(row)
        except np.WorkstreamCapError:
            logger.debug("workstream cap reached; outcome %r not recorded", o.deliverable)
        except np.StaleRevision:
            logger.debug("concurrent update on %r; outcome left for next record", o.deliverable)
    return result


@dataclass(frozen=True)
class HumanOrigin:
    """Trusted human-input receipt as the central SessionDB owner must expose
    it (TRUSTED-HUMAN-ORIGIN-INTERFACE.md): exact profile, session, message
    row and ingress event, ``origin == "human"``, and consumed state."""

    profile: str
    session_id: str
    message_row_id: Any
    ingress_event_id: str
    origin: str
    consumed: bool = False


HumanOriginReader = Callable[[Any, str, Any], Optional[HumanOrigin]]

# The owner's read accessor. ``None`` until the central Luna owner commits and
# publishes it: every consumer then fails closed. Never infer human origin
# from ``role == "user"``, a TTY/PTY, a text marker or a caller-supplied id.
_HUMAN_ORIGIN_READER: Optional[HumanOriginReader] = None


def verified_human_origin(
    origin: Any, *, db: Any = None, human_reader: Optional[HumanOriginReader] = None,
) -> Optional[HumanOrigin]:
    """``origin`` only when the owner's own read-back of (session, row) equals
    it, is ``human``, unconsumed, and belongs to this profile."""
    reader = human_reader or _HUMAN_ORIGIN_READER
    if reader is None or not isinstance(origin, HumanOrigin):
        return None
    try:
        owner = reader(db, origin.session_id, origin.message_row_id)
    except Exception:
        logger.debug("human-origin read-back failed closed", exc_info=True)
        return None
    if (
        not isinstance(owner, HumanOrigin)
        or owner != origin
        or owner.origin != "human"
        or owner.consumed
        or not owner.ingress_event_id
        or owner.profile != current_profile()
    ):
        return None
    return owner


def record_correction(
    scope: Scope, *, text: str, settings: ProactiveSettings,
    origin: Optional[HumanOrigin] = None, human_reader: Optional[HumanOriginReader] = None,
    db: Any = None, supersedes_id: Optional[str] = None,
    expected_revision: Optional[int] = None, session_id: str = "",
    approved: bool = False, now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Record a user correction backed by a verified human-origin receipt;
    supersede the stale fact with provenance. Without a receipt the owner
    confirms, nothing is recorded."""
    if not _usable(scope, settings) or not str(text or "").strip():
        return None
    owner = verified_human_origin(origin, db=db, human_reader=human_reader)
    if owner is None:
        return None
    from cron import notepad as np

    session_id = session_id or owner.session_id
    source = f"human:{owner.ingress_event_id}"
    moment = _now(now)
    clean = _clip(_redact(str(text).strip()), np.MAX_WORKSTREAM_SUMMARY_BYTES)
    title = "Correction: " + clean.splitlines()[0][:100]
    fp = _fp("correction", clean)
    if supersedes_id:
        current = np.get_workstream(scope, supersedes_id)
        if current is None:
            raise np.NotFound(f"no workstream {supersedes_id!r} in this scope")
        revision = current["revision"] if expected_revision is None else int(expected_revision)
        row = np.supersede_workstream(
            scope, supersedes_id, expected_revision=revision, kind="correction",
            title=title, summary=clean, fingerprint=fp, actor=source,
            session_id=session_id, now=moment,
        )
        if approved:
            row = np.update_workstream(scope, row["workstream_id"],
                                       expected_revision=row["revision"],
                                       approved=True, actor=source, now=moment)
        return row
    return np.create_workstream(
        scope, kind="correction", title=title, summary=clean, fingerprint=fp,
        session_id=session_id, approved=approved, source=source,
        max_items=settings.threads_max_items, now=moment,
    )


def _verified_delegation_run(
    delegation_id: str, session_id: str, readers: OwnerReaders,
) -> Optional[str]:
    """Conversation root when the delegation ledger proves a clean completed
    result *that a consumer claimed* and ``session_id`` is a real session;
    otherwise ``None`` (unknown / failed / unclaimed / unauthentic)."""
    try:
        row = readers.delegation(delegation_id)
        if not _delegation_clean(row) or not readers.consumed(delegation_id):
            return None
        return readers.session_root(session_id) or None
    except Exception:
        logger.debug("verified-run check failed closed", exc_info=True)
        return None


def procedure_key_for_goal(goal: str) -> str:
    """Stable workflow key for an identical (normalized) delegated goal."""
    norm = _norm(goal)
    slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")[:40].strip("-") or "task"
    return f"{slug}-{hashlib.sha1(norm.encode('utf-8')).hexdigest()[:8]}"


def record_verified_run(
    scope: Scope, workstream_id: str, *, procedure_key: str, proof: Any,
    session_id: str, steps: str, settings: ProactiveSettings,
    now: Optional[datetime] = None, readers: Optional[OwnerReaders] = None,
) -> Optional[Dict[str, Any]]:
    """Record one *verified successful* execution of a reusable workflow.

    Verified means the durable delegation ledger holds a clean completed
    result that a consumer claimed (``DelegationProof`` only: a cron executions
    row is transport status, not a work result) **and** ``session_id`` is a
    real SessionDB session, identified by its conversation root. Anything
    failed, unknown, unclaimed, replayed under another id or unauthentic
    records nothing. The ``executed`` (and, when the delivery owner says so,
    ``delivered``) receipts come from the owning ledgers; the evidence row is
    marked ``trust="owner_session"``, which procedure intake requires.
    """
    if not _usable(scope, settings) or not str(procedure_key or "").strip():
        return None
    if not isinstance(proof, DelegationProof):
        return None
    readers = readers or OwnerReaders()
    root = _verified_delegation_run(proof.delegation_id, session_id, readers)
    if root is None:
        return None
    receipt = record_receipt(scope, workstream_id, kind="executed", proof=proof,
                             settings=settings, now=now, readers=readers)
    if receipt is None:
        return None
    row = readers.delegation(proof.delegation_id) or {}
    if row.get("delivery_state") == "delivered":
        record_receipt(scope, workstream_id, kind="delivered",
                       proof=DeliveredProof(proof.delegation_id), settings=settings,
                       now=now, readers=readers)
    from cron import notepad as np

    moment = _now(now)
    return np.add_evidence(
        scope, workstream_id, source_kind="delegation", source_id=receipt["source_id"],
        status="verified", key="procedure_run", procedure_key=str(procedure_key).strip(),
        session_id=root, summary=_clip(_redact(steps or ""), 512),
        event_at=moment.isoformat(), trust="owner_session", now=moment,
    )


def record_delegation_run(
    scope: Scope, *, delegation_id: str, goal: str, session_id: str,
    settings: ProactiveSettings, now: Optional[datetime] = None,
    readers: Optional[OwnerReaders] = None,
) -> Optional[Dict[str, Any]]:
    """Production producer: one finished single-task delegation -> one run of
    the workflow keyed by its goal. Verifies before writing anything, so an
    unverified outcome creates no workstream and no row."""
    if not (_usable(scope, settings) and settings.procedures_enabled):
        return None
    goal = str(goal or "").strip()
    if len(goal) < 8:
        return None
    readers = readers or OwnerReaders()
    if _verified_delegation_run(delegation_id, session_id, readers) is None:
        return None
    from cron import notepad as np

    row = np.create_workstream(
        scope, kind="progress", title="Verified delegated runs",
        summary="runs of delegated goals verified by the delegation ledger",
        fingerprint="delegation-runs", source="runtime",
        max_items=settings.threads_max_items, now=_now(now),
    )
    return record_verified_run(
        scope, row["workstream_id"], procedure_key=procedure_key_for_goal(goal),
        proof=DelegationProof(delegation_id), session_id=session_id, steps=goal,
        settings=settings, now=now, readers=readers,
    )


def approve_workstream(
    scope: Scope, workstream_id: str, *, settings: ProactiveSettings,
    origin: Optional[HumanOrigin] = None, human_reader: Optional[HumanOriginReader] = None,
    db: Any = None, now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Mark one record as approved for recall (CAS on revision) on a verified
    human-origin receipt. Approval only makes the record's own text eligible
    for bounded recall into the same private scope; it grants no authority."""
    if not _usable(scope, settings):
        return None
    owner = verified_human_origin(origin, db=db, human_reader=human_reader)
    if owner is None:
        return None
    from cron import notepad as np

    row = np.get_workstream(scope, workstream_id)
    if row is None:
        raise np.NotFound(f"no workstream {workstream_id!r} in this scope")
    if row["approved"]:
        return row
    return np.update_workstream(scope, workstream_id, expected_revision=row["revision"],
                                approved=True, actor=f"human:{owner.ingress_event_id}",
                                now=_now(now))


_CORRECTION_MARKER = re.compile(
    r"^\s*(?:/correct(?:ion)?(?:\s*:\s*|\s+)|correction\s*:\s*)(\S.*?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_CORRECTION_TARGET = re.compile(r"^(ws_[0-9a-f]{16})\s*:?\s+(\S.*)$", re.DOTALL)


def parse_explicit_correction(message: Any) -> Optional[Tuple[Optional[str], str]]:
    """``(target_workstream_id | None, text)`` of an explicit ``/correct`` or
    ``Correction:`` message, else ``None``. Anchored at the start: nothing is
    ever inferred from prose, tone or silence. A target id counts only when
    written as the first word (``/correct ws_<16 hex>: ...``)."""
    if not isinstance(message, str):
        return None
    match = _CORRECTION_MARKER.match(message)
    if not match:
        return None
    body = match.group(1)
    target = _CORRECTION_TARGET.match(body)
    if target:
        return target.group(1), target.group(2).strip()
    return None, body


def explicit_correction_from_message(message: Any) -> Optional[str]:
    parsed = parse_explicit_correction(message)
    return parsed[1] if parsed else None


def latest_user_row(db: Any, session_id: str) -> Optional[Dict[str, Any]]:
    """Newest persisted ``user`` row of an existing SessionDB session (never a
    tool-result or assistant row), else ``None``. A database role is *not*
    human origin; see ``verified_human_origin``."""
    if not session_id or db is None or db.get_session(session_id) is None:
        return None
    for row in reversed(db.get_messages(session_id, limit=200, latest=True)):
        if row.get("role") == "user" and not row.get("tool_call_id"):
            return row if isinstance(row.get("content"), str) else None
    return None


def latest_user_message(db: Any, session_id: str) -> Optional[str]:
    row = latest_user_row(db, session_id)
    return row["content"] if row else None


def capture_user_correction(
    scope: Scope, *, session_id: str, settings: ProactiveSettings,
    db: Any = None, human_reader: Optional[HumanOriginReader] = None,
    expected_revision: Optional[int] = None, now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Persist an explicit user correction found in the real conversation.

    Three independent owner facts are required: the SessionDB's newest
    persisted ``user`` row of ``session_id`` begins with the explicit marker;
    that exact row (profile/session/row id) has an unconsumed owner-issued
    ``human`` ingress receipt read back through the trusted API; and the row is
    current. A database role, TTY, assistant/tool/model text or caller-supplied
    id is never evidence. With the trusted API unpublished
    (``_HUMAN_ORIGIN_READER is None``) this records nothing. Result: redacted,
    literal ids kept, superseding the explicitly named record with history,
    idempotent. An unknown named target raises ``NotFound``.
    """
    if not _usable(scope, settings) or not str(session_id or ""):
        return None
    if (human_reader or _HUMAN_ORIGIN_READER) is None:
        return None
    if db is None:
        from clover_state import SessionDB

        try:
            db = SessionDB(read_only=True)
        except Exception:
            logger.debug("session store unavailable; correction not recorded", exc_info=True)
            return None
        try:
            return capture_user_correction(
                scope, session_id=session_id, settings=settings, db=db,
                human_reader=human_reader, expected_revision=expected_revision, now=now)
        finally:
            db.close()
    row = latest_user_row(db, session_id)
    if row is None:
        return None
    parsed = parse_explicit_correction(row["content"])
    if parsed is None:
        return None
    reader = human_reader or _HUMAN_ORIGIN_READER
    try:
        issued = reader(db, session_id, row["id"])
    except Exception:
        logger.debug("human-origin read failed closed", exc_info=True)
        return None
    if issued is None or issued.session_id != session_id or issued.message_row_id != row["id"]:
        return None
    target, text = parsed
    return record_correction(
        scope, text=text, settings=settings, origin=issued, human_reader=human_reader,
        db=db, supersedes_id=target, expected_revision=expected_revision,
        session_id=session_id, approved=True, now=now,
    )


# ── feedback, timing, novelty ──────────────────────────────────────────────

def record_feedback(
    scope: Scope, workstream_id: str, *, kind: str, settings: ProactiveSettings,
    origin: Optional[HumanOrigin] = None, human_reader: Optional[HumanOriginReader] = None,
    db: Any = None, actor: str = "user", note: str = "",
    defer_until: Optional[str] = None, now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Explicit user feedback backed by a verified human-origin receipt.

    ``actor`` and a TTY prove nothing; with no owner-confirmed receipt
    nothing is written. Silence is never recorded. One ingress event yields at
    most one feedback row per kind (replay returns the original)."""
    if not _usable(scope, settings) or actor != "user" or kind not in FEEDBACK_KINDS:
        return None
    owner = verified_human_origin(origin, db=db, human_reader=human_reader)
    if owner is None:
        return None
    from cron import notepad as np

    receipt_kind = f"feedback_{kind}"
    if receipt_kind in RECEIPT_KINDS:
        for r in np.list_receipts(scope, workstream_id, kinds=[receipt_kind]):
            if r["source_id"] == owner.ingress_event_id:
                prior = [f for f in np.list_feedback(scope, workstream_id)
                         if f["feedback_id"] == r["detail"]]
                if prior:
                    return prior[0]
    moment = _now(now)
    until = defer_until
    if kind == "defer" and not until:
        until = (moment + timedelta(seconds=settings.suggestions_cooldown_seconds)).isoformat()
    row = np.add_feedback(scope, workstream_id, kind=kind, actor="user",
                          note=_redact(note), defer_until=until, now=moment)
    if receipt_kind in RECEIPT_KINDS:
        np.add_receipt(scope, workstream_id, kind=receipt_kind, owner="human_ingress",
                       source_id=owner.ingress_event_id, detail=row["feedback_id"], now=moment)
    return row


def feedback_state(scope: Scope, workstream_id: str) -> str:
    """Latest explicit feedback kind, or ``"unknown"`` (silence is unknown)."""
    from cron import notepad as np

    rows = np.list_feedback(scope, workstream_id)
    return rows[-1]["kind"] if rows else "unknown"


@dataclass(frozen=True)
class Candidate:
    workstream_id: str
    text: str
    relevance: float
    unsolicited: bool = True
    fingerprint: str = ""

    @property
    def fp(self) -> str:
        return self.fingerprint or _fp("offer", self.text)


@dataclass(frozen=True)
class GateDecision:
    action: str          # "offer" | "defer" | "suppress"
    reason: str
    surface: bool
    until: Optional[str] = None


def _decision(action: str, reason: str, until: Optional[str] = None) -> GateDecision:
    return GateDecision(action, reason, False, until)


def evaluate_suggestion(
    candidate: Candidate, scope: Scope, settings: ProactiveSettings, *,
    session_id: str = "", query_scope: Optional[Scope] = None,
    user_timing_ok: Optional[bool] = None, offered_this_session: int = 0,
    now: Optional[datetime] = None,
) -> GateDecision:
    """Deterministic usefulness/timing/novelty gate. Can only offer, defer or
    suppress; an offer needs ``settings.active`` to be surfaced at all."""
    moment = _now(now)
    if not settings.enabled or not settings.suggestions_enabled:
        return _decision("suppress", "disabled")
    if (query_scope is not None and query_scope != scope) or (
            settings.private_only and not scope.private):
        return _decision("suppress", "scope")

    from cron import notepad as np

    if candidate.workstream_id:
        fb = np.list_feedback(scope, candidate.workstream_id)
        latest = fb[-1] if fb else None
        if latest is not None:
            if latest["kind"] == "stop":
                return _decision("suppress", "stopped")
            if latest["kind"] == "done":
                return _decision("suppress", "done")
            if latest["kind"] == "reject":
                return _decision("suppress", "rejected")
            if latest["kind"] == "defer":
                until = _parse(latest.get("defer_until"))
                if until is not None and moment < until:
                    return _decision("defer", "cooldown", until.isoformat())
    if candidate.relevance < MIN_RELEVANCE:
        return _decision("suppress", "irrelevant")

    proposed = np.list_receipts(scope, None, kinds=["proposed"])
    offers = [r for r in proposed if str(r["source_id"]).startswith("offer:")]
    if any(r["source_id"] == "offer:" + candidate.fp for r in offers):
        return _decision("suppress", "duplicate")

    cooldown = timedelta(seconds=settings.suggestions_cooldown_seconds)
    anchors = [_parse(r["created_at"]) for r in offers]
    anchors += [_parse(f["created_at"]) for f in np.list_feedback(scope, None)
                if f["kind"] == "defer"]
    anchors = [a for a in anchors if a is not None]
    if anchors and moment < max(anchors) + cooldown:
        return _decision("defer", "cooldown", (max(anchors) + cooldown).isoformat())
    if offered_this_session >= settings.suggestions_max_per_session:
        return _decision("defer", "session_cap")
    if candidate.unsolicited:
        if user_timing_ok is None:
            return _decision("defer", "no_timing_preference")
        if user_timing_ok is False:
            return _decision("defer", "timing")
    if not settings.active:
        return _decision("offer", "shadow")
    return GateDecision("offer", "ok", True)


def apply_judge(decision: GateDecision, judge_verdict: Optional[str]) -> GateDecision:
    """An optional judge may only make a decision more conservative."""
    if decision.action != "offer":
        return decision
    verdict = str(judge_verdict or "").strip().lower()
    if verdict == "suppress":
        return _decision("suppress", "judge")
    if verdict == "defer":
        return _decision("defer", "judge")
    return decision


def record_offer(
    scope: Scope, workstream_id: str, candidate: Candidate,
    settings: ProactiveSettings, *, session_id: str = "",
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Note that a suggestion was actually surfaced (active mode only)."""
    if not _usable(scope, settings) or not settings.suggestions_enabled \
            or not settings.active:
        return None
    from cron import notepad as np

    return np.add_receipt(
        scope, workstream_id, kind="proposed", owner="policy",
        source_id="offer:" + candidate.fp,
        detail=f"session={session_id}", now=_now(now),
    )


def score_gate(
    fixtures: Sequence[Tuple[str, GateDecision]],
) -> Dict[str, Dict[str, int]]:
    """Per-label hit counts. Categories are reported separately, never blended."""
    correct = {
        "useful_now": lambda d: d.action == "offer",
        "missed_need": lambda d: d.action == "offer",
        "wrong_time": lambda d: d.action != "offer",
        "duplicate": lambda d: d.action == "suppress" and d.reason == "duplicate",
        "no_help": lambda d: d.action != "offer",
    }
    out = {label: {"n": 0, "correct": 0} for label in correct}
    for label, decision in fixtures:
        if label not in correct:
            raise ValueError(f"unknown fixture label: {label!r}")
        out[label]["n"] += 1
        out[label]["correct"] += 1 if correct[label](decision) else 0
    return out


# ── evidence sufficiency ───────────────────────────────────────────────────

@dataclass(frozen=True)
class EvidencePlan:
    kind: str
    missing: Tuple[str, ...]
    sources: Tuple[str, ...]


@dataclass(frozen=True)
class EvidenceAssessment:
    status: str
    detail: str
    cited: Tuple[str, ...]


_PAST = re.compile(
    r"\b(did|was|were|had|has|have|passed|ran|yesterday|earlier|before|last|ago|"
    r"previously|decided|said)\b", re.I)
_LIVE = re.compile(
    r"\b(now|currently|right now|at the moment|still|is|are|status|running|"
    r"latest|today)\b", re.I)


def classify_question(question: str) -> str:
    """``live`` (current state; needs a fresh lookup) or ``historical``."""
    if _PAST.search(question):
        return "historical"
    return "live" if _LIVE.search(question) else "historical"


def _anchored(row: Mapping[str, Any]) -> bool:
    return bool(row.get("source_id")) and bool(row.get("event_at"))


def _by_key(
    kind: str, required: Sequence[str], evidence: Sequence[Mapping[str, Any]],
    moment: datetime, ttl: int,
) -> Tuple[Dict[str, List[Mapping[str, Any]]], List[str]]:
    usable: Dict[str, List[Mapping[str, Any]]] = {k: [] for k in required}
    notes: List[str] = []
    for row in evidence:
        key = str(row.get("key") or "")
        if key not in usable or not _anchored(row):
            continue
        if kind == "live":
            if row.get("source_kind") not in _LIVE_SOURCE_KINDS:
                continue
            seen = _parse(row.get("event_at"))
            if seen is None or (moment - seen).total_seconds() > ttl:
                notes.append(f"stale live evidence for {key}")
                continue
        usable[key].append(row)
    return usable, notes


_SOURCES = {
    "live": ("process_state", "project_check", "cron_receipt", "delegation_receipt"),
    "historical": ("session_search", "cron_receipt", "delegation_receipt"),
}


def plan_evidence(
    question: str, required_keys: Sequence[str],
    evidence: Sequence[Mapping[str, Any]], *, now: Optional[datetime] = None,
    live_ttl_seconds: int = 300,
) -> EvidencePlan:
    kind = classify_question(question)
    usable, _ = _by_key(kind, list(required_keys), evidence, _now(now), live_ttl_seconds)
    decisive = ("verified", "failed")
    missing = tuple(
        k for k in required_keys
        if not any(r.get("status") in decisive for r in usable[k])
    )
    return EvidencePlan(kind=kind, missing=missing, sources=_SOURCES[kind])


def assess_evidence(
    question: str, required_keys: Sequence[str],
    evidence: Sequence[Mapping[str, Any]], *, now: Optional[datetime] = None,
    live_ttl_seconds: int = 300,
) -> EvidenceAssessment:
    """``satisfied`` only when every required key has anchored, agreeing,
    verified evidence (fresh live evidence for live asks). Anything else is
    explicitly unknown/partial/contradicted/failed — never a guess."""
    kind = classify_question(question)
    keys = list(required_keys)
    if not keys:
        return EvidenceAssessment("unknown", "no evidence keys requested", ())
    usable, notes = _by_key(kind, keys, evidence, _now(now), live_ttl_seconds)
    cited: List[str] = []
    contradicted, failed, missing, good = [], [], [], 0
    for key in keys:
        statuses = {r.get("status") for r in usable[key] if r.get("status") in ("verified", "failed")}
        if statuses == {"verified", "failed"}:
            contradicted.append(key)
        elif statuses == {"failed"}:
            failed.append(key)
        elif statuses == {"verified"}:
            good += 1
        else:
            missing.append(key)
            continue
        cited.extend(f"{r.get('source_kind')}:{r.get('source_id')}" for r in usable[key]
                     if r.get("status") in ("verified", "failed"))
    cites = tuple(dict.fromkeys(cited))
    if contradicted:
        return EvidenceAssessment("contradicted", "conflicting evidence for: "
                                  + ", ".join(contradicted), cites)
    if missing:
        detail = "no usable evidence for: " + ", ".join(missing)
        if notes:
            detail += " (" + "; ".join(dict.fromkeys(notes)) + ")"
        status = "partial" if (good or failed) else "unknown"
        return EvidenceAssessment(status, detail, cites)
    if failed:
        return EvidenceAssessment("failed", "evidence shows failure for: "
                                  + ", ".join(failed), cites)
    return EvidenceAssessment("satisfied", "all required evidence verified", cites)


# ── bounded context selection ──────────────────────────────────────────────

_OPEN_KINDS = ("obligation", "blocker", "correction")
_RECALL_KINDS = ("constraint", "decision", "correction", "obligation", "blocker")


def _cite(row: Mapping[str, Any]) -> str:
    return f"[ws:{row['workstream_id']}@r{row['revision']}]"


def _fit(lines: Sequence[str], header: str, budget: int) -> Tuple[str, int]:
    """Whole lines that fit ``budget`` UTF-8 bytes; the first line is clipped
    rather than dropped so a bounded note is still delivered. Returns
    (text, lines_used)."""
    body = header
    used = 0
    for line in lines:
        candidate = body + line + "\n"
        if len(candidate.encode("utf-8")) <= budget:
            body, used = candidate, used + 1
            continue
        if used == 0:
            room = budget - len(header.encode("utf-8")) - 1
            if room > 16:
                body += _clip(line, room) + "\n"
                used = 1
        break
    return (body.rstrip("\n") if used else ""), used


def _unexpired(rows: Sequence[Dict[str, Any]], moment: datetime) -> List[Dict[str, Any]]:
    """Drop rows whose own ``expires_at`` has passed (prune will mark them)."""
    keep = []
    for row in rows:
        until = _parse(row.get("expires_at"))
        if until is None or moment < until:
            keep.append(row)
    return keep


def thread_context(scope: Scope, settings: ProactiveSettings, *,
                   now: Optional[datetime] = None) -> str:
    """Open obligations/blockers/corrections for one scope, newest first,
    within ``threads_max_context_bytes``. Empty when there is nothing to say."""
    if not _usable(scope, settings):
        return ""
    return _thread_rows_text(scope, settings, _now(now))[0]


def _thread_rows_text(
    scope: Scope, settings: ProactiveSettings, moment: datetime,
) -> Tuple[str, List[Dict[str, Any]]]:
    from cron import notepad as np

    rows = _unexpired(
        np.list_workstreams(scope, statuses=["open", "deferred"],
                            kinds=list(_OPEN_KINDS), limit=settings.threads_max_items),
        moment,
    )
    if not rows:
        return "", []
    lines = [f"- {r['title']}: {r['summary']} {_cite(r)}" for r in rows]
    header = "Open work for this project (durable record):\n"
    text, used = _fit(lines, header, settings.threads_max_context_bytes)
    return text, rows[:used]


@dataclass(frozen=True)
class RecallResult:
    text: str
    items: Tuple[Dict[str, Any], ...]


def recall_context(scope: Scope, query: str, settings: ProactiveSettings, *,
                   now: Optional[datetime] = None,
                   exclude_ids: Sequence[str] = ()) -> RecallResult:
    """Approved, private, lexically relevant constraints/decisions/corrections,
    at most ``recall_max_items`` within ``recall_max_bytes``, cited."""
    empty = RecallResult("", ())
    if not _usable(scope, settings) or not settings.recall_enabled or not str(query or "").strip():
        return empty
    from cron import notepad as np

    rows = _unexpired(
        np.list_workstreams(scope, statuses=["open", "deferred"],
                            kinds=list(_RECALL_KINDS), limit=200),
        _now(now),
    )
    scored = []
    for row in rows:
        if not row.get("approved") or row["workstream_id"] in exclude_ids:
            continue
        score = relevance(query, f"{row['title']} {row['summary']}")
        if score >= MIN_RELEVANCE:
            scored.append((score, row))
    if not scored:
        return empty
    scored.sort(key=lambda sr: (-sr[0], sr[1]["workstream_id"]))
    picked = [r for _, r in scored[: settings.recall_max_items]]
    lines = [f"- {r['summary']} {_cite(r)}" for r in picked]
    header = "Relevant saved decisions/constraints:\n"
    text, used = _fit(lines, header, settings.recall_max_bytes)
    if not used:
        return empty
    return RecallResult(text, tuple(picked[:used]))


def current_turn_note(scope: Scope, query: str, settings: ProactiveSettings, *,
                      now: Optional[datetime] = None) -> str:
    """The one string Luna appends at both composition sites. ``""`` = nothing."""
    if not _usable(scope, settings):
        return ""
    threads, rows = _thread_rows_text(scope, settings, _now(now))
    recalled = recall_context(scope, query, settings, now=now,
                              exclude_ids=[r["workstream_id"] for r in rows])
    return "\n".join(part for part in (threads, recalled.text) if part)
