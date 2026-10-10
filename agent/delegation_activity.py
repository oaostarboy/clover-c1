"""Observed, titled activity of delegated agents — pure tracker + renderer.

Consumes ``subagent.*`` events — relayed by ``tools.delegate_tool`` for
in-process children, or synthesized by ``tools.agent_job_observer`` for
explicitly registered external agent CLIs — and folds them into one truthful
state per worker plus a short public activity feed, grouped by delegation.

Rendering is activity-first, not a roll call: a counts-only header, one
"now" line per *active* worker, and the latest attributed events (tool
results with rapid repeats coalesced, public notes). Finished workers leave
the live view; their result is raised once (finding / alert) and kept for the
final summary the card becomes when every worker is done.

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
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

PUBLIC_ACTIVITY_STATUS = {
    "requesting": ("waiting", "requesting provider"),
    "waiting": ("waiting", "waiting for provider"),
    "retrying": ("waiting", "retrying provider"),
    "provider_result": ("running", "provider result received"),
    "awaiting_input": ("blocked", "awaiting input"),
}
# Provider round-trip reasons. Every model call relays them, so they must
# never replace a known current action on the card (C1.3 f): they only fill
# the slot before the first tool/note, and "retrying" is appended as a
# distinct problem signal.
_PROVIDER_WAIT_REASONS = frozenset(
    PUBLIC_ACTIVITY_STATUS[s][1] for s in ("requesting", "waiting", "retrying")
)
_PROVIDER_RETRY_REASON = PUBLIC_ACTIVITY_STATUS["retrying"][1]

STATES = (
    "queued",
    "starting",
    "running",
    "tool-running",
    "waiting",
    "blocked",
    "completed",
    "incomplete",
    "failed",
    "cancelled",
)
TERMINAL_STATES = frozenset({"completed", "incomplete", "failed", "cancelled"})
_ACTIVE_STATES = frozenset({"starting", "running", "tool-running", "waiting", "blocked"})

_ICONS = {
    "queued": "⏳",
    "starting": "🚀",
    "running": "▶️",
    "tool-running": "🔧",
    "waiting": "⌛",
    "blocked": "⚠️",
    "completed": "✅",
    # Ran out of steps (turn/iteration cap): not a crash, so never a red X.
    "incomplete": "⏳",
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
    "incomplete": "incomplete",
    "max_turns": "incomplete",
    "max_iterations": "incomplete",
}

_TITLE_MAX = 60
_NOTE_MAX = 110
_TOOL_MAX = 72
_REASON_MAX = 120
_SUMMARY_MAX = 600
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
    "description",
    "action",
)


# ---------------------------------------------------------------------------
# Turn-scoped activity sink
# ---------------------------------------------------------------------------

# The gateway binds its per-turn DelegationActivityPublisher here inside the
# turn's copied context, so tools running for that turn (e.g. a terminal
# registering an external agent job) reach exactly that chat's publisher and
# nothing else. Unset everywhere else (CLI, cron, tests) -> None.
_ACTIVITY_SINK: ContextVar[Any] = ContextVar("clover_delegation_activity_sink", default=None)


def bind_activity_sink(sink: Any) -> Any:
    """Bind ``sink`` for the current context; returns the reset token."""
    return _ACTIVITY_SINK.set(sink)


def current_activity_sink() -> Any:
    return _ACTIVITY_SINK.get()


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

_FEED_KEEP = 40            # activity entries retained per group
_FEED_SHOWN = 5            # recent activity lines rendered while active
_MAX_ACTIVE_LINES = 6      # "now" lines rendered while active
_COALESCE_WINDOW = 30.0    # seconds: repeated same-tool calls merge in the feed
_FEED_TITLE_MAX = 32
_MODEL_MAX = 24


@dataclass
class OpenTool:
    call_id: str
    name: str
    summary: str
    started_at: float


@dataclass
class FeedEntry:
    """One public, attributed activity line (a finished tool or a note)."""

    child_key: str
    kind: str                 # "tool" | "note"
    at: float
    tool: str = ""
    summary: str = ""
    text: str = ""
    count: int = 1
    ok: Optional[bool] = None  # tool outcome; None = unknown
    duration: Optional[float] = None


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
    # "tools" = tool-level events are observable; "lifecycle" = only
    # start/exit/output growth (plain external CLI output). Rendered so the
    # card never implies tool visibility it does not have.
    visibility: str = "tools"
    state: str = "queued"
    reason: Optional[str] = None
    first_seen: float = 0.0
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    last_event_at: float = 0.0
    open_tools: Dict[str, OpenTool] = field(default_factory=dict)
    tools_ok: int = 0
    tools_failed: int = 0
    last_tool_result: Optional[str] = None
    note: Optional[str] = None
    # Last thing the worker visibly did (tool phrase or progress note), shown
    # while it waits on the provider between actions.
    last_action: Optional[str] = None
    summary: Optional[str] = None
    files_written: List[str] = field(default_factory=list)
    duration_s: Optional[float] = None
    alerted: Set[str] = field(default_factory=set)
    _auto_call: int = 0

    @property
    def started(self) -> bool:
        return self.started_at is not None

    @property
    def current_tool(self) -> Optional[str]:
        latest = self._latest_open()
        return latest.name if latest else None

    @property
    def tool_summary(self) -> str:
        latest = self._latest_open()
        return latest.summary if latest else ""

    def _latest_open(self) -> Optional[OpenTool]:
        latest: Optional[OpenTool] = None
        for tool in self.open_tools.values():  # insertion order breaks ties
            if latest is None or tool.started_at >= latest.started_at:
                latest = tool
        return latest

    @property
    def label(self) -> str:
        title = _truncate_words(self.title, _FEED_TITLE_MAX)
        model = _short_model(self.model)
        return f"#{self.index + 1} {title}" + (f" · {model}" if model else "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subagent_id": self.subagent_id,
            "index": self.index,
            "title": self.title,
            "model": self.model,
            "provider": self.provider,
            "visibility": self.visibility,
            "state": self.state,
            "reason": self.reason,
            "current_tool": self.current_tool,
            "tool_summary": self.tool_summary,
            "open_tools": len(self.open_tools),
            "tools_ok": self.tools_ok,
            "tools_failed": self.tools_failed,
            "note": self.note,
            "summary": self.summary,
            "files_written": list(self.files_written),
            "duration_s": self.duration_s,
        }


def _close_open_tools(child: "ChildActivity") -> None:
    """A tool still running when its worker ends never finished: count it
    as failed so the done header never shows fewer calls than the live one."""
    child.tools_failed += len(child.open_tools)
    child.open_tools.clear()


def _short_model(model: str) -> str:
    text = (model or "").strip()
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return _truncate_words(text, _MODEL_MAX) if text else ""


@dataclass
class DelegationGroup:
    group_id: str
    created_at: float
    children: Dict[str, ChildActivity] = field(default_factory=dict)
    feed: List[FeedEntry] = field(default_factory=list)

    def ordered(self) -> List[ChildActivity]:
        return sorted(self.children.values(), key=lambda c: (c.index, c.key))

    @property
    def finished(self) -> bool:
        return bool(self.children) and all(
            c.state in TERMINAL_STATES for c in self.children.values()
        )

    def push(self, entry: FeedEntry) -> None:
        self.feed.append(entry)
        if len(self.feed) > _FEED_KEEP:
            del self.feed[: len(self.feed) - _FEED_KEEP]


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

    def feed_snapshot(self, group_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            group = self._groups.get(group_id)
            if not group:
                return []
            return [
                {"child": e.child_key, "kind": e.kind, "tool": e.tool,
                 "summary": e.summary, "text": e.text, "count": e.count, "ok": e.ok}
                for e in group.feed
            ]

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
                if et in {"subagent.progress", "subagent.text"}:
                    return None, []
                group = DelegationGroup(group_id=group_id, created_at=now)
                self._groups[group_id] = group
            child = self._child_for(group, kw, now)
            self._refresh_identity(child, kw)
            if child.state in TERMINAL_STATES:
                return None, []  # final: late/replayed events cannot regress it
            alerts: List[str] = []
            changed = self._apply(group, child, et, tool_name, preview, args, kw, now, alerts)
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
        if kw.get("visibility") in {"tools", "lifecycle"}:
            child.visibility = kw["visibility"]

    def _start(self, child: ChildActivity, now: float) -> None:
        if child.started_at is None:
            child.started_at = now
        if child.state == "queued":
            child.state = "starting"

    def _settle_state(self, child: ChildActivity) -> None:
        child.state = "tool-running" if child.open_tools else "running"
        child.reason = None

    def _apply(
        self,
        group: DelegationGroup,
        child: ChildActivity,
        et: str,
        tool_name: Any,
        preview: Any,
        args: Any,
        kw: Dict[str, Any],
        now: float,
        alerts: List[str],
    ) -> bool:
        if et == "subagent.progress" and kw.get("activity_status") in PUBLIC_ACTIVITY_STATUS:
            state, reason = PUBLIC_ACTIVITY_STATUS[kw["activity_status"]]
            if (child.state, child.reason) == (state, reason):
                return False
            self._start(child, now)
            child.state, child.reason = state, reason
            return True
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
            if not note or note == child.note:
                return False
            self._start(child, now)
            child.note = note
            child.last_action = f"💬 {note}"
            group.push(FeedEntry(child_key=child.key, kind="note", at=now, text=note))
            self._settle_state(child)
            return True
        if et == "subagent.tool":
            self._start(child, now)
            name = sanitize_text(tool_name or "tool", 40)
            call_id = str(kw.get("tool_call_id") or "")
            if call_id and call_id in child.open_tools:
                return False  # duplicate start for an already-open call
            if not call_id:
                child._auto_call += 1
                call_id = f"auto{child._auto_call}"
            child.open_tools[call_id] = OpenTool(
                call_id=call_id,
                name=name,
                summary=summarize_tool_call(tool_name, preview, args),
                started_at=now,
            )
            child.last_action = f"🔧 {_tool_phrase(name, child.open_tools[call_id].summary)}"
            self._settle_state(child)
            return True
        if et == "subagent.tool_done":
            call_id = kw.get("tool_call_id")
            if call_id and str(call_id) not in child.open_tools:
                return False  # duplicate / replayed result for a closed call
            self._start(child, now)
            opened = self._pop_open_tool(child, tool_name, call_id)
            name = opened.name if opened else sanitize_text(tool_name or "tool", 40)
            dur = _as_float(kw.get("duration_seconds"))
            if dur is None and opened is not None:
                dur = max(0.0, now - opened.started_at)
            failed = bool(kw.get("is_error"))
            if failed:
                child.tools_failed += 1
            else:
                child.tools_ok += 1
            child.last_tool_result = (
                f"{name} {'failed' if failed else 'ok'}"
                + (f" in {format_duration(dur)}" if dur is not None else "")
            )
            self._record_tool(group, child, name, opened.summary if opened else "",
                              not failed, dur, now)
            if opened is not None:
                child.last_action = f"🔧 {_tool_phrase(name, opened.summary)}"
            self._settle_state(child)
            return True
        if et == "subagent.complete":
            self._complete(group, child, preview, kw, now, alerts)
            return True
        return False  # subagent.progress / subagent.text / unknown: no state

    @staticmethod
    def _pop_open_tool(child: ChildActivity, tool_name: Any, call_id: Any) -> Optional[OpenTool]:
        if call_id and str(call_id) in child.open_tools:
            return child.open_tools.pop(str(call_id))
        if tool_name:
            name = sanitize_text(tool_name, 40)
            same = [t for t in child.open_tools.values() if t.name == name]
            if same:
                oldest = min(same, key=lambda t: t.started_at)
                return child.open_tools.pop(oldest.call_id)
        if child.open_tools:
            latest = child._latest_open()
            return child.open_tools.pop(latest.call_id) if latest else None
        return None

    @staticmethod
    def _record_tool(group: DelegationGroup, child: ChildActivity, name: str,
                     summary: str, ok: bool, dur: Optional[float], now: float) -> None:
        last = group.feed[-1] if group.feed else None
        if (
            last is not None
            and last.kind == "tool"
            and last.child_key == child.key
            and last.tool == name
            and last.ok is True
            and ok
            and now - last.at <= _COALESCE_WINDOW
        ):
            # Rapid repeats of the same successful tool collapse into one
            # line ("read_file ×4 'last.py'") instead of a scrolling list.
            last.count += 1
            last.summary = summary or last.summary
            last.at = now
            last.duration = dur
            return
        group.push(FeedEntry(child_key=child.key, kind="tool", at=now, tool=name,
                             summary=summary, ok=ok, duration=dur))

    def _complete(
        self,
        group: DelegationGroup,
        child: ChildActivity,
        preview: Any,
        kw: Dict[str, Any],
        now: float,
        alerts: List[str],
    ) -> None:
        status = str(kw.get("status") or "completed").strip().lower()
        state = _COMPLETE_STATUS.get(status, "failed")
        if str(kw.get("exit_reason") or "").lower() in {"max_iterations", "max_turns"}:
            state = "incomplete"
        child.state = state
        child.ended_at = now
        _close_open_tools(child)
        dur = _as_float(kw.get("duration_seconds"))
        child.duration_s = dur
        if child.started_at is None:
            child.started_at = now - (dur or 0.0)
        summary_raw = kw.get("summary") or preview or ""
        if state == "completed":
            child.reason = None
            child.summary = plain_summary(summary_raw) or None
        elif state == "cancelled":
            child.reason = sanitize_text(kw.get("reason") or "", _REASON_MAX) or "stopped before finishing"
        elif state == "incomplete":
            child.reason = sanitize_text(kw.get("reason") or "", _REASON_MAX) or "ran out of steps"
        elif status == "timeout":
            child.reason = f"timed out after {format_duration(dur)}"
        else:
            child.reason = sanitize_text(plain_summary(summary_raw) or summary_raw, _REASON_MAX) or "failed"
        files = kw.get("files_written") or []
        if isinstance(files, (list, tuple)):
            child.files_written = [
                sanitize_text(str(p).replace("\\", "/").rsplit("/", 1)[-1], 40)
                for p in files[:20]
                if p
            ]
        # Finished workers leave the live feed; their result is delivered
        # once (finding / alert) and kept for the final summary.
        group.feed = [e for e in group.feed if e.child_key != child.key]
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
                    if child.state in TERMINAL_STATES:
                        group.feed = [e for e in group.feed if e.child_key != child.key]
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
            _close_open_tools(child)
            self._alert(child, "failed", alerts)
            return True
        activity_age = _as_float((info or {}).get("seconds_since_activity"))
        before = (child.state, child.reason)
        if child.open_tools:
            return False  # an in-flight tool is observed work, not a stall
        if (info or {}).get("external"):
            # External CLI worker: activity = its own output growth.
            age = quiet if activity_age is None else activity_age
            if (info or {}).get("has_output") is False:
                # Nothing printed yet: the real work hasn't started (e.g. a
                # queue waiting on another worker). Silence is expected, so
                # it is never a stall, however long it lasts.
                if self.heartbeat_seconds and age >= self.heartbeat_seconds:
                    child.state = "waiting"
                    child.reason = "queued (waiting to start)"
                else:
                    child.state = "running"
                    child.reason = None
            elif age >= self.stall_seconds:
                child.state = "blocked"
                child.reason = f"no output for {format_duration(age)}"
                self._alert(child, "blocked", alerts, quiet_for=age)
            elif self.heartbeat_seconds and age >= self.heartbeat_seconds:
                child.state = "waiting"
                child.reason = f"no new output for {format_duration(age)}"
            else:
                child.state = "running"
                child.reason = (
                    f"output {format_duration(age)} ago" if activity_age is not None else None
                )
            return (child.state, child.reason) != before
        idle = quiet if activity_age is None else min(quiet, activity_age)
        if idle >= self.stall_seconds:
            desc = sanitize_text((info or {}).get("activity") or "", 60)
            child.state = "blocked"
            child.reason = f"no activity observed for {format_duration(idle)}" + (
                f" (last: {desc})" if desc else ""
            )
            self._alert(child, "blocked", alerts, quiet_for=idle)
        elif self.heartbeat_seconds and quiet >= self.heartbeat_seconds:
            tool = (info or {}).get("current_tool")
            child.state = "waiting"
            child.reason = (
                f"waiting on {sanitize_text(tool, 40)}" if tool else "waiting for model response"
            )
        return (child.state, child.reason) != before

    # -- alerts / findings -------------------------------------------------

    def _alert(
        self,
        child: ChildActivity,
        kind: str,
        alerts: List[str],
        quiet_for: Optional[float] = None,
    ) -> None:
        """Out-of-band pings. Only a stall is worth interrupting the chat for:
        completion, failure and cancellation are reported once, in the final
        summary message, so a result never shows up twice."""
        if kind in child.alerted:
            return
        child.alerted.add(kind)
        if kind != "blocked":
            return
        title = _truncate_words(child.title, _FEED_TITLE_MAX)
        alerts.append(
            f"⚠️ {title} has been quiet for {_quiet_minutes(quiet_for)}. "
            "It's still running; I'll keep watching."
        )

    # -- rendering -------------------------------------------------------

    def children_for(self, group_ids: List[str]) -> List[ChildActivity]:
        """Every worker across ``group_ids``, in start order (combined card)."""
        with self._lock:
            out: List[ChildActivity] = []
            for gid in group_ids:
                group = self._groups.get(gid)
                if group is not None:
                    out.extend(group.ordered())
            return out

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
            if group.finished:
                return _render_final(group, now, stamp)
            return _render_active(group, now, stamp)


_MODEL_NAMES = (
    # (prefix, family label) — "claude-opus-5-5" -> "Opus 5.5"
    ("claude-opus-", "Opus"),
    ("claude-sonnet-", "Sonnet"),
    ("claude-haiku-", "Haiku"),
)
_FINAL_SUMMARY_SINGLE = 320
_FINAL_SUMMARY_MULTI = 160


def pretty_model(model: str) -> str:
    """Human model label: ``claude-opus-5-5`` -> ``Opus 5.5``, ``gpt-6-sol`` -> ``GPT-6 Sol``."""
    text = _short_model(model)
    if not text:
        return ""
    low = text.lower()
    for prefix, family in _MODEL_NAMES:
        if low.startswith(prefix):
            parts = re.split(r"[-.]", low[len(prefix):])
            nums = [v for v in parts if v.isdigit() and len(v) < 8]
            return f"{family} {'.'.join(nums)}" if nums else family
    if low.startswith("gpt-"):
        parts = text.split("-")
        head = f"GPT-{parts[1]}" if len(parts) > 1 else "GPT"
        rest = " ".join(p.capitalize() for p in parts[2:])
        return f"{head} {rest}".strip()
    return text


def _short_tool_arg(summary: str) -> str:
    """File paths show as their basename; everything else stays as is."""
    text = (summary or "").strip()
    if not text:
        return ""
    if " " not in text and ("/" in text or "\\" in text):
        base = text.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return base or text
    return text


def _tool_phrase(name: str, summary: str) -> str:
    arg = _short_tool_arg(summary)
    return f"{name} {arg}" if arg else name


def _quiet_minutes(seconds: Optional[float]) -> str:
    total = max(0, int(seconds or 0))
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _elapsed(child: ChildActivity, now: float) -> str:
    started = child.started_at if child.started_at is not None else child.first_seen
    if child.duration_s is not None:
        return format_duration(child.duration_s)
    return format_duration((child.ended_at or now) - started)


def _doing(child: ChildActivity, now: float) -> str:
    """One short phrase: what this worker is doing right now."""
    latest = child._latest_open()
    if latest is not None:
        phrase = f"🔧 {_tool_phrase(latest.name, latest.summary)}"
        extra = len(child.open_tools) - 1
        return phrase + (f" (+{extra})" if extra > 0 else "")
    if child.state in {"waiting", "blocked"} and child.reason:
        if child.reason in _PROVIDER_WAIT_REASONS and child.last_action:
            # A provider round-trip never hides the last visible action;
            # only a retry is worth a (quiet) mention next to it.
            if child.reason == _PROVIDER_RETRY_REASON:
                return f"{child.last_action} · {child.reason}"
            return child.last_action
        return f"{_state_icon(child.state)} {child.reason}"
    if child.state == "queued":
        return "queued"
    if child.visibility == "lifecycle":
        return "running · no tool detail"
    if child.last_action:
        return child.last_action
    if child.note:
        return f"💬 {child.note}"
    return "starting…" if child.state == "starting" else "thinking…"


def _tool_count(child: ChildActivity) -> str:
    tools = child.tools_ok + child.tools_failed
    if not tools:
        return ""
    text = f"{tools} tool{'s' if tools != 1 else ''}"
    if child.tools_failed:
        text += f" ({child.tools_failed} failed)"
    return text


def _calls(child: ChildActivity, *, live: bool) -> int:
    return child.tools_ok + child.tools_failed + (len(child.open_tools) if live else 0)


def _stats_head(icon: str, label: str, calls: int, elapsed: str, failed: int = 0) -> str:
    """Header in the same shape as the main agent's turn card:
    ``🔀 Opus 5.5 · 🛠 2 tool calls · ⏱ 22s``."""
    # One line on a phone: failed tool calls are not called out here (a
    # retried call is normal); failed workers show on their own row.
    parts = [f"{icon} {label}".strip()]
    if calls:
        parts.append(f"🛠 {calls} tool call{'s' if calls != 1 else ''}")
    if elapsed:
        parts.append(f"⏱ {elapsed}")
    return " · ".join(parts)


WORKER_ICON = "🍀"


# Fits one line inside a quote on a phone (~36 chars), so rows never wrap.
_ROW_TITLE_MAX = 32


def _bold_title(child: ChildActivity, limit: int = _TITLE_MAX) -> str:
    title = _truncate_words(child.title, limit).replace("*", "")
    return f"**{title}**"


def _italic(text: str) -> str:
    text = (text or "").replace("*", "").strip()
    return f"*{text}*" if text else ""


def _worker_rows(child: ChildActivity, now: float, *, live: bool) -> List[str]:
    """Two fixed rows per worker, so every worker lines up the same way:
    ``🍀 **Title**`` then an italic ``Opus 5.5 · 🛠 2 tool calls · ⏱ 9s``.
    Live cards show what it's doing instead of the time."""
    if live:
        icon = WORKER_ICON
    else:
        icon = WORKER_ICON if child.state == "completed" else _state_icon(child.state, "•")
    title = f"{icon} {_bold_title(child, _ROW_TITLE_MAX)}"
    calls = _calls(child, live=live)
    bits = [pretty_model(child.model)]
    if calls:
        bits.append(f"🛠 {calls} tool call{'s' if calls != 1 else ''}")
    if live:
        bits.append(_doing(child, now).replace("🔧 ", ""))
    else:
        bits.append(f"⏱ {_elapsed(child, now)}")
    return [title, _italic(" · ".join(b for b in bits if b))]


