"""Observed, titled state for delegated subagents — pure tracker + renderer.

Consumes the ``subagent.*`` events that ``tools.delegate_tool`` relays to the
parent's ``tool_progress_callback`` and folds them into one truthful state
per child, grouped by delegation (one ``delegate_task`` call). Rendering is a
pure function of that state, so every surface that wants a roster (gateway
cards today) shows the same thing.

Contract:

* **Observed state only.** Every transition comes from a relayed event or an
  explicit liveness probe of the in-process child registry. No percentages,
  no guessed progress. Terminal states are final: late or replayed events
  cannot regress them.
* **No private reasoning.** Only notes the relay marks ``note_kind="note"``
  (the child's visible interim content, with any inline reasoning blocks
  removed) are shown. ``reasoning.available`` text and spinner chatter are
  never rendered.
* **Redact, then truncate.** All free text passes through
  ``agent.redact.redact_sensitive_text(force=True)`` before it is shortened;
  tool *output* is never rendered — only the outcome (ok / failed, duration).
* **Out of band.** Nothing here touches messages, prompts or toolsets, so
  prompt caching is unaffected.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

STATES = (
    "queued",
    "starting",
    "running",
    "tool-running",
    "waiting",
    "blocked",
    "completed",
    "failed",
    "cancelled",
)
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
_ACTIVE_STATES = frozenset({"starting", "running", "tool-running", "waiting", "blocked"})

_ICONS = {
    "queued": "⏳",
    "starting": "🚀",
    "running": "▶️",
    "tool-running": "🔧",
    "waiting": "⌛",
    "blocked": "⚠️",
    "completed": "✅",
    "failed": "❌",
    "cancelled": "⏹",
}

# delegate_task / _run_single_child completion statuses -> lifecycle state.
_COMPLETE_STATUS = {
    "completed": "completed",
    "ok": "completed",
    "success": "completed",
    "interrupted": "cancelled",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "failed": "failed",
    "error": "failed",
    "timeout": "failed",
}

_TITLE_MAX = 60
_NOTE_MAX = 110
_TOOL_MAX = 72
_REASON_MAX = 120
_SUMMARY_MAX = 160
_MAX_DETAILED_CHILDREN = 8
_CARD_MAX_CHARS = 3500

_REASONING_TAGS = r"(?:think|thinking|reasoning|reflection|REASONING_SCRATCHPAD)"
_REASONING_BLOCK_RE = re.compile(
    rf"<({_REASONING_TAGS})\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_REASONING_OPEN_RE = re.compile(rf"<{_REASONING_TAGS}\b[^>]*>", re.IGNORECASE)
_REASONING_STRAY_RE = re.compile(rf"</?{_REASONING_TAGS}\b[^>]*>", re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")

# Tool-argument keys, in priority order, that make a good one-line summary.
_ARG_SUMMARY_KEYS = (
    "command",
    "path",
    "file_path",
    "url",
    "query",
    "pattern",
    "name",
    "goal",
    "action",
)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _redact(text: str) -> str:
    if not text:
        return ""
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True) or ""
    except Exception:  # pragma: no cover - core module; never leak on failure
        return "[withheld: redaction unavailable]"


def sanitize_text(value: Any, limit: int) -> str:
    """Redact secrets, collapse to one line, neutralise code fences, truncate."""
    text = _redact(str(value or ""))
    text = " ".join(text.split())
    # Backticks would break the card's own code spans on markdown surfaces.
    text = text.replace("`", "'")
    if limit > 0 and len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def _truncate_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut[limit // 2 :]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "…"


def derive_task_title(goal: Any, explicit: Any = None, limit: int = _TITLE_MAX) -> str:
    """Short human-readable title: the caller's ``title`` or the goal's lead."""
    raw = str(explicit or "").strip() or str(goal or "").strip()
    if not raw:
        return "Untitled task"
    raw = _redact(raw)
    first_line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    lead = _SENTENCE_END_RE.split(first_line, 1)[0].strip()
    lead = " ".join(lead.split()).replace("`", "'")
    if len(lead) <= limit:
        lead = lead.rstrip(".")
    return _truncate_words(lead, limit) or "Untitled task"


def extract_progress_note(content: Any) -> str:
    """First line of a child's visible content with reasoning blocks removed.

    Complete ``<think>…</think>``-style blocks are dropped; an unterminated
    opener withholds everything after it; stray tags are stripped.
    """
    text = str(content or "")
    text = _REASONING_BLOCK_RE.sub("", text)
    opener = _REASONING_OPEN_RE.search(text)
    if opener:
        text = text[: opener.start()]
    text = _REASONING_STRAY_RE.sub("", text)
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def summarize_tool_call(tool_name: Any, preview: Any, args: Any, limit: int = _TOOL_MAX) -> str:
    """One-line, redacted summary of a tool call's primary argument."""
    candidate = ""
    if isinstance(args, dict):
        for key in _ARG_SUMMARY_KEYS:
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                candidate = val
                break
    if not candidate and preview:
        candidate = str(preview)
    lines = [ln for ln in candidate.splitlines() if ln.strip()]
    if not lines:
        return ""
    first = lines[0].strip()
    if len(lines) > 1:
        first += " …"
    return sanitize_text(first, limit)


