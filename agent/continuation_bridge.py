"""Blocker-clear bridge between a linked monitor event and an owned next stage.

A monitor event is evidence, never authority and never a completion. This
module adds no executor and no new permission. It gives the runtime's existing,
admission-checked staged continuation (``router_backstop._continue_one`` ->
``claim_continuation_stage`` -> ``dispatch_generated_tool``) one extra
precondition and one extra trigger:

* **Hold (original owner, at binding).** When the runtime binds the later
  stages of a staged plan to the stage-before's durable row, and the agent's
  canonical scope holds exactly one human-approved, open ``blocker`` workstream
  that a proactive-eligible monitor job is linked to, the bound plan carries a
  ``hold`` naming that blocker and the exact plan reference is written to the
  workstream's authority link. Without a hold the plan behaves exactly as
  before. A hold can only *delay* a stage the runtime already accepted.
* **Release (scheduler -> runtime).** A real monitor change claims its pending
  occurrence and, only when the held plan's stage-before has a settled,
  delivered, clean completion, emits one internal ``continuation_release``
  event naming that delegation. The gateway delivers it as the same trusted
  completion-id turn a helper completion uses, so the stage is started by the
  runtime's own ``_continue_one`` after :func:`admit_release` re-validates
  every fence at admission time.

Nothing here reads model text, MessageEvent labels or scoped ids as proof.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

HOLD_VERSION = 1
EVENT_TYPE = "continuation_release"
CLAIMER_PREFIX = "scheduler:"
DEFAULT_BUDGET = {"turns": 1}


@dataclass(frozen=True)
class Release:
    occurrence_key: str
    claimer: str
    workstream_id: str


def _scope_dict(scope: Any) -> Dict[str, str]:
    return {"profile": scope.profile, "project": scope.project,
            "audience": scope.audience, "root": scope.root}


def _linked_job(scope: Any, workstream_id: str, settings: Any) -> Optional[dict]:
    """The eligible monitor job linked to ``workstream_id`` in exactly ``scope``."""
    from agent import workstreams
    from cron import jobs

    for job in jobs.list_jobs(include_disabled=True)[:200]:
        if str(job.get("workstream_id") or "") != workstream_id:
            continue
        job_id = str(job.get("id") or "")
        if not job_id or not workstreams.job_eligible(settings, job_id):
            continue
        if not (job.get("monitor_script") or job.get("monitor_url")):
            continue
        try:
            if workstreams.job_scope(job) == scope:
                return job
        except Exception:
            continue
    return None


def _blocker_ok(record: Optional[dict]) -> bool:
    return bool(
        record and record.get("approved") and record.get("kind") == "blocker"
        and record.get("status") in ("open", "deferred")
    )


def select_hold(agent: Any) -> Optional[dict]:
    """The hold a plan bound *now* in this agent's scope must carry, else ``None``.

    Exactly one approved open blocker with an eligible linked monitor job in
    the canonical scope; zero or several yields no hold (ordinary behavior).
    """
    try:
        from agent import workstreams
        from cron import notepad

        settings = workstreams.load_settings()
        if not settings.enabled:
            return None
        scope = workstreams.scope_for_agent(agent)
        if settings.private_only and not scope.private:
            return None
        found = []
        for row in notepad.list_workstreams(
            scope, statuses=["open", "deferred"], kinds=["blocker"], limit=50,
        ):
            if not _blocker_ok(row):
                continue
            job = _linked_job(scope, row["workstream_id"], settings)
            if job is not None:
                found.append((row, job))
        if len(found) != 1:
            return None
        row, job = found[0]
        return {
            "version": HOLD_VERSION, "workstream_id": row["workstream_id"],
            "job_id": str(job["id"]), "scope": _scope_dict(scope),
        }
    except Exception:
        logger.debug("blocker hold selection failed closed", exc_info=True)
        return None


def runtime_fingerprint(agent: Any) -> dict:
    """Effective route + inherited tool surface (no secrets) of ``agent``."""
    from agent.delegation_followthrough import runtime_policy_for

    return runtime_policy_for({}, agent)


def record_authority(agent: Any, hold: dict, plan: dict) -> bool:
    """Write the exact plan reference onto the blocker's authority link.

    References only: a link grants nothing; every release re-validates the
    live plan row. ``False`` (nothing linked) when a different plan already
    holds this blocker.
    """
    try:
        from agent import workstreams
        from cron import notepad

        scope = workstreams.scope_for_agent(agent)
        ref = {
            "delegation_id": plan["delegation_id"], "request_id": plan["request_id"],
            "generation": plan["generation"], "stage_digest": plan["digests"][0],
            "session_id": plan.get("session_id", ""),
        }
        link = notepad.link_authority(
            scope, hold["workstream_id"], plan_ref=ref, budget=dict(DEFAULT_BUDGET))
        return link["plan_ref"] == ref
    except Exception:
        logger.debug("authority link not written", exc_info=True)
        return False


def _hold_scope(hold: dict):
    from agent import workstreams

    s = hold.get("scope") or {}
    return workstreams.Scope(s.get("profile", ""), s.get("project", ""),
                             s.get("audience", ""), s.get("root", ""))


def held_route(job: dict) -> Tuple[str, Optional[str]]:
    """Scheduler-side classification of a linked job's blocker plan.

    ``("ready", delegation_id)`` -- a live unconsumed held plan whose
    stage-before completed cleanly and was claimed/delivered;
    ``("waiting", delegation_id)`` -- live but the stage-before is not settled;
    ``("none", None)`` -- no held plan (consume the event as unknown).
    """
    try:
        from agent import workstreams
        from agent.delegation_followthrough import plan_is_live
        from cron import notepad
        from tools import async_delegation as ad

        scope = workstreams.job_scope(job)
        ws = str(job.get("workstream_id") or "")
        authority = notepad.get_authority(scope, ws) if ws else None
        if not authority:
            return "none", None
        did = str(authority["plan_ref"].get("delegation_id") or "")
        plan = ad.get_continuation_plan(did) if did else None
        hold = plan.get("hold") if isinstance(plan, dict) else None
        if (
            not isinstance(hold, dict) or hold.get("workstream_id") != ws
            or hold.get("job_id") != str(job.get("id") or "")
            or _hold_scope(hold) != scope or not plan_is_live(plan)
            or plan.get("consumed") != 0 or not plan.get("stages")
        ):
            return "none", None
        row = ad.get_durable_delegation(did) or {}
        if str(row.get("state") or "") == "completed" and did in ad.owned_receipts_ready([did]):
            return "ready", did  # a claimed/delivered terminal receipt
        if str(row.get("state") or "") in ("", "running", "finalizing", "stalling", "completed"):
            return "waiting", did  # not settled/claimed yet
        return "none", None  # failed / unknown / dropped: never a release
    except Exception:
        logger.debug("held route lookup failed closed", exc_info=True)
        return "none", None


def release_event(job: dict, delegation_id: str, occurrence: dict) -> Optional[dict]:
    """The internal event the gateway turns into a trusted completion-id turn."""
    from tools import async_delegation as ad

    route = ad.get_delegation_route(delegation_id)
    if not route:
        return None
    evt: Dict[str, Any] = {
        "type": EVENT_TYPE, "delegation_id": delegation_id,
        "workstream_id": str(job.get("workstream_id") or ""),
        "job_id": str(job.get("id") or ""),
        "occurrence_key": str(occurrence.get("occurrence_key") or ""),
        "emitted_at": time.time(),
    }
    for field, value in route.items():
        if value:
            evt[field] = value
    return evt


def emit(evt: dict) -> bool:
    try:
        from tools.process_registry import process_registry

        process_registry.completion_queue.put(evt)
        return True
    except Exception:
        logger.warning("continuation release not queued", exc_info=True)
        return False


def admit_release(agent: Any, plan: dict, delegation_id: str) -> Tuple[Optional[Release], str]:
    """Admission-time validation of a held stage's release.

    Returns ``(Release, "")`` only when every fence holds *now*; otherwise
    ``(None, reason)`` -- ``"waiting"`` when no release has been claimed yet.
    """
    try:
        from agent import workstreams
        from cron import notepad

        hold = plan.get("hold")
        settings = workstreams.load_settings()
        if not settings.enabled:
            return None, "proactive is disabled"
        scope = workstreams.scope_for_agent(agent)
        if _hold_scope(hold) != scope:
            return None, "scope changed since the plan was bound"
        if getattr(agent, "_interrupt_requested", False) is True:
            return None, "stop requested"
        ws = hold["workstream_id"]
        if not _blocker_ok(notepad.get_workstream(scope, ws)):
            return None, "blocker is no longer approved and open"
        job = _linked_job(scope, ws, settings)
        if job is None or str(job.get("id")) != hold.get("job_id"):
            return None, "monitor link is gone or not eligible"
        authority = notepad.get_authority(scope, ws)
        ref = (authority or {}).get("plan_ref") or {}
        if (
            not authority or ref.get("delegation_id") != delegation_id
            or ref.get("request_id") != plan.get("request_id")
            or ref.get("generation") != plan.get("generation")
            or ref.get("stage_digest") != (plan.get("digests") or [None])[0]
        ):
            return None, "authority link does not match the bound plan"
        if int((authority.get("budget") or {}).get("turns", 0)) < 1:
            return None, "continuation budget exhausted"
        if runtime_fingerprint(agent) != plan.get("runtime_policy"):
            return None, "effective route or tool surface changed"
        claimed = [
            o for o in notepad.list_occurrences(scope, ws)
            if o.get("state") == "claimed" and o.get("source") == "monitor"
            and str(o.get("claimer") or "").startswith(CLAIMER_PREFIX)
        ]
        if not claimed:
            return None, "waiting"
        occurrence = claimed[0]
        return Release(occurrence["occurrence_key"], occurrence["claimer"], ws), ""
    except Exception:
        logger.debug("release admission failed closed", exc_info=True)
        return None, "admission could not be validated"


def settle(agent: Any, release: Release, state: str, detail: str, *, spend: bool = False) -> None:
    """Settle the claimed occurrence (and charge the original budget once)."""
    try:
        from agent import workstreams
        from cron import notepad

        scope = workstreams.scope_for_agent(agent)
        if spend:
            try:
                notepad.spend_budget(scope, release.workstream_id, turns=1)
            except notepad.BudgetExhausted:
                logger.warning("continuation budget could not be charged", exc_info=True)
        notepad.settle_occurrence(
            scope, release.workstream_id, occurrence_key=release.occurrence_key,
            claimer=release.claimer, state=state, detail=detail,
        )
    except Exception:
        logger.warning("continuation occurrence not settled", exc_info=True)


def deny(agent: Any, plan: dict, reason: str) -> None:
    """A release was claimed but failed admission: settle it cancelled, never
    leave it claimed (a later restart would call it unknown) and never retry."""
    try:
        from agent import workstreams
        from cron import notepad

        scope = workstreams.scope_for_agent(agent)
        ws = (plan.get("hold") or {}).get("workstream_id")
        if not ws:
            return
        for o in notepad.list_occurrences(scope, ws):
            if (
                o.get("state") == "claimed" and o.get("source") == "monitor"
                and str(o.get("claimer") or "").startswith(CLAIMER_PREFIX)
            ):
                notepad.settle_occurrence(
                    scope, ws, occurrence_key=o["occurrence_key"], claimer=o["claimer"],
                    state="cancelled", detail=f"not started: {reason}"[:200],
                )
    except Exception:
        logger.warning("denied continuation release not settled", exc_info=True)