def _render_active(group: DelegationGroup, now: float, stamp: str) -> str:
    children = group.ordered()
    active = [c for c in children if c.state in _ACTIVE_STATES or c.state == "queued"]
    # Once every worker has ended the card's clock stops at the last one's end,
    # whether or not the result was ever delivered: the board keeps showing a
    # finished group until its summary posts, and every redraw used "now".
    settled = bool(children) and all(c.state in TERMINAL_STATES for c in children)
    if settled:
        end = max((c.ended_at for c in children if c.ended_at), default=now)
        elapsed = format_duration(max(0.0, end - group.created_at))
    else:
        elapsed = format_duration(now - group.created_at)
    stopped = settled and all(c.state == "cancelled" for c in children)
    if len(children) == 1:
        child = children[0]
        label = pretty_model(child.model) or "Subagent"
        if stopped:
            label += " · stopped"
        head = _stats_head("🔀", label, _calls(child, live=True), elapsed)
        if settled:
            quoted = [_bold_title(child), f"{_state_icon(child.state)} {child.reason or child.state}"]
        else:
            quoted = [_bold_title(child), _doing(child, now)]
        if child.note and child.open_tools and child.visibility != "lifecycle":
            quoted.append(f"💬 {child.note}")
        return "\n".join([head] + _quote(quoted))
    done = sum(1 for c in children if c.state == "completed")
    label = f"{len(children)} subagents" + (f" · {done} done" if done else "")
    if stopped:
        label += " · stopped"
    head = _stats_head("🔀", label, sum(_calls(c, live=True) for c in children), elapsed)
    quoted: List[str] = []
    shown = 0
    for child in children:
        if child.state in TERMINAL_STATES:
            if child.state != "completed":
                # Failures/cancellations stay visible; successes are just
                # counted in the header until the final summary.
                if quoted:
                    quoted.append(SPACER)
                quoted += _worker_rows(child, now, live=False)
            continue
        if shown >= _MAX_ACTIVE_LINES:
            continue
        shown += 1
        if quoted:
            quoted.append(SPACER)
        quoted += _worker_rows(child, now, live=True)
    hidden = len(active) - shown
    if hidden > 0:
        quoted.append(f"+{hidden} more running")
    return "\n".join([head] + _quote(quoted))


