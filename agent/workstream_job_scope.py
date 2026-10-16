"""Exact authenticated human monitor admission; record approval, never permission.

The installed post-persistence consumer accepts an optional explicit blocker
specification. Existing-record requests keep their original link behavior.
SQLite batches own record creation/approval and durable create intent; the
existing jobs owner owns jobs.json. An ambiguous cross-store effect is unknown,
never a reason to create a second job. No model/tool schema exposes this path.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from agent import workstreams as ws, workstream_human_hooks as hooks
from agent import workstream_origin as origin
from cron import jobs, notepad

OWNER = "workstream_job_scope"
_UNKNOWN = "unknown: monitor persistence not confirmed; inspect local state before retry"


@dataclass(frozen=True)
class AdmissionResult:
    handled: bool = False
    job_id: str = ""
    reason: str = ""


def _request(message, scope):
    data = json.loads(message[len("workstream-monitor: "):], object_pairs_hook=jobs._json_unique_object)
    required = {"scope", "workstream_id", "prompt", "schedule", "monitor_script", "approve"}
    if (not isinstance(data, dict) or not required <= set(data)
            or set(data) - required - {"job_id", "blocker"}):
        raise ValueError("exact monitor request fields required")
    job_id_present = "job_id" in data
    job_id = data.pop("job_id", None)
    # A present null is not an absent job id.
    if job_id_present and (not isinstance(job_id, str) or not job_id or job_id != job_id.strip()):
        raise ValueError("literal job id required")
    if data["scope"] != {k: getattr(scope, k) for k in ("profile", "project", "audience", "root")}:
        raise ValueError("original accepted scope does not match this human surface")
    if not scope.private or data["approve"] is not True:
        raise ValueError("explicit private blocker approval required")
    for key in ("workstream_id", "prompt", "schedule", "monitor_script"):
        if not isinstance(data[key], str) or not data[key] or data[key] != data[key].strip():
            raise ValueError("non-empty literal monitor request fields required")
    if not re.fullmatch(r"ws_[0-9a-f]{16}", data["workstream_id"]):
        raise ValueError("literal workstream id must be ws_<16 lowercase hex>")
    if "blocker" in data:
        spec = data["blocker"]
        if (not isinstance(spec, dict) or set(spec) != {"title", "summary"}
                or any(not isinstance(spec[k], str) or not spec[k].strip()
                       or spec[k] != spec[k].strip() for k in spec)
                or len(spec["title"]) > notepad.MAX_WORKSTREAM_TITLE_CHARS
                or len(spec["summary"].encode("utf-8")) > notepad.MAX_WORKSTREAM_SUMMARY_BYTES):
            raise ValueError("exact literal blocker title and summary required")
        from agent.redact import redact_sensitive_text
        if any(redact_sensitive_text(spec[k], force=True) != spec[k] for k in spec):
            raise ValueError("blocker text contains sensitive values; submit redacted text")
        # Validate with existing owners before any record/intent write.
        jobs.parse_schedule(data["schedule"])
        jobs._normalize_workdir(scope.project)
    return data, job_id


def _blocker(scope, data, agent, receipt, settings, db, *, create=False):
    """Caller holds write_batch: read/create/approval CAS are indivisible."""
    wid, spec = data["workstream_id"], data.get("blocker")
    row = notepad.get_workstream(scope, wid)
    if row is None and spec is not None and create:
        row = notepad.create_workstream(
            scope, kind="blocker", title=spec["title"], summary=spec["summary"],
            fingerprint="human-blocker:" + wid, workstream_id=wid,
            session_id=agent.session_id, source="human:" + receipt.ingress_event_id,
            max_items=settings.threads_max_items)
    if not row or row["kind"] != "blocker" or row["status"] not in ("open", "deferred"):
        raise ValueError("no open blocker in the accepted scope")
    if spec is not None and (row["session_id"] != agent.session_id
            or row["fingerprint"] != "human-blocker:" + wid
            or any(row[k] != spec[k] for k in ("title", "summary"))):
        raise ValueError("blocker differs from this original owner/specification")
    approved = ws.approve_workstream(scope, wid, settings=settings, origin=receipt,
                                    human_reader=origin.read_human_origin, db=db)
    if not approved or not approved.get("approved"):
        raise ValueError(_UNKNOWN)
    return approved


def _owned_job(job, data, scope, session_id):
    metadata = (job or {}).get("workstream_scope") or {}
    return bool(job and ws.job_scope(job) == scope and metadata.get("request") == data
                and metadata.get("owner", {}).get("session_id") == session_id)


def _create_monitor(agent, message, row_id, scope, data, receipt, settings, db):
    """Reserve once in existing receipts before crossing into jobs.json.

    BEGIN IMMEDIATE + unique receipt key serialize two independent workers.
    A committed intent without confirmed save is unknown even if no job can
    be found: a crashed owner might have crossed the external write boundary.
    """
    wid = data["workstream_id"]
    source_id = "monitor-create:" + wid
    digest = hashlib.sha256(json.dumps([data, agent.session_id], sort_keys=True,
                                     ensure_ascii=True).encode("utf-8")).hexdigest()
    with notepad.write_batch():
        _blocker(scope, data, agent, receipt, settings, db, create=True)
        # Stop may land while the record transaction is open. Roll back the
        # record and approval as well, before committing any create intent.
        if hooks._authenticate(agent, role="user", message=message, session_id=agent.session_id,
                               message_id=None, row_id=row_id) is None:
            raise ValueError("current human invocation changed; nothing was recorded")
        receipts = [r for r in notepad.list_receipts(scope, wid)
                    if r["owner"] == OWNER and r["source_id"] == source_id]
        intent = next((r for r in receipts if r["kind"] == "proposed"), None)
        saved = next((r for r in receipts if r["kind"] == "saved"), None)
        if intent:
            if intent["detail"] != digest:
                raise ValueError("monitor differs from this original owner/request")
            job = jobs.get_job(saved["detail"]) if saved else None
            if (not saved or any(r["kind"] == "unknown" for r in receipts)
                    or not _owned_job(job, data, scope, agent.session_id)):
                raise ValueError(_UNKNOWN)
            return job
        notepad.add_evidence(scope, wid, source_kind="human_message",
                             source_id=receipt.ingress_event_id, status="verified", trust="human",
                             session_id=agent.session_id, summary="Explicit scoped blocker record approval")
        notepad.add_receipt(scope, wid, kind="proposed", owner=OWNER,
                            source_id=source_id, detail=digest)
    # The intent is durable BEFORE the external effect. Do not merge these
    # batches: rollback after a jobs.json save would otherwise erase intent.
    try:
        with notepad.write_batch():
            with jobs._use_workstream_scope_owner(agent, message=message, row_id=row_id):
                job = jobs.create_job(data["prompt"], data["schedule"], deliver="local",
                                      workdir=scope.project, monitor_script=data["monitor_script"])
            persisted = jobs.get_job(job["id"])
            if not _owned_job(persisted, data, scope, agent.session_id):
                raise ValueError(_UNKNOWN)
            if hooks._authenticate(agent, role="user", message=message, session_id=agent.session_id,
                                   message_id=None, row_id=row_id) is None:
                raise ValueError(_UNKNOWN)
            notepad.add_receipt(scope, wid, kind="saved", owner=OWNER,
                                source_id=source_id, detail=job["id"])
        return persisted
    except Exception:
        # A failed reconciliation write cannot erase the already durable intent.
        try:
            notepad.add_receipt(scope, wid, kind="unknown", owner=OWNER, source_id=source_id,
                                detail="Monitor creation requires owner reconciliation; no automatic retry")
        except Exception:
            pass
        raise ValueError(_UNKNOWN) from None


def consume_persisted_turn(agent, *, message, row_id):
    settings = ws.load_settings()
    if not settings.enabled:
        return AdmissionResult()
    if not isinstance(message, str) or not message.startswith("workstream-monitor: "):
        return AdmissionResult()
    authenticated = hooks._authenticate(
        agent, role="user", message=message, session_id=agent.session_id,
        message_id=None, row_id=row_id)
    if authenticated is None:
        return AdmissionResult(reason="not the current authenticated human turn")
    db, row, receipt, ingress = authenticated
    try:
        scope = ws.scope_for_agent(agent)
        data, job_id = _request(message, scope)
        if job_id is not None:
            job = jobs.get_job(job_id)
            if not _owned_job(job, data, scope, agent.session_id):
                raise ValueError("job is not this original scope/request owner")
            if not ws.job_eligible(settings, job_id):
                raise ValueError("job is not independently enrolled")
            with notepad.write_batch():
                blocker = _blocker(scope, data, agent, receipt, settings, db)
                # Unknown creates cannot be promoted by a later link request.
                if data.get("blocker") is not None and any(
                        r["kind"] == "unknown" or (r["kind"] == "proposed" and not any(
                            s["kind"] == "saved" and s["source_id"] == r["source_id"]
                            for s in notepad.list_receipts(scope, blocker["workstream_id"])))
                        for r in notepad.list_receipts(scope, blocker["workstream_id"])
                        if r["owner"] == OWNER):
                    raise ValueError(_UNKNOWN)
                with jobs._use_workstream_scope_owner(agent, message=message, row_id=row_id):
                    linked = ws.link_job_to_workstream(scope, blocker["workstream_id"], job_id, settings,
                                              origin=receipt, human_reader=origin.read_human_origin, db=db)
                persisted = jobs.get_job(job_id)
                if not linked or not persisted or persisted.get("workstream_id") != blocker["workstream_id"]:
                    raise ValueError(_UNKNOWN)
        elif "blocker" in data:
            job = _create_monitor(agent, message, row_id, scope, data, receipt, settings, db)
        else:
            # Legacy existing-record behavior, including multiple monitors.
            with notepad.write_batch():
                _blocker(scope, data, agent, receipt, settings, db)
                with jobs._use_workstream_scope_owner(agent, message=message, row_id=row_id):
                    job = jobs.create_job(data["prompt"], data["schedule"], deliver="local",
                                          workdir=scope.project, monitor_script=data["monitor_script"])
        if not db.consume_human_origin(agent.session_id, row["id"], consumer=OWNER):
            return AdmissionResult(reason="unknown: operation receipt was not consumed; inspect local state before retry")
        action = ("linked locally to blocker evidence; no execution permission granted" if job_id is not None
                  else "created locally; not enrolled or linked; no execution permission granted")
        return AdmissionResult(True, job["id"], f"Monitor job {job['id']} {action}.")
    except (ValueError, notepad.WorkstreamError) as exc:
        return AdmissionResult(reason=str(exc))
    except Exception:
        return AdmissionResult(reason=_UNKNOWN)
