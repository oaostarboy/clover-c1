"""Profile-local deterministic settings controls, shared by chat surfaces.

No agent, ledger or scheduler is constructed here. Settings readers reload the
file at operation boundaries; in-flight turns keep their current context.
"""
from __future__ import annotations

import copy

from agent.workstreams import load_settings
from clover_cli import config as config_api


MIGRATION = (
    "Recurring prompts now use /loop. Use /loop <interval> <prompt> "
    "(or /loop status | pause | resume | stop). "
    "/proactive only controls the work ledger; nothing was run or changed."
)


def proactive_command(args: str = "", *, can_change: bool) -> str:
    """Read status or apply a bounded preset through the config persistence owner.

    Gateway callers MUST pass a decision from their authenticated admin boundary.
    Local CLI/TUI use the same filesystem authority as `clover config set`.
    """
    action = args.strip().lower()
    if action not in {"", "status", "on", "off", "enable", "disable"}:
        return MIGRATION
    changing = action in {"on", "off", "enable", "disable"}
    if changing and not can_change:
        return ("Only an explicitly configured gateway admin can change proactive settings. "
                "Ask the owner to set allow_admin_from (group_allow_admin_from in groups).")
    note = ""
    try:
        if changing:
            # Serialize read/modify/save with other in-process config writers.
            with config_api._CONFIG_LOCK:
                raw = config_api.require_readable_config_before_write()
                updated = copy.deepcopy(raw)
                root = updated.setdefault("proactive", {})
                if not isinstance(root, dict):
                    raise ValueError("proactive must be a settings mapping")
                enabled = action in {"on", "enable"}
                if enabled and load_settings(updated).mode != "active":
                    # First transition from shadow: deterministic recall/correction
                    # and compaction evidence only. No suggestion/judge/procedure
                    # calls, repair budget, monitor enrollment or timing grant.
                    root["mode"] = "active"
                    for section in ("recall", "compaction_audit"):
                        child = root.setdefault(section, {})
                        if not isinstance(child, dict):
                            raise ValueError(f"proactive.{section} must be a mapping")
                        child["enabled"] = True
                root["enabled"] = enabled
                if updated != raw:
                    config_api.save_config(updated, strip_defaults=False)
                    persisted = config_api.require_readable_config_before_write()
                    if persisted.get("proactive") != root:
                        raise RuntimeError("settings readback did not match the requested change")
                    note = "Saved; settings readers use this on the next turn/operation. "
                else:
                    note = "Already saved; no change. "
        # Read both the saved file and the real merged/managed reader. Never
        # replace an agent's config, history, tools or cached prompt mid-turn.
        # Unlike the forgiving diagnostic raw reader, a status menu must not
        # label broken YAML as Off (or create a corruption backup on a read).
        try:
            raw = config_api.fast_safe_load(
                config_api.get_config_path().read_text(encoding="utf-8-sig")
            ) or {}
        except FileNotFoundError:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError("config must be a settings mapping")
        persisted = load_settings(raw)
        effective = load_settings(config_api.load_config(migrate=False))
    except Exception:
        return "Proactive settings could not be confirmed; no success claimed. Check config permissions and administrator policy."
    saved_state = "On" if persisted.enabled else "Off"
    effective_state = "On" if effective.enabled else "Off"
    text = (
        f"{note}Proactive: {saved_state} (saved, mode: {persisted.mode}).\n"
        f"Effective settings: {effective_state} (mode: {effective.mode}); "
        "in-flight turns keep their current context. No restart required.\n"
        "Keeps scoped unfinished work, corrections and bounded recall. "
        "Not a universal autonomous executor: enabling creates no jobs, "
        "external-action permission or extra model-call opt-in. "
        "Existing schedules and explicitly configured opt-ins stay unchanged.\n"
        "/proactive on — Enable; /proactive off — Disable; /proactive status"
    )
    if not can_change:
        text += "\nChanging settings requires an explicitly configured gateway admin."
    return text