_PLAIN_MARKER_RE = re.compile(r"(?im)^\s*[*_#\s]*plain summary\s*[*_]*\s*:\s*[*_]*\s*")
# Real path shapes only: a segment with a dot/underscore or a leading ~ . /,
# never URLs, dates (2026/09/27), ratios (24/7) or words (and/or).
_URL_RE = re.compile(r"\bhttps?://\S+")
_PATH_RE = re.compile(
    r"(?<![\w/:])(?:~|\.{1,2})?/?(?:[\w.-]+/)+([\w-]+\.[A-Za-z0-9]{1,8})\b"
)
# ":123" / ":12-40" only right after a filename.
_CODE_REF_RE = re.compile(r"(\.[A-Za-z][A-Za-z0-9]{0,7}):\d+(?:-\d+)?\b")
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_MD_NOISE_RE = re.compile(r"\*\*|__|`+|^\s*#+\s*|^\s*[-•*]\s+|^\s*\d+\.\s+", re.MULTILINE)
_CODE_SPAN_RE = re.compile(r"`[^`]*`\s*")
_EMPTY_PARENS_RE = re.compile(r"\s*\(\s*[,;:]?\s*\)")
PLAIN_SUMMARY_MAX = 320


def plain_summary(text: Any, limit: int = PLAIN_SUMMARY_MAX) -> str:
    """A short plain-English paragraph for the chat summary.

    Prefers the worker's own ``Plain summary:`` paragraph (subagents are asked
    to end with one). Otherwise falls back to the first sentences of the
    answer with markdown, paths and line refs stripped. Always redacted.
    """
    raw = _redact(str(text or ""))
    # Code blocks are never prose, and a "Plain summary:" quoted inside one
    # (tool output, a file being reviewed) is not the worker's own.
    raw = _FENCE_RE.sub("\n", raw)
    marker = None
    for candidate in _PLAIN_MARKER_RE.finditer(raw):
        line_start = raw.rfind("\n", 0, candidate.start()) + 1
        if raw[line_start:candidate.start()].lstrip().startswith(">"):
            continue  # quoted, not the worker's own
        marker = candidate
    if marker is not None:
        # Just that paragraph: stop at the first blank line.
        para = re.split(r"\n\s*\n", raw[marker.end():].strip(), maxsplit=1)[0]
        if para.strip():
            raw = para
        else:
            marker = None
    if marker is None:
        # Keep prose; a bullet item becomes its own sentence.
        kept = []
        for ln in raw.splitlines():
            s = ln.strip()
            if not s or s.startswith(("#", "|", ">")):
                continue
            if re.match(r"^([-•*]|\d+\.)\s+", s) and not re.search(r"[.!?:]$", s):
                s += "."
            kept.append(s)
        raw = "\n".join(kept)
    if marker is None:
        # Fallback prose: code spans are identifiers, not plain English.
        raw = _CODE_SPAN_RE.sub("", raw)
    urls: List[str] = []

    def _keep_url(m: "re.Match[str]") -> str:
        urls.append(m.group(0))
        return f"\x00{len(urls) - 1}\x00"

    text = _URL_RE.sub(_keep_url, raw)
    text = _MD_NOISE_RE.sub("", text)
    text = _PATH_RE.sub(lambda m: m.group(1), text)
    text = _CODE_REF_RE.sub(r"\1", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: urls[int(m.group(1))], text)
    text = _EMPTY_PARENS_RE.sub("", text)
    text = " ".join(text.split()).replace("`", "'")
    if marker is None:
        # Removing code spans can leave broken sentences ("X clears , so");
        # keep only sentences that still read as prose.
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        # Only leftover punctuation marks a sentence broken by stripping.
        clean = [s for s in sentences if not re.search(r"\s[,;:.]|^[,;:.]", s)]
        text = " ".join(clean or sentences[:1])
    if len(text) <= limit:
        return text
    # Cut on a sentence boundary when one is close enough.
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= limit // 2:
        return cut[: end + 1]
    return _truncate_words(text, limit)