def format_duration(seconds: Optional[float]) -> str:
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class ChildActivity:
    key: str
    subagent_id: Optional[str]
    index: int
    count: int
    title: str
    model: str = ""
    provider: str = ""
    depth: int = 0
    state: str = "queued"
    reason: Optional[str] = None
    first_seen: float = 0.0
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    last_event_at: float = 0.0
    current_tool: Optional[str] = None
    tool_summary: str = ""
    tool_started_at: Optional[float] = None
    tools_ok: int = 0
    tools_failed: int = 0
    last_tool_result: Optional[str] = None
    note: Optional[str] = None
    summary: Optional[str] = None
    files_written: List[str] = field(default_factory=list)
    duration_s: Optional[float] = None
    alerted: Set[str] = field(default_factory=set)

    @property
    def started(self) -> bool:
        return self.started_at is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subagent_id": self.subagent_id,
            "index": self.index,
            "title": self.title,
            "model": self.model,
            "provider": self.provider,
            "state": self.state,
            "reason": self.reason,
            "current_tool": self.current_tool,
            "tool_summary": self.tool_summary,
            "tools_ok": self.tools_ok,
            "tools_failed": self.tools_failed,
            "note": self.note,
            "summary": self.summary,
            "files_written": list(self.files_written),
            "duration_s": self.duration_s,
        }


@dataclass
class DelegationGroup:
    group_id: str
    created_at: float
    children: Dict[str, ChildActivity] = field(default_factory=dict)

    def ordered(self) -> List[ChildActivity]:
        return sorted(self.children.values(), key=lambda c: (c.index, c.key))

    @property
    def finished(self) -> bool:
        return bool(self.children) and all(
            c.state in TERMINAL_STATES for c in self.children.values()
        )


LivenessProbe = Callable[[str], Optional[Dict[str, Any]]]


