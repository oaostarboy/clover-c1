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
        data = json.loads(message[len("workstream-monitor: "):])
        if not isinstance(data, dict) or set(data) != {
            "scope", "workstream_id", "prompt", "schedule", "monitor_script", "approve"}:
            raise ValueError("exact monitor request fields required")
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
        # This approves evidence linkage only, not an executable stage.
        ws.approve_workstream(scope, blocker["workstream_id"], settings=settings,
                             origin=receipt, human_reader=origin.read_human_origin, db=db)
        metadata = {"version": 1, "scope": data["scope"], "request": data,
                    "owner": {"session_id": receipt.session_id,
                              "message_row_id": receipt.message_row_id,
                              "ingress_event_id": receipt.ingress_event_id}}
        with jobs._use_workstream_scope_owner(metadata):
            job = jobs.create_job(data["prompt"], data["schedule"], deliver="local",
                                  workdir=scope.project, monitor_script=data["monitor_script"])
        # Creation never auto-enrolls a job or widens the eligibility gate.
        # A separate current human link request is needed after enrollment.
        db.consume_human_origin(agent.session_id, row["id"], consumer="workstream_job_scope")
        return AdmissionResult(True, job["id"])
    except (ValueError, notepad.WorkstreamError) as exc:
        return AdmissionResult(reason=str(exc))