# Visually blank line that chat platforms don't collapse away (U+2800).
SPACER = "\u2800"


def _quote(lines: List[str]) -> List[str]:
    """Blockquote: renders as a side-barred block on Telegram/Discord/Slack,
    setting subagent output apart from the main chat."""
    return [f"> {ln}" for ln in lines if ln]


def _final_body(child: ChildActivity, limit: int) -> str:
    if child.state == "completed":
        return _truncate_words(child.summary or "", limit)
    if child.state == "incomplete":
        return "ran out of steps before finishing"
    if child.state == "cancelled":
        return f"stopped: {child.reason}" if child.reason else "stopped before finishing"
    return f"failed: {child.reason}" if child.reason else "failed"


def _render_final(group: DelegationGroup, now: float, stamp: str) -> str:
    children = group.ordered()
    if len(children) == 1:
        child = children[0]
        icon = _state_icon(child.state, "•")
        head = _stats_head(icon, pretty_model(child.model) or "Subagent",
                           _calls(child, live=False), _elapsed(child, now),
                           child.tools_failed)
        quoted = [_bold_title(child)]
        body = _final_body(child, _FINAL_SUMMARY_SINGLE)
        if body:
            quoted.append(body)
        if child.files_written:
            quoted.append("📄 " + ", ".join(child.files_written[:4])
                          + (f" +{len(child.files_written) - 4}" if len(child.files_written) > 4 else ""))
        return "\n".join([head] + _quote(quoted))
    end = max((c.ended_at or now) for c in children)
    counts = _state_counts(children)
    final_icon = group_icon(children)
    label = f"{len(children)} subagents" + (f" · {counts}" if counts else "")
    lines = [_stats_head(final_icon, label,
                         sum(_calls(c, live=False) for c in children),
                         short_duration(end - group.created_at))]
    quoted: List[str] = []
    for i, child in enumerate(children):
        if i:
            quoted.append(SPACER)  # breathing room between workers
        quoted += _worker_rows(child, now, live=False)
        body = _final_body(child, _FINAL_SUMMARY_MULTI)
        if body:
            quoted.append(body)
    return "\n".join(lines + _quote(quoted))