class DelegationActivityTracker:
    """Thread-safe fold of relayed ``subagent.*`` events into per-child state."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        heartbeat_seconds: float = 60.0,
        stall_seconds: float = 600.0,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._wall_clock = wall_clock
        self.heartbeat_seconds = max(0.0, float(heartbeat_seconds or 0))
        self.stall_seconds = max(1.0, float(stall_seconds or 600))
        self._lock = threading.RLock()
        self._groups: Dict[str, DelegationGroup] = {}

    # -- queries ---------------------------------------------------------

    def snapshot(self, group_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            group = self._groups.get(group_id)
            return [c.to_dict() for c in group.ordered()] if group else []

    def group_ids(self) -> List[str]:
        with self._lock:
            return list(self._groups)

    def group_finished(self, group_id: str) -> bool:
        with self._lock:
            group = self._groups.get(group_id)
            return bool(group and group.finished)

    def active_group_ids(self) -> List[str]:
        with self._lock:
            return [gid for gid, g in self._groups.items() if not g.finished]

    # -- event folding ---------------------------------------------------

    def observe(
        self,
        event_type: Any,
        tool_name: Any = None,
        preview: Any = None,
        args: Any = None,
        **kw: Any,
    ) -> Tuple[Optional[str], List[str]]:
        """Apply one relayed event. Returns ``(changed_group_id, alerts)``."""
        et = str(event_type or "")
        if not et.startswith("subagent."):
            return None, []
        now = self._clock()
        with self._lock:
            group_id = str(kw.get("delegation_id") or "delegation")
            group = self._groups.get(group_id)
            if group is None:
                if et in {"subagent.complete", "subagent.progress", "subagent.text"}:
                    # A completion for a group we never saw start carries no
                    # truthful roster to show; don't open a card for it.
                    if et != "subagent.complete":
                        return None, []
                group = DelegationGroup(group_id=group_id, created_at=now)
                self._groups[group_id] = group
            child = self._child_for(group, kw, now)
            self._refresh_identity(child, kw)
            if child.state in TERMINAL_STATES:
                return None, []  # final: late/replayed events cannot regress it
            alerts: List[str] = []
            changed = self._apply(child, et, tool_name, preview, args, kw, now, alerts)
            if changed:
                child.last_event_at = now
                return group_id, alerts
            return None, alerts

    def _child_for(self, group: DelegationGroup, kw: Dict[str, Any], now: float) -> ChildActivity:
        sid = kw.get("subagent_id")
        index = _as_int(kw.get("task_index"), 0)
        key = str(sid) if sid else f"idx{index}"
        child = group.children.get(key)
        if child is None:
            child = ChildActivity(
                key=key,
                subagent_id=str(sid) if sid else None,
                index=index,
                count=max(1, _as_int(kw.get("task_count"), 1)),
                title=derive_task_title(kw.get("goal"), kw.get("title")),
                first_seen=now,
                last_event_at=now,
            )
            group.children[key] = child
        return child

    @staticmethod
    def _refresh_identity(child: ChildActivity, kw: Dict[str, Any]) -> None:
        title = kw.get("title")
        if title:
            child.title = derive_task_title(kw.get("goal"), title)
        if kw.get("model") and not child.model:
            child.model = sanitize_text(kw["model"], 48)
        if kw.get("provider") and not child.provider:
            child.provider = sanitize_text(kw["provider"], 32)
        if kw.get("depth") is not None:
            child.depth = max(0, _as_int(kw.get("depth"), 0))
        if kw.get("task_count"):
            child.count = max(1, _as_int(kw.get("task_count"), child.count))

    def _start(self, child: ChildActivity, now: float) -> None:
        if child.started_at is None:
            child.started_at = now
        if child.state == "queued":
            child.state = "starting"

    def _apply(
        self,
        child: ChildActivity,
        et: str,
        tool_name: Any,
        preview: Any,
        args: Any,
        kw: Dict[str, Any],
        now: float,
        alerts: List[str],
    ) -> bool:
        if et == "subagent.queued":
            return child.started_at is None and child.first_seen == now
        if et == "subagent.start":
            if child.started:
                return False  # replayed start
            self._start(child, now)
            child.reason = None
            return True
        if et == "subagent.thinking":
            if kw.get("note_kind") != "note":
                return False  # reasoning / spinner chatter: never shown
            note = sanitize_text(extract_progress_note(preview or tool_name), _NOTE_MAX)
            if not note:
                return False
            self._start(child, now)
            child.note = note
            if child.state != "tool-running":
                child.state = "running"
                child.reason = None
            return True
        if et == "subagent.tool":
            self._start(child, now)
            child.state = "tool-running"
            child.reason = None
            child.current_tool = sanitize_text(tool_name or "tool", 40)
            child.tool_summary = summarize_tool_call(tool_name, preview, args)
            child.tool_started_at = now
            return True
        if et == "subagent.tool_done":
            self._start(child, now)
            name = sanitize_text(tool_name or child.current_tool or "tool", 40)
            dur = _as_float(kw.get("duration_seconds"))
            failed = bool(kw.get("is_error"))
            if failed:
                child.tools_failed += 1
            else:
                child.tools_ok += 1
            child.last_tool_result = (
                f"{name} {'failed' if failed else 'ok'}"
                + (f" in {format_duration(dur)}" if dur is not None else "")
            )
            child.current_tool = None
            child.tool_summary = ""
            child.tool_started_at = None
            child.state = "running"
            child.reason = None
            return True
        if et == "subagent.complete":
            self._complete(child, preview, kw, now, alerts)
            return True
        return False  # subagent.progress / subagent.text / unknown: no state

    def _complete(
        self,
        child: ChildActivity,
        preview: Any,
        kw: Dict[str, Any],
        now: float,
        alerts: List[str],
    ) -> None:
        status = str(kw.get("status") or "completed").strip().lower()
        state = _COMPLETE_STATUS.get(status, "failed")
        child.state = state
        child.ended_at = now
        child.current_tool = None
        child.tool_summary = ""
        dur = _as_float(kw.get("duration_seconds"))
        child.duration_s = dur
        if child.started_at is None:
            child.started_at = now - (dur or 0.0)
        summary_raw = kw.get("summary") or preview or ""
        if state == "completed":
            child.reason = None
            child.summary = sanitize_text(summary_raw, _SUMMARY_MAX) or None
        elif state == "cancelled":
            child.reason = "stopped before finishing"
        elif status == "timeout":
            child.reason = f"timed out after {format_duration(dur)}"
        else:
            child.reason = sanitize_text(summary_raw, _REASON_MAX) or "failed"
        files = kw.get("files_written") or []
        if isinstance(files, (list, tuple)):
            child.files_written = [
                sanitize_text(str(p).replace("\\", "/").rsplit("/", 1)[-1], 40)
                for p in files[:20]
                if p
            ]
        if state in {"failed", "cancelled"}:
            self._alert(child, state, alerts)

    # -- heartbeat -------------------------------------------------------

    def tick(
        self, probe: Optional[LivenessProbe] = None
    ) -> Tuple[Set[str], List[Tuple[str, str]]]:
        """Re-classify quiet children from observed liveness.

        Returns ``(groups_whose_state_changed, [(group_id, alert), ...])``.
        """
        now = self._clock()
        changed: Set[str] = set()
        alerts: List[Tuple[str, str]] = []
        with self._lock:
            for gid, group in self._groups.items():
                for child in group.children.values():
                    if child.state in TERMINAL_STATES or not child.started:
                        continue
                    raised: List[str] = []
                    if self._classify(child, now, probe, raised):
                        changed.add(gid)
                    alerts.extend((gid, text) for text in raised)
        return changed, alerts

    def seconds_since_last_event(self, group_id: str) -> Optional[float]:
        now = self._clock()
        with self._lock:
            group = self._groups.get(group_id)
            if not group or not group.children:
                return None
            return min(now - c.last_event_at for c in group.children.values())

    def _classify(
        self,
        child: ChildActivity,
        now: float,
        probe: Optional[LivenessProbe],
        alerts: List[str],
    ) -> bool:
        info: Optional[Dict[str, Any]] = None
        if probe is not None and child.subagent_id:
            try:
                info = probe(child.subagent_id)
            except Exception:
                info = None
        quiet = now - child.last_event_at
        grace = max(self.heartbeat_seconds, 30.0)
        if info is not None and info.get("registered") is False and quiet >= grace:
            # Started, never reported completion, and no longer in the live
            # registry: the worker is gone (crash, process restart).
            child.state = "failed"
            child.reason = "worker ended without a completion report"
            child.ended_at = now
            child.current_tool = None
            self._alert(child, "failed", alerts)
            return True
        if child.state == "tool-running":
            return False  # an in-flight tool is observed work, not a stall
        activity_age = _as_float((info or {}).get("seconds_since_activity"))
        idle = quiet if activity_age is None else min(quiet, activity_age)
        before = (child.state, child.reason)
        if idle >= self.stall_seconds:
            desc = sanitize_text((info or {}).get("activity") or "", 60)
            child.state = "blocked"
            child.reason = f"no activity observed for {format_duration(idle)}" + (
                f" (last: {desc})" if desc else ""
            )
            self._alert(child, "blocked", alerts)
        elif self.heartbeat_seconds and quiet >= self.heartbeat_seconds:
            tool = (info or {}).get("current_tool")
            child.state = "waiting"
            child.reason = (
                f"waiting on {sanitize_text(tool, 40)}" if tool else "waiting for model response"
            )
        return (child.state, child.reason) != before

    # -- alerts ----------------------------------------------------------

    def _alert(self, child: ChildActivity, kind: str, alerts: List[str]) -> None:
        if kind in child.alerted:
            return
        child.alerted.add(kind)
        label = f"Subagent #{child.index + 1} “{child.title}”"
        elapsed = format_duration(
            child.duration_s
            if child.duration_s is not None
            else (child.ended_at or self._clock()) - (child.started_at or child.first_seen)
        )
        if kind == "cancelled":
            alerts.append(f"⏹ {label} was cancelled after {elapsed}.")
        elif kind == "blocked":
            alerts.append(
                f"⚠️ {label} looks stalled: {child.reason}. It is still running; nothing was stopped."
            )
        else:
            alerts.append(f"❌ {label} failed after {elapsed}: {child.reason}")

    # -- rendering -------------------------------------------------------

    def render(self, group_id: str) -> str:
        now = self._clock()
        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                return ""
            try:
                stamp = time.strftime("%H:%M %Z", time.localtime(self._wall_clock())).strip()
            except Exception:
                stamp = ""
            return _render_group(group, now, stamp)


def _state_counts(children: List[ChildActivity]) -> str:
    active = sum(1 for c in children if c.state in _ACTIVE_STATES)
    queued = sum(1 for c in children if c.state == "queued")
    done = sum(1 for c in children if c.state == "completed")
    failed = sum(1 for c in children if c.state == "failed")
    cancelled = sum(1 for c in children if c.state == "cancelled")
    parts = []
    if active:
        parts.append(f"{active} active")
    if queued:
        parts.append(f"{queued} queued")
    if done:
        parts.append(f"{done} reported done")
    if failed:
        parts.append(f"{failed} failed")
    if cancelled:
        parts.append(f"{cancelled} cancelled")
    return " · ".join(parts)


def _render_child(child: ChildActivity, now: float, detailed: bool) -> List[str]:
    icon = _ICONS.get(child.state, "•")
    head = f"{icon} #{child.index + 1} {child.title}"
    if child.state == "queued":
        return [f"{head} · queued"]
    ident = " · ".join(p for p in (child.model, child.provider) if p) or "model unknown"
    tools = child.tools_ok + child.tools_failed + (1 if child.current_tool else 0)
    tool_bit = f"{tools} tool{'s' if tools != 1 else ''}" if tools else ""
    if child.tools_failed:
        tool_bit += f" ({child.tools_failed} failed)"
    started = child.started_at if child.started_at is not None else child.first_seen

    if child.state in TERMINAL_STATES:
        dur = format_duration(
            child.duration_s if child.duration_s is not None else (child.ended_at or now) - started
        )
        verb = {
            "completed": f"reported done in {dur}",
            "failed": f"failed after {dur}",
            "cancelled": f"cancelled after {dur}",
        }[child.state]
        lines = [head, "   " + " · ".join(p for p in (ident, verb, tool_bit) if p)]
        if child.state == "completed":
            if child.summary:
                lines.append(f"   ↳ {child.summary}")
            if child.files_written:
                shown = ", ".join(child.files_written[:3])
                more = len(child.files_written) - 3
                lines.append(
                    f"   📄 {len(child.files_written)} file"
                    f"{'s' if len(child.files_written) != 1 else ''}: {shown}"
                    + (f" +{more}" if more > 0 else "")
                )
            lines.append("   awaiting parent review")
        elif child.reason:
            lines.append(f"   ↳ {child.reason}")
        return lines

    label = {
        "starting": "starting",
        "running": "running",
        "tool-running": "running a tool",
        "waiting": "waiting",
        "blocked": "blocked",
    }.get(child.state, child.state)
    lines = [
        head,
        "   " + " · ".join(p for p in (ident, f"{label} {format_duration(now - started)}", tool_bit) if p),
    ]
    if not detailed:
        return lines
    if child.current_tool:
        tool_line = f"   🔧 {child.current_tool}"
        if child.tool_summary:
            tool_line += f": `{child.tool_summary}`"
        if child.tool_started_at is not None:
            tool_line += f" · {format_duration(now - child.tool_started_at)}"
        lines.append(tool_line)
    elif child.last_tool_result:
        lines.append(f"   last: {child.last_tool_result}")
    if child.reason and child.state in {"waiting", "blocked"}:
        lines.append(f"   ↳ {child.reason}")
    if child.note:
        lines.append(f"   📝 {child.note}")
    return lines


def _render_group(group: DelegationGroup, now: float, stamp: str = "") -> str:
    children = group.ordered()
    finished = group.finished
    elapsed = format_duration(
        (max((c.ended_at or now) for c in children) if finished and children else now)
        - group.created_at
    )
    header = "🔀 Subagents" + (" · finished" if finished else "")
    counts = _state_counts(children)
    lines = [" · ".join(p for p in (header, counts, elapsed) if p)]
    detailed_budget = _MAX_DETAILED_CHILDREN
    hidden_terminal = 0
    for child in children:
        if detailed_budget <= 0 and child.state in TERMINAL_STATES:
            hidden_terminal += 1
            continue
        lines.extend(_render_child(child, now, detailed=detailed_budget > 0))
        detailed_budget -= 1
    if hidden_terminal:
        lines.append(f"… +{hidden_terminal} more finished")
    # The timestamp makes a card frozen by a gateway restart visibly stale.
    footer = "child activity only · the main agent's own steps are separate"
    lines.append(f"{footer} · updated {stamp}" if stamp else footer)
    text = "\n".join(lines)
    if len(text) > _CARD_MAX_CHARS:
        text = text[: _CARD_MAX_CHARS - 1].rsplit("\n", 1)[0] + "\n…"
    return text
