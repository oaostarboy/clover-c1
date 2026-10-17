"""After-reply actionable suggestions on the existing owned choice picker."""
from __future__ import annotations

import asyncio
import copy
import inspect
import time

from agent import actionable_suggestions as actions
from agent import workstreams as ws
from clover_constants import get_clover_home


def _strict_policy() -> dict:
    from clover_cli.managed_scope import get_managed_dir, load_managed_config

    # M1 supplies the shared fresh strict reader. Before that dependency lands,
    # genuinely unconfigured scope works, but configured managed scope refuses
    # actions. Never substitute the forgiving/cached startup policy parser.
    if "strict" not in inspect.signature(load_managed_config).parameters:
        if get_managed_dir() is not None:
            raise ValueError("Action approval needs the administrator policy update (M1).")
        return {}
    return load_managed_config(strict=True)


async def send_offer(runner, event, artifact: dict) -> bool:
    """Bind one runtime proposal to the exact admitted transport and callback.

    No token or typed action payload from model/user text can create a pending
    capability. Unsupported adapters remain prose; a failed send grants nothing.
    """
    action = artifact.get("action", {})
    p = actions.pending(action.get("token", ""))
    if p is None or action.get("kind") != "pause_job" or action.get("target") != p.snapshot["id"]:
        return False
    if getattr(event, "internal", False):
        return False
    source = copy.deepcopy(event.source)
    home = get_clover_home().resolve()
    adapter = runner._adapter_for_source(source)
    credential = runner._adapter_credential_fingerprint(adapter)
    route_home = runner._resolve_profile_home_for_source(source).resolve()
    if (str(home) != p.turn.scope.profile or route_home != home
            or runner._session_key_for_source(source) != p.ingress.session_key
            or str(source.user_id) != str(getattr(p.agent, "_user_id", ""))):
        return False

    def authorized(uid):
        from gateway.run import _profile_runtime_scope

        if str(uid) != str(source.user_id) or time.monotonic() >= p.expires_at or not p.is_current():
            return False
        try:
            with _profile_runtime_scope(home):
                managed = _strict_policy()
                proactive = managed.get("proactive", {})
                if not isinstance(proactive, dict):
                    return False
                suggestions = proactive.get("suggestions", {})
                if not isinstance(suggestions, dict):
                    return False
                if proactive.get("enabled") is False or suggestions.get("enabled") is False:
                    return False
                return (runner._adapter_for_source(source) is adapter
                        and runner._adapter_credential_fingerprint(adapter) == credential
                        and runner._resolve_profile_home_for_source(source).resolve() == route_home
                        and runner._proactive_caller_is_admin(source)
                        and ws.scope_for_surface(
                            platform=source.platform.value, chat_type=source.chat_type,
                            chat_id=source.chat_id, user_id=source.user_id,
                            project=p.turn.scope.project) == p.turn.scope)
        except Exception:
            return False

    if not authorized(source.user_id):
        return False

    async def selected(chat_id, value):
        if str(chat_id) != str(source.chat_id):
            return "This action belongs to another chat. Nothing ran."
        # Existing permission/profile/task context, not a new agent or budget.
        # The existing job owner's final guard rechecks current authorization.
        return await asyncio.to_thread(
            p.context.copy().run, lambda: actions.choose(
                p, value, authorize=lambda: authorized(source.user_id)))

    return await runner._try_send_choice_picker(
        event, runner._session_key_for_source(source),
        f"Pause job {p.snapshot['id']} future runs?\nNo current run is cancelled. The failure is not repaired.",
        [{"value": "do", "label": "🔧 Do it"}, {"value": "later", "label": "Not now"}],
        selected, choice_guard={"owner_user_id": str(source.user_id),
                                "expires_at": p.expires_at, "authorize": authorized,
                                "denied_text": "Action expired, stopped or no longer authorized. Nothing ran; ask for a new action offer."})


async def after_reply(runner, event, result: dict) -> None:
    """Use the delivery owner's existing one-shot boundary, never a scheduler."""
    artifact = result.get("workstream_suggestion")
    if not isinstance(artifact, dict) or not artifact.get("action"):
        return
    adapter = runner._adapter_for_source(event.source)
    if adapter is None:
        return

    async def deliver():
        await send_offer(runner, event, artifact)

    if result.get("already_sent"):
        await deliver()
    elif callable(getattr(type(adapter), "register_post_delivery_callback", None)):
        key = runner._session_key_for_source(event.source)
        active = getattr(adapter, "_active_sessions", {}).get(key)
        adapter.register_post_delivery_callback(
            key, deliver, generation=getattr(active, "_clover_run_generation", None))
