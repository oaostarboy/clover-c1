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
    return {
        "provider": creds.get("provider") or getattr(parent_agent, "provider", None),
        "model": creds.get("model") or getattr(parent_agent, "model", None),
        "api_mode": creds.get("api_mode") or getattr(parent_agent, "api_mode", None),
        "endpoint_sha256": sha256(str(endpoint).encode("utf-8")).hexdigest() if endpoint else None,
        "tool_surface": sorted(str(name) for name in tool_names),
    }


def stage_digest(stage: dict) -> str:
    encoded = json.dumps(stage, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def matches_stage(args: Any, stage: dict) -> bool:
    """Require the tool request to name exactly the predeclared next payload."""
    if not isinstance(args, dict) or set(args) - {"goal", "context", "action"}:
        return False
    if args.get("action") not in (None, "", "spawn"):
        return False
    requested = {"goal": args.get("goal")}
    if args.get("context") is not None:
        requested["context"] = args.get("context")
    return stage_digest(requested) == stage_digest(stage)


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
