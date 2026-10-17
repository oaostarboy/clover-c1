"""Deterministic, profile-local controls shared by bot, CLI and TUI.

Only explicit actions write raw preferences. No model, scheduler or ledger is
constructed. In-flight turns keep their context; managed overlays stay separate.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from agent.workstreams import load_settings
from clover_cli import config as config_api

PROACTIVE_ACTIONS = (
    "/proactive on — On; /proactive off — Off; /proactive customize — Customize"
)
RECOMMENDED = {
    "recall": {"enabled": True},
    "suggestions": {"enabled": True, "preferred_timing": "after_reply",
                    "cooldown_seconds": 3600, "max_per_session": 1},
    "compaction_audit": {"enabled": True},
    "procedures": {"enabled": False},
}
PRESET_SUMMARY = (
    "First On: work recall/corrections, after-reply suggestions (3600s cooldown, "
    "1/session), summary audit. Saved choices take priority."
)
HELP = (
    PROACTIVE_ACTIONS + "\n"
    "/proactive recall on|off; suggestions on|off; timing after_reply|never; "
    "frequency hourly|daily; audit on|off; skill-drafts on|off.\n"
    "/proactive recommended — select the whole preset (keeps master On/Off).\n"
    "Text choices save immediately. Bot Customize uses Save/Cancel. "
    "/proactive advanced — monitoring, costs, scope and availability. "
    "/proactive status — Details. Recurring prompts use /loop <interval> <prompt>."
)
MIGRATION = "Unknown proactive choice; nothing changed.\n" + HELP
LOCKED = "Controls locked by command policy: admin access required."
FAILED = ("Proactive settings could not be confirmed; no success claimed. "
          "Check config permissions and administrator policy.")
OPTIONAL = (
    "Skill-draft suggestions: optional, owner-verified local audit workflows only; "
    "3 verified runs across 2 sessions by default, then /skills pending for approval. "
    "Ordinary skill creation stays independent. Matching ordinary skills is not "
    "yet coordinated; review for overlap.\n"
    "Advanced (information only): model-assisted judges can cost extra; automatic "
    "repair, monitor jobs and external actions need separate configuration and "
    "permission. These controls do not enable them or change their budgets."
)
ADVANCED_ENTRIES = {
    "monitors": "Task / job monitors",
    "watcher": "Inbox watcher (paid) — guide",
    "judges": "Model judges — availability",
    "repairs": "Summary repair — availability",
    "scope": "Scope / evidence / limits",
    "routines": "Routines / reminders",
    "desktop": "Desktop activity — unavailable",
}


def advanced_info(name: str, root: dict) -> str:
    """Read-only discovery: do not turn an unwired setting into a fake switch."""
    s = load_settings({"proactive": root})
    pages = {
        "monitors": (
            "Task / job monitors — guided setup, not an all-jobs switch.\n"
            f"Saved enrolled job links: {len(s.eligible_jobs)} (not running-status proof). "
            "A monitor observes one approved script or URL on its own schedule; "
            "unchanged output skips the agent. Changed output may run its configured "
            "model and use paid tokens. Workstream links require the exact project, "
            "audience, owner, approved record and enrolled job. A cleared blocker can "
            "resume only an already-authorized continuation, with Stop/expiry/budgets "
            "rechecked; evidence never grants permission.\n"
            "Use /cron to inspect and manage existing schedules. This menu creates "
            "or enrolls none. Off suspends workstream use, not independently scheduled jobs."
        ),
        "watcher": (
            "Inbox watcher — existing optional catalog automation, not desktop capture.\n"
            "It reads a connected inbox, sends candidate content to the configured "
            "monitor model for urgency scoring, and may send alerts. The starter "
            "cadence is every 30 minutes; scoring adds extra model usage, plus the "
            "scheduled agent's usage. Provider rates vary; no dollar estimate is implied. "
            "It is not activated by On or this menu; any existing jobs stay unchanged.\n"
            "Safe activation here is unavailable: the existing classifier lacks an "
            "enforced per-day call budget, and proactive.aux limits do not cover it. "
            "Before activation, separately review source, schedule, delivery, permissions, "
            "cost acceptance and enforceable job/model limits. No acceptance button is "
            "offered until those gates exist. /cron inspects existing jobs; "
            "/suggestions catalog shows separate automation ideas, not free features."
        ),
        "judges": (
            "Model-assisted suggestion judges — not wired to the foreground offer path. "
            "Only a supplied suppression/defer verdict is supported; this menu cannot "
            "start a judge or change spending. "
            f"Saved auxiliary limits: {s.aux_max_calls_per_event}/event, "
            f"{s.aux_max_calls_per_day}/day, {s.aux_max_output_tokens} output tokens. "
            "These are parsed settings, not an enforced budget for the inbox classifier. "
            "A paid integration needs explicit cost approval and enforced limits first."
        ),
        "repairs": (
            "Automatic model-assisted summary repair — not wired to the supported audit. "
            f"Saved max_repairs: {s.compaction_audit_max_repairs} (not activation). "
            "Summary audit keeps a bounded local capsule during supported native text "
            "compaction, with no repair-model calls. Opaque provider compaction is not "
            "audited. A paid repair workflow needs a separate reviewed opt-in and limits."
        ),
        "scope": (
            f"Scope / evidence / limits — private-only: {s.private_only}. "
            "Profile, project, audience and owner stay isolated. These are current "
            "effective limits; this information page changes none.\n"
            f"Work ledger: {s.threads_max_items} items, {s.threads_max_context_bytes} context bytes, "
            f"{s.threads_retention_days} retention days. Recall: {s.recall_max_items} items, "
            f"{s.recall_max_bytes} bytes. Draft evidence: at least "
            f"{max(3, s.procedures_min_verified_runs)} verified runs in "
            f"{max(2, s.procedures_min_sessions)} sessions.\n"
            "Typed owner receipts distinguish executed/saved/delivered. Missing evidence "
            "retrieval is bounded and read-only, not a model judge. Corrections and "
            "explicit feedback use authenticated input; silence means unknown. "
            "No broad capture or permission grant. Managed settings stay authoritative."
        ),
        "routines": (
            "Routines / reminders — separate scheduled jobs and in-chat loops. "
            "Use /cron for existing schedules, /loop status for recurring prompts, "
            "and /suggestions for automation proposals. Creating one is a separate "
            "explicit action that can consume model usage. Off does not cancel "
            "independently scheduled jobs or loops. This menu creates none."
        ),
        "desktop": (
            "Desktop activity capture is not available in this proactivity package. "
            "No screen, microphone, keystroke or app-activity watcher is installed or "
            "enabled here. MineContext and N.E.K.O. were research, not shipped capture. "
            "Task monitors and the paid inbox classifier are different capabilities."
        ),
    }
    if name == "advanced":
        return ("Advanced Customize — status and setup guides; no activation.\n" +
                "\n".join(f"/proactive {key} — {label}" for key, label in ADVANCED_ENTRIES.items()) +
                "\nOff does not cancel independent schedules. Recommended On adds no paid watcher, jobs or authority.")
    return pages[name]


FIELDS = {"recall": ("recall", "enabled"), "suggestions": ("suggestions", "enabled"),
          "timing": ("suggestions", "preferred_timing"),
          "audit": ("compaction_audit", "enabled"),
          "skill-drafts": ("procedures", "enabled")}


def _validate(root) -> None:
    if not isinstance(root, dict) or any(
        name in root and not isinstance(root[name], dict)
        for name in (*RECOMMENDED, "threads", "aux")
    ):
        raise ValueError("proactive sections must be settings mappings")


def _raw() -> dict:
    """Strict read without migrations, backup writes or forgiving broken YAML."""
    try:
        value = config_api.fast_safe_load(config_api.get_config_path().read_text(encoding="utf-8-sig")) or {}
    except FileNotFoundError:
        value = {}
    if not isinstance(value, dict) or not isinstance(value.get("proactive", {}), dict):
        raise ValueError("config and proactive must be settings mappings")
    _validate(value.get("proactive", {}))
    return value


def _put(root: dict, section: str, key: str, value) -> None:
    child = root.setdefault(section, {})
    if not isinstance(child, dict):
        raise ValueError(f"proactive.{section} must be a mapping")
    child[key] = value


def _persist(raw: dict, updated: dict) -> str:
    """Caller holds config lock; save only explicit leaf changes, then read back."""
    from clover_cli import managed_scope
    before, after = raw.get("proactive", {}), updated.get("proactive", {})
    keys = set()
    for name, value in after.items():
        old = before.get(name)
        if value != old:
            if isinstance(value, dict) and isinstance(old, (dict, type(None))):
                old_values = old or {}
                keys.update(f"proactive.{name}.{k}" for k in value
                            if k not in old_values or value[k] != old_values[k])
            else:
                keys.add(f"proactive.{name}")
    pins = managed_scope.managed_config_keys()
    if any(k == p or k.startswith(p + ".") or p.startswith(k + ".") for k in keys for p in pins):
        return "Not saved: a changed setting is managed by your administrator."
    if updated == raw:
        return "Already saved; no change."
    config_api.save_config(updated, strip_defaults=False, strip_managed=False)
    if config_api.require_readable_config_before_write().get("proactive") != after:
        raise RuntimeError("settings readback mismatch")
    return "Saved; applies next turn/operation."


def _choices(root: dict) -> dict:
    settings = load_settings({"proactive": root})
    sugg = root.get("suggestions", {})
    if not isinstance(sugg, dict):
        raise ValueError("suggestions must be a mapping")
    timing = sugg.get("preferred_timing")
    timing = timing if timing in ("after_reply", "never") else None
    return {"recall": settings.recall_enabled, "suggestions": settings.suggestions_enabled,
            "timing": timing, "cooldown": settings.suggestions_cooldown_seconds,
            "cap": settings.suggestions_max_per_session, "audit": settings.compaction_audit_enabled,
            "skill-drafts": settings.procedures_enabled}


def _describe(root: dict) -> str:
    c = _choices(root)
    onoff = lambda value: "On" if value else "Off"
    return (f"Work recall: {onoff(c['recall'])}; suggestions: {onoff(c['suggestions'])} "
            f"({c['timing'] or 'not chosen'}, {c['cooldown']}s cooldown, {c['cap']}/session); "
            f"summary audit: {onoff(c['audit'])}; skill drafts: {onoff(c['skill-drafts'])}.")


@dataclass
class ProactiveDraft:
    """Memory-only draft; optimistic snapshot rejects concurrent preference edits."""
    baseline: dict
    effective: dict
    changes: dict = field(default_factory=dict)

    @classmethod
    def open(cls) -> ProactiveDraft:
        with config_api._CONFIG_LOCK:
            baseline = copy.deepcopy(_raw().get("proactive", {}))
            effective = copy.deepcopy(config_api.load_config(migrate=False).get("proactive", {}))
            _validate(effective)
            return cls(baseline, effective)

    @property
    def root(self) -> dict:
        root = copy.deepcopy(self.baseline)
        for section, values in self.changes.items():
            for key, value in values.items():
                _put(root, section, key, value)
        return root

    def set(self, name: str, value: str) -> None:
        if name == "frequency" and value in {"hourly", "daily"}:
            _put(self.changes, "suggestions", "cooldown_seconds", 3600 if value == "hourly" else 86400)
            _put(self.changes, "suggestions", "max_per_session", 1)
        elif name == "timing" and value in {"after_reply", "never"}:
            _put(self.changes, *FIELDS[name], value)
        elif name in FIELDS and name != "timing" and value in {"on", "off"}:
            _put(self.changes, *FIELDS[name], value == "on")
        else:
            raise ValueError("Unsupported choice")

    def recommended(self) -> None:
        self.changes = copy.deepcopy(RECOMMENDED)

    def render(self, view: str = "basic") -> str:
        c = _choices(self.root)
        labels = {"recall": "🧠 Recall", "suggestions": "💡 Suggestions",
                  "audit": "🧾 Context checks", "skill-drafts": "📚 Skill drafts"}
        descriptions = {
            "recall": "Carry scoped unfinished work and corrections into relevant context. Bounded lexical recall can miss rephrasing.",
            "suggestions": "Offer useful local next steps after a reply. No extra model call or permission to act. Timing, cooldown and session cap still apply.",
            "audit": "Keep bounded evidence through supported text-summary compaction. No model repair; opaque provider compaction is not audited.",
            "skill-drafts": "Optional verified local-audit drafts go to /skills pending for approval. Ordinary skill creation stays independent; matching ordinary skills is not coordinated yet. Review for overlap.",
        }
        if view in labels:
            text = f"{labels[view]}: {'On' if c[view] else 'Off'} · unsaved\n" + descriptions[view]
        elif view == "timing":
            text = f"🕓 Suggestions timing: {c['timing'] or 'not chosen'}\nAfter replies or never. Off/On preserves your choice; nothing runs on a timer."
        elif view == "frequency":
            text = (f"⏱ Frequency: {c['cooldown']}s cooldown · {c['cap']}/session\n"
                    "Choose hourly or daily; both cap offers at 1/session. Existing custom limits remain unless you choose one.")
        elif view == "advanced":
            text = "⚙️ Advanced · status and setup guides\nPaid options never start from this menu."
        elif view in ADVANCED_ENTRIES:
            text = advanced_info(view, self.effective)
        elif view == "recommended":
            text = "✨ Recommended selected · unsaved\n" + _describe(self.root) + "\nMaster On/Off stays unchanged. Save to apply; Cancel to keep your choices."
        else:
            text = ("Customize · unsaved until Save\nChoose a feature. Save applies choices; Cancel keeps settings.")
        if (load_settings({"proactive": self.baseline}) != load_settings({"proactive": self.effective})
                or _choices(self.baseline) != _choices(self.effective)):
            text += "\nSaved/effective choices differ (managed policy); /proactive status has details."
        return text

    def choices(self, view: str = "basic") -> list[dict]:
        c = _choices(self.root)
        if view in {"recall", "suggestions", "audit", "skill-drafts"}:
            entries = [(f"{view} on", "On"), (f"{view} off", "Off")]
        elif view == "timing":
            entries = [("timing after_reply", "After replies"), ("timing never", "Never suggest")]
        elif view == "frequency":
            entries = [("frequency hourly", "Hourly · 1/session"), ("frequency daily", "Daily · 1/session")]
        elif view == "advanced":
            entries = [("view " + k, v) for k, v in {
                "monitors": "👀 Task monitors · Guide", "watcher": "📬 Inbox watcher · Paid",
                "judges": "⚖️ Judges · Unavailable", "repairs": "🛠 Repair · Unavailable",
                "scope": "🔒 Scope / limits · Info", "routines": "📅 Routines · Guide",
                "desktop": "🖥 Capture · Unavailable"}.items()]
        elif view in ADVANCED_ENTRIES or view == "recommended":
            entries = []
        else:
            entries = [("view " + k, f"{label} · {'On' if c[k] else 'Off'}") for k, label in
                       [("recall", "🧠 Recall"), ("suggestions", "💡 Suggestions"),
                        ("skill-drafts", "📚 Skill drafts"), ("audit", "🧾 Context checks")]]
            entries += [("view timing", "🕓 Timing · " + {"after_reply": "After reply", "never": "Never"}.get(c['timing'], "Not set")),
                        ("view frequency", "⏱ Frequency · Set"),
                        ("view advanced", "⚙️ Advanced / costs"),
                        ("recommended", "✨ Use recommended")]
        if view != "basic":
            entries.append(("view advanced" if view in ADVANCED_ENTRIES else "view basic", "‹ Back"))
        entries += [("save", "💾 Save"), ("cancel", "✖️ Cancel")]
        return [{"value": value, "label": label} for value, label in entries]

    def save(self) -> str:
        try:
            with config_api._CONFIG_LOCK:
                raw = config_api.require_readable_config_before_write()
                if raw.get("proactive", {}) != self.baseline:
                    return "Not saved: preferences changed elsewhere. Open Customize again."
                updated = copy.deepcopy(raw)
                updated["proactive"] = self.root
                # Saving a choice marks customization; later On must not fill
                # previously absent leaves with a new preset.
                updated["proactive"]["mode"] = "active"
                return _persist(raw, updated)
        except Exception:
            return FAILED


def proactive_command(args: str = "", *, can_change: bool, show_commands: bool = True,
                      expected: dict | None = None) -> str:
    """Text parity: validated single choices save immediately, never invoke a model."""
    action = args.strip().lower()
    parts = action.split()
    mutation = action in {"on", "off", "enable", "disable", "recommended"} or (
        len(parts) == 2 and parts[0] in {*FIELDS, "frequency"})
    if mutation and not can_change:
        return LOCKED + "\n/proactive status — permission details."
    note = ""
    try:
        if action in {*ADVANCED_ENTRIES, "advanced"}:
            return advanced_info(action, config_api.load_config(migrate=False).get("proactive", {}))
        if action == "customize":
            draft = ProactiveDraft.open()
            return draft.render() + "\n" + _describe(draft.root) + "\n" + (HELP + "\n" + OPTIONAL if can_change else LOCKED)
        if action in {"recommended"} or (len(parts) == 2 and parts[0] in {*FIELDS, "frequency"}):
            draft = ProactiveDraft.open()
            if action == "recommended":
                draft.recommended()
            else:
                try:
                    draft.set(*parts)
                except ValueError:
                    return MIGRATION
            note = draft.save() + "\n"
        elif action in {"on", "off", "enable", "disable"}:
            with config_api._CONFIG_LOCK:
                raw = config_api.require_readable_config_before_write()
                if expected is not None and raw.get("proactive", {}) != expected:
                    return "Not saved: preferences changed elsewhere. Run /proactive again."
                updated = copy.deepcopy(raw)
                root = updated.setdefault("proactive", {})
                _validate(root)
                enabled = action in {"on", "enable"}
                if enabled and load_settings(updated).mode != "active":
                    root["mode"] = "active"
                    # An existing section represents saved choices, including
                    # omitted leaves whose old defaults must not be reinterpreted.
                    for section, values in RECOMMENDED.items():
                        if section not in root:
                            root[section] = copy.deepcopy(values)
                root["enabled"] = enabled
                note = _persist(raw, updated) + "\n"
        elif action not in {"", "status"}:
            return MIGRATION
        saved = _raw().get("proactive", {})
        effective_root = config_api.load_config(migrate=False).get("proactive", {})
        _validate(effective_root)
        persisted, effective = load_settings({"proactive": saved}), load_settings({"proactive": effective_root})
    except Exception:
        return FAILED
    saved_state, effective_state = ("On" if persisted.enabled else "Off"), ("On" if effective.enabled else "Off")
    text = f"Proactive: {effective_state}\n" + note
    if persisted != effective or _choices(saved) != _choices(effective_root):
        text += f"Saved: {saved_state} ({persisted.mode}); effective: {effective_state} ({effective.mode}, managed policy).\n"
        text += "Saved choices: " + _describe(saved) + "\n"
    elif effective.enabled and not effective.active:
        text += f"Mode: {effective.mode} (not active).\n"
    if action == "status":
        text += (f"Effective settings: {effective_state} (mode: {effective.mode}); "
                 "in-flight turns keep their current context. No restart required.\n")
    if action or effective.enabled:
        text += _describe(effective_root) + "\n"
    else:
        text += PRESET_SUMMARY + "\n"
    if not action:
        text += "On resumes saved choices; Off keeps them.\n"
    if action in {"off", "disable"}:
        text += "Independent schedules/loops keep running.\n"
    if action == "status":
        text += ("No new jobs, external-action permission or extra model-call opt-in. "
                 "Existing schedules and explicit advanced opt-ins stay unchanged.\n" + OPTIONAL + "\n")
    if can_change:
        if show_commands:
            text += PROACTIVE_ACTIONS + "\n"
        text += "/proactive status — Details"
    else:
        text += LOCKED + "\n/proactive status — permission details."
    return text
