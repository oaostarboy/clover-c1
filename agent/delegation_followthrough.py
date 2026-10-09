"""Validation and durable binding for bounded delegate follow-through.

Stage text is merely task data until the runtime's generation-bound dispatch
 ticket accepts a real async job. This module deliberately contains no
model-facing authorization flag.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Iterable, Optional

MAX_STAGES = 1
MAX_GOAL_CHARS = 2000
MAX_CONTEXT_CHARS = 8000
MAX_PLAN_BYTES = 24_000
MAX_PLAN_AGE_SECONDS = 24 * 60 * 60


def canonical_stages(value: Any) -> Optional[tuple[dict, ...]]:
    """Return a bounded immutable plan of exact {goal, context?} payloads."""
    if value is None:
        return ()
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_STAGES:
        return None
    stages = []
    for stage in value:
        if not isinstance(stage, dict) or set(stage) - {"goal", "context"}:
            return None
        goal, context = stage.get("goal"), stage.get("context")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_GOAL_CHARS:
            return None
        if context is not None and (not isinstance(context, str) or len(context) > MAX_CONTEXT_CHARS):
            return None
        stages.append({"goal": goal, **({"context": context} if context is not None else {})})
    encoded = json.dumps(stages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PLAN_BYTES:
        return None
    return tuple(stages)


def runtime_policy_for(creds: Any, parent_agent: Any) -> dict:
    """Fingerprint the effective route and inherited tool surface without secrets."""
    from hashlib import sha256

    creds = creds if isinstance(creds, dict) else {}
    endpoint = creds.get("base_url") or getattr(parent_agent, "base_url", None)
    tool_names = getattr(parent_agent, "_delegate_saved_tool_names", None)
    if tool_names is None:
        tool_names = getattr(parent_agent, "enabled_toolsets", ()) or ()
    if isinstance(tool_names, str):
        tool_names = [tool_names]
    def names(value):
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set, frozenset, dict)):
            return []
        return sorted(str(name) for name in value)

    # Enabled labels alone are not capabilities: child construction separately
    # subtracts the parent's deny-list and resolves role/depth/MCP restrictions.
    # Fence all those inputs, plus the actually loaded parent tool names.
    from tools import approval, delegate_tool
    depth = getattr(parent_agent, "_delegate_depth", 0)
    depth = depth if isinstance(depth, int) else 0
    from clover_cli.config import load_config_readonly
    config = load_config_readonly()
    session_key = approval.get_current_session_key(default="") or getattr(parent_agent, "session_id", "")
    session_key = session_key if isinstance(session_key, str) else ""
    with approval._lock:
        approval_state = {
            "config": approval._get_approval_config(),
            "transport": (config.get("security") or {}).get("approval", {}),
            "configured_allowlist": config.get("command_allowlist", []),
            "process_yolo": approval._YOLO_MODE_FROZEN,
            "session_yolo": session_key in approval._session_yolo,
            "session_allowlist": names(approval._session_approved.get(session_key, ())),
            "permanent_allowlist": names(approval._permanent_approved),
        }
    approval_digest = sha256(json.dumps(approval_state, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "provider": creds.get("provider") or getattr(parent_agent, "provider", None),
        "model": creds.get("model") or getattr(parent_agent, "model", None),
        "api_mode": creds.get("api_mode") or getattr(parent_agent, "api_mode", None),
        "endpoint_sha256": sha256(str(endpoint).encode("utf-8")).hexdigest() if endpoint else None,
        "tool_surface": names(tool_names),
        "enabled_toolsets": (None if getattr(parent_agent, "enabled_toolsets", None) is None
                             else names(getattr(parent_agent, "enabled_toolsets", None))),
        "disabled_toolsets": names(getattr(parent_agent, "disabled_toolsets", None)),
        "loaded_tool_names": names(getattr(parent_agent, "valid_tool_names", None)),
        "child_restrictions": {
            "parent_depth": depth,
            "max_spawn_depth": delegate_tool._get_max_spawn_depth(),
            "orchestrator_enabled": delegate_tool._get_orchestrator_enabled(),
            "inherit_mcp": delegate_tool._get_inherit_mcp_toolsets(),
            "blocked_tools": names(delegate_tool.DELEGATE_BLOCKED_TOOLS),
        },
        "approval_policy_sha256": approval_digest,
    }


def stage_digest(stage: dict) -> str:
    encoded = json.dumps(stage, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_SPAWN_TOP_LEVEL_KEYS = frozenset({"goal", "context", "action", "tasks", "handoff"})
_HANDOFF_KEYS = frozenset(
    {"work", "outcome", "estimated_minutes_min", "estimated_minutes_max"}
)
# Display-only task label; it never reaches the child's goal, tools or route.
_TASK_DISPLAY_KEYS = frozenset({"title"})


def _requested_stage(args: Any) -> Optional[dict]:
    """The single {goal, context?} payload a spawn call would actually run.

    A model repeats a stored stage in either of the two shapes every ordinary
    dispatch uses: top-level ``goal``/``context`` or ``tasks=[{goal, context}]``
    (optionally with the user-facing ``handoff`` card text and a task
    ``title``). Both are normalised to the same payload. Anything else that
    could widen what runs (a second task, a model/tier/toolsets/role override,
    ``follow_through``, ``max_iterations``, unknown keys, a malformed handoff,
    a goal given in both places) yields ``None`` so the caller fails closed.
    """
    if not isinstance(args, dict) or set(args) - _SPAWN_TOP_LEVEL_KEYS:
        return None
    if args.get("action") not in (None, "", "spawn"):
        return None
    handoff = args.get("handoff")
    if handoff is not None:
        if (
            not isinstance(handoff, dict) or set(handoff) - _HANDOFF_KEYS
            or any(isinstance(v, (dict, list)) for v in handoff.values())
        ):
            return None
    tasks = args.get("tasks")
    if tasks is None:
        source = args
    else:
        # An empty/None ``tasks`` next to a top-level goal is the tolerated
        # small-model shape; any non-empty tasks must be the only carrier.
        if isinstance(tasks, list) and not tasks:
            source = args
        elif (
            not isinstance(tasks, list) or len(tasks) != 1
            or not isinstance(tasks[0], dict)
            or args.get("goal") is not None or args.get("context") is not None
            or set(tasks[0]) - ({"goal", "context"} | _TASK_DISPLAY_KEYS)
        ):
            return None
        else:
            source = tasks[0]
    requested = {"goal": source.get("goal")}
    if source.get("context") is not None:
        requested["context"] = source.get("context")
    return requested


def matches_stage(args: Any, stage: dict) -> bool:
    """Require the tool request to name exactly the predeclared next payload."""
    requested = _requested_stage(args)
    return requested is not None and stage_digest(requested) == stage_digest(stage)


def persist_accepted_plan(
    *, delegation_id: str, request_id: str, generation: int,
    receipt_ids: Iterable[str], stages: tuple[dict, ...], accepted_at: float,
    runtime_policy: dict,
) -> bool:
    """Attach provenance only to an actually persisted running async row."""
    if not stages:
        return True
    payload = {
        "version": 1, "delegation_id": delegation_id, "request_id": request_id,
        "generation": generation, "receipt_ids": list(receipt_ids),
        "accepted_at": accepted_at, "expires_at": accepted_at + MAX_PLAN_AGE_SECONDS,
        "stages": list(stages), "digests": [stage_digest(stage) for stage in stages],
        "runtime_policy": runtime_policy,
        "consumed": 0,
    }
    try:
        from tools.async_delegation import bind_continuation_plan
        return bool(bind_continuation_plan(delegation_id, payload))
    except Exception:
        return False


def plan_is_live(plan: Any, now: Optional[float] = None) -> bool:
    if not isinstance(plan, dict) or plan.get("version") != 1 or plan.get("cancelled"):
        return False
    expires = plan.get("expires_at")
    return isinstance(expires, (int, float)) and (now or time.time()) < expires
