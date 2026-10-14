"""Exact human-admitted monitor scope; data, never continuation permission.

Host hook: consume_persisted_turn at the same post-persistence boundary as
workstream_human_hooks, before the invocation ingress is retired. No model
schema exposes this path. The original message must be a single explicit
workstream-monitor: JSON object naming the complete canonical scope.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from agent import workstreams as ws, workstream_human_hooks as hooks
from agent import workstream_origin as origin
from cron import jobs, notepad


@dataclass(frozen=True)
class AdmissionResult:
    handled: bool = False
    job_id: str = ""
    reason: str = ""


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
        data = json.loads(message[len("workstream-monitor: "):], object_pairs_hook=jobs._json_unique_object)
        required = {"scope", "workstream_id", "prompt", "schedule", "monitor_script", "approve"}
        if not isinstance(data, dict) or set(data) not in (required, required | {"job_id"}):
            raise ValueError("exact monitor request fields required")
        linking = "job_id" in data
        job_id = data.pop("job_id", None)
        if linking and job_id is None:
            raise ValueError("literal job id required")
        scope = ws.scope_for_agent(agent)
        if data["scope"] != {k: getattr(scope, k) for k in ("profile", "project", "audience", "root")}:
            raise ValueError("original accepted scope does not match this human surface")
        if not scope.private or data["approve"] is not True:
            raise ValueError("explicit private blocker approval required")
        for key in ("workstream_id", "prompt", "schedule", "monitor_script"):
            if not isinstance(data[key], str) or not data[key] or data[key] != data[key].strip():
                raise ValueError("non-empty literal monitor request fields required")
        blocker = notepad.get_workstream(scope, data["workstream_id"])
        if not blocker or blocker["kind"] != "blocker" or blocker["status"] not in ("open", "deferred"):
            raise ValueError("no open blocker in the accepted scope")
        if job_id is not None:
            if not isinstance(job_id, str) or not job_id or job_id != job_id.strip():
                raise ValueError("literal job id required")
            job = jobs.get_job(job_id)
            metadata = (job or {}).get("workstream_scope") or {}
            if (not job or ws.job_scope(job) != scope or metadata.get("request") != data
                    or metadata.get("owner", {}).get("session_id") != agent.session_id):
                raise ValueError("job is not this original scope/request owner")
            if not ws.job_eligible(settings, job_id):
                raise ValueError("job is not independently enrolled")
        else:
            with jobs._use_workstream_scope_owner(agent, message=message, row_id=row_id):
                job = jobs.create_job(data["prompt"], data["schedule"], deliver="local",
                                      workdir=scope.project, monitor_script=data["monitor_script"])
        # This approves evidence linkage only, not an executable stage.
        approved = ws.approve_workstream(scope, blocker["workstream_id"], settings=settings,
                             origin=receipt, human_reader=origin.read_human_origin, db=db)
        if not approved or not approved.get("approved"):
            return AdmissionResult(reason="unknown: blocker approval was not confirmed; inspect local state before retry")
        if job_id is not None:
            with jobs._use_workstream_scope_owner(agent, message=message, row_id=row_id):
                linked = ws.link_job_to_workstream(scope, blocker["workstream_id"], job_id, settings,
                                          origin=receipt, human_reader=origin.read_human_origin, db=db)
            persisted = jobs.get_job(job_id)
            if not linked or not persisted or persisted.get("workstream_id") != blocker["workstream_id"]:
                return AdmissionResult(reason="unknown: monitor link was not confirmed; inspect local state before retry")
        # Creation never auto-enrolls a job or widens the eligibility gate.
        # A separate current human link request is needed after enrollment.
        if not db.consume_human_origin(agent.session_id, row["id"], consumer="workstream_job_scope"):
            return AdmissionResult(reason="unknown: operation receipt was not consumed; inspect local state before retry")
        action = ("linked locally to blocker evidence; no execution permission granted" if job_id is not None
                  else "created locally; not enrolled or linked; no execution permission granted")
        return AdmissionResult(True, job["id"], f"Monitor job {job['id']} {action}.")
    except (ValueError, notepad.WorkstreamError) as exc:
        return AdmissionResult(reason=str(exc))
