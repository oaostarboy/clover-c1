"""Typed suggestions are proposals, not executable model text or permission.

Initial adapter: pause future runs of one failing local cron job. No universal
application restart/unsaved-work adapter, shell plans, paid turns or scheduler.
Pending capabilities are process-local and bounded; restart invalidates them.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from agent import workstream_origin as origin
from agent import workstreams as ws
from cron import jobs, notepad


@dataclass
class _Proposal:
    agent: Any
    turn: Any
    artifact_id: str
    snapshot: dict
    policy: dict
    is_current: Any
    expires_at: float
    ingress: Any
    context: Any = field(default_factory=contextvars.copy_context)
    state: str = "proposed"
    lock: Any = field(default_factory=threading.Lock)


_PENDING: dict[str, _Proposal] = {}
_LOCK = threading.Lock()
TTL_SECONDS = 300
MAX_PENDING = 64


def _job_ok(job: dict, scope: ws.Scope) -> bool:
    # Pausing only future local runs never cancels a live run, sends data,
    # deletes a job or repairs its failure. Unknown ownership/state is denied.
    return bool(
        job.get("enabled") is True and job.get("state") == "scheduled"
        and job.get("last_status") == "error" and job.get("deliver") == "local"
        and not job.get("run_claim") and not job.get("fire_claim")
        and isinstance(job.get("workstream_scope"), dict)
        and job.get("workdir") == scope.project
        and jobs_scope(job) == scope
    )


def jobs_scope(job: dict) -> ws.Scope | None:
    try:
        return ws.job_scope(job)
    except (TypeError, ValueError, KeyError):
        return None


def _saved_authorization(*, platform: str | None = None) -> tuple[str, bool]:
    """Fresh scoped authorization inputs, never startup caches or all-file hashes.

    Keep the two saved layers separate: an administrator overlay must not hide
    a changed user restriction. No config/env/cache mutation or raw policy logs.
    Missing/empty documents retain their ordinary unconfigured meaning.
    """
    from agent.skill_utils import parse_config_string_list
    from clover_cli.config import _expand_env_vars, fast_safe_load, get_config_path
    from clover_cli.managed_scope import load_managed_config

    def mapping(value):
        if value is None:
            return {}
        if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
            raise ValueError("invalid authorization mapping")
        return value

    def names(value):
        if value is None:
            return []
        if not isinstance(value, (str, list)):
            raise ValueError("invalid authorization names")
        if isinstance(value, list) and any(not isinstance(v, str) for v in value):
            raise ValueError("invalid authorization name")
        if isinstance(value, str) and value.strip().startswith("["):
            import ast

            parsed = ast.literal_eval(value.strip())
            if not isinstance(parsed, list) or any(not isinstance(v, str) for v in parsed):
                raise ValueError("invalid authorization names")
        return sorted({v.strip() for v in parse_config_string_list(value) if v.strip()})

    denied = False
    def relevant(document):
        nonlocal denied
        document = mapping(document)
        agent = mapping(document.get("agent"))
        disabled = names(agent.get("disabled_toolsets"))
        # The loaded task tool surface can itself predate a saved denial.
        # Use the ordinary toolset subtraction rules, including bundle semantics.
        from toolsets import bundle_non_core_tools, get_toolset, resolve_toolset, validate_toolset

        for name in disabled:
            if validate_toolset(name):
                tools = (bundle_non_core_tools(name) if name.startswith("clover-")
                         or (get_toolset(name) or {}).get("posture") else resolve_toolset(name))
                if "cronjob" in tools:
                    denied = True
        platform_tools = mapping(document.get("platform_toolsets"))
        if platform is not None:
            # A null platform selection is the same default as an absent key,
            # including when ordinary job registration migrates old settings.
            platform_tools = {k: v for k, v in platform_tools.items() if v is not None}
        if platform is not None and platform_tools.get(platform) is not None:
            # Watcher consent also needs the explicit native platform selection.
            # Reuse string/list and composed-toolset resolution, not the CLI
            # discovery normalizer: that may discover plugins/migrate settings.
            # Missing/null selections keep the normal default. Check both saved
            # layers separately so an overlay cannot hide a denial.
            if not any("cronjob" in resolve_toolset(name)
                       for name in names(platform_tools[platform])):
                denied = True
        platforms = mapping(document.get("platforms"))
        # Restrict source binding to admission/control/credential inputs, not
        # display/model/latency preferences. Native actor/route checks still run.
        source_keys = {"enabled", "token", "allow_from", "group_allow_from", "dm_policy",
                       "group_policy", "allow_admin_from", "group_allow_admin_from",
                       "user_allowed_commands", "group_user_allowed_commands", "allowed_users",
                       "allowed_chats", "group_allowed_chats", "allowed_topics", "require_mention",
                       "verified_owner_controls"}
        def source(value):
            value = mapping(value)
            extra = mapping(value.get("extra"))
            return {k: v for k, v in {**extra, **value}.items() if k in source_keys}

        return {"disabled_toolsets": disabled,
                "toolsets": names(document.get("toolsets")),
                "platform_toolsets": {k: names(v) for k, v in platform_tools.items()},
                "platforms": {k: source(v) for k, v in platforms.items()},
                "gateway_platforms": {k: source(v) for k, v in mapping(
                    mapping(document.get("gateway")).get("platforms")).items()},
                "approvals": mapping(document.get("approvals")),
                "approval_transport": mapping(mapping(document.get("security")).get("approval")),
                "command_allowlist": names(document.get("command_allowlist"))}

    try:
        path = get_config_path()
        try:
            user = fast_safe_load(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            # A dangling settings link is unreadable policy, not absence.
            try:
                path.lstat()
            except FileNotFoundError:
                user = None
            else:
                raise
        layers = [relevant(_expand_env_vars(user)),
                  relevant(_expand_env_vars(load_managed_config(strict=True)))]
        encoded = json.dumps(layers, sort_keys=True, allow_nan=False)
        if "${" in encoded:
            raise ValueError("unresolved authorization input")
        return hashlib.sha256(encoded.encode()).hexdigest(), denied
    except Exception:
        # Parser messages may contain private config values. Never expose them.
        raise ValueError("Current saved permissions are unavailable, invalid or deny this action. Nothing ran.") from None


def _policy(agent) -> dict:
    from agent.delegation_followthrough import runtime_policy_for

    # Runtime state still binds the original tools, route, approvals and budget;
    # the saved fingerprint independently bypasses both mtime/size caches.
    digest, denied = _saved_authorization()
    return {"runtime": runtime_policy_for({}, agent), "source_policy": digest,
            "saved_tool_denied": denied}


def propose(agent: Any, turn: Any, artifact: dict, *, is_current) -> dict | None:
    """Attach a typed bounded target to an already gated, committed offer.

    User/model/job text can nominate a canonical ID, never authorize it. Only
    an authenticated native callback can accept the stored exact proposal.
    """
    if "cronjob" not in (getattr(agent, "valid_tool_names", ()) or ()):
        return None
    ids = set(re.findall(r"(?<![\w])([0-9a-f]{12})(?![\w])", artifact["text"]))
    if len(ids) != 1:
        return None
    job_id = ids.pop()
    if not re.search(r"(?<![\w])" + re.escape(job_id) + r"(?![\w])", turn.query):
        return None
    job = jobs.get_job(job_id)
    if not job or not _job_ok(job, turn.scope) or not is_current():
        return None
    try:
        policy = _policy(agent)
    except ValueError:
        return None
    if policy["saved_tool_denied"]:
        return None
    proposal = _Proposal(agent, turn, artifact["receipt_id"], job,
                         policy, is_current,
                         time.monotonic() + TTL_SECONDS, turn.ingress)
    with _LOCK:
        for key, old in list(_PENDING.items()):
            if time.monotonic() >= old.expires_at or old.state != "proposed":
                del _PENDING[key]
        if len(_PENDING) >= MAX_PENDING:
            return None
        token = secrets.token_urlsafe(18)
        _PENDING[token] = proposal
    return {"kind": "pause_job", "target": job_id, "token": token,
            "state": "proposed", "summary": f"Pause job {job_id} future runs?"}


def pending(token: str) -> _Proposal | None:
    """Opaque local lookup, never deserialize a model-supplied action payload."""
    with _LOCK:
        proposal = _PENDING.get(token)
    return proposal if proposal and proposal.state == "proposed" and time.monotonic() < proposal.expires_at else None


def _check(p: _Proposal) -> None:
    if time.monotonic() >= p.expires_at or not p.is_current():
        raise ValueError("This offer expired or the task changed. Ask again.")
    agent, turn = p.agent, p.turn
    hard_stop = getattr(agent, "_hard_interrupt_requested", None)
    if (getattr(agent, "_interrupt_requested", False) or
            (hard_stop is not None and hard_stop.is_set())):
        raise ValueError("Stopped. Nothing else will run.")
    if ws.scope_for_agent(agent) != turn.scope or turn.scope.profile != ws.current_profile():
        raise ValueError("The profile or project changed. Ask again.")
    settings = ws.load_settings()
    if not settings.active or not settings.suggestions_enabled:
        raise ValueError("Action offers are now disabled.")
    if "cronjob" not in (getattr(agent, "valid_tool_names", ()) or ()):
        raise ValueError("The original task no longer has this tool permission.")
    policy = _policy(agent)
    if policy["saved_tool_denied"] or policy != p.policy:
        raise ValueError("Tool or approval permissions changed. Ask again.")
    budget = getattr(agent, "iteration_budget", None)
    if budget is None or budget.remaining <= 0:
        raise ValueError("The original task budget is exhausted. Make a new request.")
    db = getattr(agent, "_session_db", None)
    receipt = origin.read_human_origin(db, turn.session_id, turn.row_id) if db else None
    if (receipt is None or receipt.consumed or receipt.ingress_event_id != turn.event_id
            or receipt.profile != turn.scope.profile
            or db.get_conversation_root(agent.session_id) != turn.conversation_root):
        raise ValueError("The original authenticated task is no longer current.")
    current = jobs.get_job(p.snapshot["id"])
    if current is None:
        raise ValueError("Job state is unknown. Nothing ran; inspect this exact job.")
    if current.get("run_claim") or current.get("fire_claim"):
        raise ValueError("This job has an active run. Nothing ran; wait for it to settle.")
    if current != p.snapshot or not _job_ok(current, turn.scope):
        raise ValueError("The job target or safety conditions changed. Nothing ran; ask again.")
    record = notepad.get_workstream(turn.scope, _workstream_id(p))
    if (not record or not record.get("approved") or record.get("status") not in {"open", "deferred"}
            or ws.feedback_state(turn.scope, record["workstream_id"]) in {"stop", "done", "reject"}):
        raise ValueError("The suggestion was stopped or withdrawn.")


def _workstream_id(p: _Proposal) -> str:
    receipts = notepad.list_receipts(p.turn.scope, kinds=["proposed"])
    row = next((r for r in receipts if r["receipt_id"] == p.artifact_id), None)
    if row is None or row["owner"] != "gateway_response":
        raise ValueError("The original suggestion receipt is unavailable.")
    return row["workstream_id"]


def _receipt(p: _Proposal, kind: str, state: str, *, extra: dict | None = None) -> None:
    detail = {"state": state, "action": "pause_job", "target": p.snapshot["id"],
              "session_id": p.turn.session_id, "event_id": p.turn.event_id,
              **(extra or {})}
    text = json.dumps(detail, separators=(",", ":"))
    if len(text) > 512:
        raise ValueError("Action receipt exceeds its bounded storage.")
    saved = notepad.add_receipt(p.turn.scope, _workstream_id(p), kind=kind,
                               owner="actionable_suggestion",
                               source_id=p.artifact_id + ":" + state, detail=text)
    rows = notepad.list_receipts(p.turn.scope, kinds=[kind])
    if not any(r["receipt_id"] == saved["receipt_id"] and r["detail"] == text for r in rows):
        raise ValueError("Action receipt readback is unknown.")
    p.state = state


def choose(p: _Proposal, value: str, *, authorize) -> str:
    """Called only by an owned native callback, within the original context.

    Single claim precedes any effect. No new agent, budget, paid route or tool
    grant. Runtime middleware still applies; a blocked/unknown call never retries.
    """
    with p.lock:
        if p.state != "proposed":
            return "This offer was already used. No second action ran."
        try:
            if value not in {"do", "later"} or not authorize():
                return "Not authorized now. Nothing ran."
            _check(p)
        except Exception as exc:
            return str(exc) if isinstance(exc, ValueError) else "Safety checks are unavailable. Nothing ran."
        if value == "later":
            _receipt(p, "deferred", "deferred")
            from datetime import timedelta
            from clover_time import now

            notepad.add_feedback(p.turn.scope, _workstream_id(p), kind="defer", actor="user",
                                 note="Not now (authenticated native action callback)",
                                 defer_until=(now() + timedelta(seconds=ws.load_settings().suggestions_cooldown_seconds)).isoformat())
            return "Not now. Nothing ran."
        try:
            snapshot_hash = hashlib.sha256(json.dumps(p.snapshot, sort_keys=True).encode()).hexdigest()
            _receipt(p, "authorized", "requested", extra={"snapshot": snapshot_hash})
        except Exception:
            p.state = "unknown"
            return "Approval could not be recorded. Nothing ran."

    from agent.tool_executor import dispatch_generated_tool

    def authorize_now():
        if not authorize():
            raise ValueError("Permission was revoked. Nothing ran.")
        _check(p)

    args = {"action": "pause", "job_id": p.snapshot["id"]}

    def before_dispatch(actual):
        if actual != args:
            raise ValueError("The proposed action changed. Nothing ran.")
        authorize_now()
        _receipt(p, "unknown", "started")

    try:
        with jobs.pause_job_guard(p.snapshot, authorize_now) as execution:
            raw = dispatch_generated_tool(p.agent, "cronjob", args,
                                          before_dispatch=before_dispatch)
        result = json.loads(raw)
        if result.get("success") is not True:
            _receipt(p, "unknown", "failed")
            return "The runtime refused or failed this action. No success is confirmed."
        if not execution.get("completed") or execution.get("job_id") != p.snapshot["id"]:
            _receipt(p, "unknown", "unknown")
            return "No execution receipt from the job owner. Outcome is unknown; no retry."
        _receipt(p, "executed", "completed")
    except Exception:
        p.state = "unknown"
        return "Action outcome is unknown. It will not retry; inspect this job."
    try:
        readback = jobs.get_job(p.snapshot["id"])
        if not readback or readback.get("enabled") is not False or readback.get("state") != "paused":
            raise ValueError("readback unavailable")
        # Only lifecycle fields may differ. Pausing is not a software repair.
        lifecycle = {"enabled", "state", "paused_at", "paused_reason"}
        if {k: v for k, v in readback.items() if k not in lifecycle} != {
                k: v for k, v in p.snapshot.items() if k not in lifecycle}:
            raise ValueError("target changed")
        _receipt(p, "saved", "verified")
    except Exception:
        p.state = "unknown"
        try:
            _receipt(p, "unknown", "verification_unknown")
        except Exception:
            pass  # No receipt success can be asserted when persistence fails.
        return "The tool reported completion; verification is unknown. It will not retry."
    return f"Verified: job {p.snapshot['id']} is paused for future runs. Its failure is not repaired."