def _state_counts(children: List[ChildActivity]) -> str:
    done = sum(1 for c in children if c.state == "completed")
    failed = sum(1 for c in children if c.state == "failed")
    cancelled = sum(1 for c in children if c.state == "cancelled")
    incomplete = sum(1 for c in children if c.state == "incomplete")
    if failed == 0 and cancelled == 0 and incomplete == 0:
        return ""
    parts = [f"{done} done"] if done else []
    if incomplete:
        parts.append(f"{incomplete} unfinished")
    if failed:
        parts.append(f"{failed} failed")
    if cancelled:
        parts.append(f"{cancelled} stopped")
    return " · ".join(parts)


def _state_icon(state: str, default: Optional[str] = None) -> str:
    """Card icon for *state*; done/failed follow the active skin's marks."""
    from agent.display import get_done_mark, get_fail_mark

    if state == "completed":
        return get_done_mark(_ICONS["completed"])
    if state == "failed":
        return get_fail_mark(_ICONS["failed"])
    return _ICONS[state] if default is None else _ICONS.get(state, default)


def group_icon(children: List[ChildActivity]) -> str:
    """❌ only for a real failure; running out of steps is ⏳, not red."""
    from agent.display import get_done_mark, get_fail_mark

    states = {c.state for c in children}
    if "failed" in states:
        return get_fail_mark()
    if "incomplete" in states:
        return "⏳"
    if "cancelled" in states:
        return "⏹"
    return get_done_mark()


def short_duration(seconds: Optional[float]) -> str:
    """Header time: ``9s``, ``2m05s``, ``22m``, ``1h05m`` (drops seconds past 10m)."""
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    if 600 <= total < 3600:
        return f"{total // 60}m"
    return format_duration(total)


def turn_card_body(children: List[ChildActivity], now: float) -> List[str]:
    """Worker rows for the combined per-turn card: two fixed rows per worker
    (``🍀 **Title**`` + italic stats) and, once done, its plain summary."""
    rows: List[str] = []
    for i, child in enumerate(children):
        if i:
            rows.append(SPACER)
        live = child.state not in TERMINAL_STATES
        rows += [r for r in _worker_rows(child, now, live=live) if r]
        if not live:
            body = _final_body(child, _FINAL_SUMMARY_MULTI)
            if body:
                rows.append(body)
    return rows
