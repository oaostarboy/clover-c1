"""The live checklist under a helper acknowledgement.

The ack a root's turn ends with is::

    on it, sorting the card bugs out now.

    🔄 fixing the helper-card bugs → test build + PR, ~10–25 min
    🍀 found the bugs
    🔄 writing the fix
    ⬜ tests
    ⬜ test bot

The first line is the root's own words (``handoff.say``) and is left out when
it gave none.  When the helper reports, the header icon becomes 🍀 / 🍂 / ⏹ and
the steps flip to their final icons (a step that was running when it failed
shows 🍂).  A helper that finished but says it did not do the job
(``Result: not done``) counts as failed, never 🍀.  No result line is added: the root's own reply to the completion
delivers the result as a normal message.  This module is
pure state + rendering: the agent renders the first text (``render``), and the
gateway keeps the same ``Checklist`` per delegation to edit that message in
place (``gateway.delegation_activity``).

The steps come from ``delegate_task``'s ``handoff.steps`` and are mirrored
from the helper's own ``todo`` list -- observed status, never guessed.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Sequence, Set, Tuple

MAX_STEPS = 6
MAX_STEP_CHARS = 60
MAX_SAY_CHARS = 200

# ``display.helper_card``: ``unified`` (default) puts the helpers' live
# activity -- recent tool calls, model, tool count, time and, when done, the
# plain summary -- inside this one message (one block per helper when a
# dispatch has several) and the separate 🔀 roster card is not posted for
# that dispatch.  ``classic`` is the previous look: this checklist
# plus the separate 🔀 card.  Read from config (cached briefly), so switching
# back is one ``clover config set display.helper_card classic``.
STYLE_UNIFIED = "unified"
STYLE_CLASSIC = "classic"
_STYLE_TTL = 15.0
_style_cache: List[Any] = [None, 0.0]
_style_override: List[Optional[str]] = [None]


def card_style() -> str:
    if _style_override[0]:
        return _style_override[0]
    now = time.monotonic()
    if _style_cache[0] is not None and now - _style_cache[1] < _STYLE_TTL:
        return _style_cache[0]
    style = STYLE_UNIFIED
    try:
        from clover_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        raw = str(((cfg.get("display") or {}).get("helper_card") or "")).strip().lower()
        if raw == STYLE_CLASSIC:
            style = STYLE_CLASSIC
    except Exception:
        pass
    _style_cache[0], _style_cache[1] = style, now
    return style


def set_style_for_tests(style: Optional[str]) -> None:
    _style_override[0] = style
    _style_cache[0] = None


def unified() -> bool:
    return card_style() == STYLE_UNIFIED

RUNNING = "running"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"

PENDING = "pending"  # a helper of a multi dispatch that has not started
RECENT_TOOLS_SINGLE = 3  # tool lines on a one-helper card
RECENT_TOOLS_MULTI = 2  # tool lines per running helper on a several-helper card
SUMMARY_CHARS = 140  # a helper's summary on a several-helper card
COMPACT_OVER = 4  # more helpers than this: the compact layout
COMPACT_RUNNING = 3  # ...shows at most this many running helpers in full
COMPACT_RESULT_CHARS = 48  # ...and a done helper's result in this many
COMPACT_NAMES = 5  # names listed on a grouped "waiting" / "more running" line
CARD_BUDGET = 3900  # Telegram's message limit, with room to spare

PENDING_ICON = "\u2b1c"  # ⬜
ACTIVE_ICON = "\U0001f504"  # 🔄
DONE_ICON = "\U0001f340"  # 🍀
FAILED_ICON = "\U0001f342"  # 🍂
STOPPED_ICON = "\u23f9"  # ⏹

_STEP_ICON = {
    "completed": DONE_ICON,
    "in_progress": ACTIVE_ICON,
    "pending": PENDING_ICON,
    "cancelled": STOPPED_ICON,
    "failed": FAILED_ICON,
}
_END_ICON = {DONE: DONE_ICON, FAILED: FAILED_ICON, STOPPED: STOPPED_ICON}
_CHILD_ICON = {PENDING: PENDING_ICON, RUNNING: ACTIVE_ICON, **_END_ICON}
_TOOL_OK, _TOOL_ERR = "\u2713", "\u2717"  # ✓ ✗
_MAX_OPEN_TOOLS = 8


def clean_say(value: Any) -> str:
    """The root's one-or-two-sentence opener, or ``""``."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not any(ch.isalnum() for ch in text):
        return ""
    if len(text) > MAX_SAY_CHARS:
        cut = text[: MAX_SAY_CHARS - 1]
        space = cut.rfind(" ")
        if space >= MAX_SAY_CHARS // 2:
            cut = cut[:space]
        text = cut.rstrip(" ,;:-") + "\u2026"
    return text


def clean_steps(value: Any) -> Tuple[str, ...]:
    """1-6 short single-line steps from the model's ``handoff.steps``.

    Anything that is not a list of non-empty strings is dropped; extras past
    ``MAX_STEPS`` are dropped and each step is cut at a word boundary.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    out: List[str] = []
    for raw in value:
        if not isinstance(raw, str):
            continue
        text = " ".join(raw.replace("\r", "\n").split("\n")[0].split()).strip()
        if not any(ch.isalnum() for ch in text):
            continue
        if len(text) > MAX_STEP_CHARS:
            cut = text[: MAX_STEP_CHARS - 1]
            space = cut.rfind(" ")
            if space >= MAX_STEP_CHARS // 2:
                cut = cut[:space]
            text = cut.rstrip(" ,;:.!?-") + "\u2026"
        out.append(text)
        if len(out) >= MAX_STEPS:
            break
    return tuple(out)


@dataclass
class Child:
    """One helper of a multi-helper dispatch (observed state only)."""

    index: int
    title: str = ""
    goal: str = ""
    sid: str = ""
    state: str = PENDING
    model: str = ""
    calls: int = 0
    doing: str = ""
    summary: str = ""
    started: Optional[float] = None
    ended: Optional[float] = None
    titled: bool = False  # the helper's own title was seen (beats the goal)
    hidden: int = 0  # its todo-list calls: hidden, so not counted either
    hidden_ids: Set[str] = field(default_factory=set)
    open: List[Tuple[str, str, str]] = field(default_factory=list)  # (call id, name, phrase)
    recent: Deque[Tuple[str, bool]] = field(default_factory=lambda: deque(maxlen=RECENT_TOOLS_SINGLE))

    @property
    def label(self) -> str:
        return (self.title or "").replace("*", "").strip() or f"helper {self.index + 1}"


@dataclass
class Checklist:
    """One dispatch's header line + steps + (once over) its result."""

    header: str
    steps: List[List[str]] = field(default_factory=list)  # [text, status]
    state: str = RUNNING
    say: str = ""
    # Live activity for the unified card (observed, never guessed).
    model: str = ""
    calls: int = 0
    doing: str = ""
    summary: str = ""
    started: Optional[float] = None
    ended: Optional[float] = None
    # The current tool(s) and the last few finished ones (newest last).
    open: List[Tuple[str, str, str]] = field(default_factory=list)
    recent: Deque[Tuple[str, bool]] = field(default_factory=lambda: deque(maxlen=RECENT_TOOLS_SINGLE))
    # A dispatch of several helpers: one entry each, in dispatch order.  The
    # card then shows a block per helper instead of one helper's steps.
    kids: List[Child] = field(default_factory=list)
    hidden: int = 0  # the helper's todo-list calls: hidden, so not counted either
    hidden_ids: Set[str] = field(default_factory=set)

    @classmethod
    def start(cls, header: str, steps: Sequence[str], say: str = "") -> "Checklist":
        rows = [[s, "pending"] for s in steps]
        if rows:
            rows[0][1] = "in_progress"
        return cls(header=header, steps=rows, say=say)

    # -- state -----------------------------------------------------------

    def apply_todos(self, todos: Any) -> bool:
        """Mirror the helper's todo list onto the steps.  True when it changed.

        Matches by position (the list was seeded from the steps, ids ``s1``..),
        so a helper that renames an item still ticks the right row.  Items the
        helper added past the seeded steps are ignored: the user was promised
        this list.  Never regresses a finished checklist.
        """
        if self.state != RUNNING or not self.steps or not isinstance(todos, list):
            return False
        changed = False
        for i, row in enumerate(self.steps):
            if i >= len(todos) or not isinstance(todos[i], dict):
                break
            status = str(todos[i].get("status") or "").strip().lower()
            if status in _STEP_ICON and row[1] != status:
                row[1] = status
                changed = True
        return changed

    def finish(self, state: str) -> bool:
        """Close the checklist.  A second call changes nothing."""
        if self.state != RUNNING or state not in _END_ICON:
            return False
        self.state = state
        if state == DONE:
            for row in self.steps:
                if row[1] in ("pending", "in_progress"):
                    row[1] = "completed"
        else:
            # The step that was running is where it stopped / went wrong;
            # steps never reached stay as they were.
            mark = "failed" if state == FAILED else "cancelled"
            for row in self.steps:
                if row[1] == "in_progress":
                    row[1] = mark
                    break
        return True

    # -- text ------------------------------------------------------------

    def render(self, summary_chars: int = SUMMARY_CHARS, tool_cap: int = RECENT_TOOLS_MULTI) -> str:
        if unified():
            if self.kids:
                return self.render_unified_multi(summary_chars=summary_chars, tool_cap=tool_cap)
            return self.render_unified()
        return self.render_classic()

    def _title_outcome(self) -> Tuple[str, str, str]:
        """(icon, title, outcome) of the header line."""
        icon, rest = ACTIVE_ICON, self.header
        for ic in (ACTIVE_ICON, DONE_ICON, FAILED_ICON, STOPPED_ICON):
            if rest.startswith(ic):
                rest = rest[len(ic):].strip()
                break
        if self.state != RUNNING:
            icon = _END_ICON[self.state]
        work, _, outcome = rest.partition(" \u2192 ")
        outcome = outcome.replace(", ~", " \u00b7 ~", 1)
        if self.state != RUNNING and " \u00b7 ~" in outcome:
            outcome = outcome.split(" \u00b7 ~", 1)[0]
        return icon, work.replace("*", "").strip(), outcome

    def render_unified(self, now: Optional[float] = None) -> str:
        icon, title, outcome = self._title_outcome()
        lines = [f"{icon} **{title}**"]
        quoted: List[str] = []
        if outcome:
            quoted.append(_italic("\u2192 " + outcome))
        if self.steps:
            quoted.append(_SPACER)
            quoted += [f"{_STEP_ICON[status]} {text}" for text, status in self.steps]
        tail: List[str] = []
        if self.state == RUNNING:
            tail += _tool_lines(self, RECENT_TOOLS_SINGLE)
        elif self.summary:
            tail.append(self.summary.replace("*", ""))
        stats = _stats_line(self.model, self.calls, self.started, self.ended, now)
        if stats:
            tail.append(_italic(stats))
        if tail:
            quoted.append(_SPACER)
            quoted += tail
        body = "\n".join(lines + [f"> {q}" for q in quoted if q])
        return f"{self.say}\n\n{body}" if self.say else body

    def render_unified_multi(
        self, now: Optional[float] = None, summary_chars: int = SUMMARY_CHARS,
        tool_cap: int = RECENT_TOOLS_MULTI,
    ) -> str:
        """One block per helper: icon + title, its recent tool calls while it
        runs (its summary once over), its stats; a totals line at the end."""
        now = time.monotonic() if now is None else now
        icon, title, outcome = self._title_outcome()
        kids = self.kids
        done = sum(1 for k in kids if k.state == DONE)
        head = f"{icon} **{title}**"
        if self.state == RUNNING or done < len(kids):
            head += f" \u00b7 {done} of {len(kids)} done"
        quoted: List[str] = []
        if outcome:
            quoted.append(_italic("\u2192 " + outcome))
        if len(kids) > COMPACT_OVER:
            quoted += _compact_kids(kids, now, tool_cap)
        else:
            quoted += _full_kids(kids, now, summary_chars, tool_cap)
        started = [k.started for k in kids if k.started is not None]
        ended = [k.ended for k in kids if k.ended is not None]
        calls = sum(k.calls for k in kids)
        foot = [f"{len(kids)} helpers"]
        if calls:
            foot.append(f"\U0001f6e0 {calls} tool call{'s' if calls != 1 else ''}")
        if started:
            end = max(ended) if self.state != RUNNING and ended else now
            foot.append("\u23f1 " + _duration(max(0.0, end - min(started))))
        quoted += [_SPACER, _italic(" \u00b7 ".join(foot))]
        body = "\n".join([head] + [f"> {q}" for q in quoted if q])
        return f"{self.say}\n\n{body}" if self.say else body

    def render_classic(self) -> str:
        head = self.header
        if self.state != RUNNING:
            head = _END_ICON[self.state] + _after_icon(head)
        lines = [head]
        lines += [f"{_STEP_ICON[status]} {text}" for text, status in self.steps]
        body = "\n".join(lines)
        return f"{self.say}\n\n{body}" if self.say else body


_SPACER = "\u2800"


def _italic(text: str) -> str:
    text = (text or "").replace("*", "").strip()
    return f"*{text}*" if text else ""


def _duration(seconds: float) -> str:
    try:
        from agent.delegation_activity import short_duration

        return short_duration(seconds)
    except Exception:
        return f"{int(seconds)}s"


def _stats_line(
    model: str, calls: int, started: Optional[float], ended: Optional[float], now: Optional[float],
) -> str:
    parts: List[str] = []
    if model:
        parts.append(model)
    if calls:
        parts.append(f"\U0001f6e0 {calls} tool call{'s' if calls != 1 else ''}")
    if started is not None:
        end = ended if ended is not None else (now if now is not None else time.monotonic())
        parts.append("\u23f1 " + _duration(max(0.0, end - started)))
    return " \u00b7 ".join(parts)


def _cut_word(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "\u2026"


def _full_kids(kids: Sequence[Child], now: float, summary_chars: int, tool_cap: int) -> List[str]:
    """Up to ``COMPACT_OVER`` helpers: a full block each."""
    quoted: List[str] = []
    for k in kids:
        quoted.append(_SPACER)
        quoted.append(f"{_CHILD_ICON[k.state]} **{k.label}**")
        if k.state == PENDING:
            quoted.append(_italic("waiting to start"))
            continue
        if k.state == RUNNING:
            quoted += _tool_lines(k, tool_cap)
        elif k.summary and summary_chars > 0:
            quoted.append(_cut_word(k.summary.replace("*", ""), summary_chars))
        stats = _stats_line(k.model, k.calls, k.started, k.ended, now)
        if stats:
            quoted.append(_italic(stats))
    return quoted


def _names(kids: Sequence[Child]) -> str:
    shown = [k.label for k in kids[:COMPACT_NAMES]]
    more = len(kids) - len(shown)
    return ", ".join(shown) + ("\u2026" if more > 0 else "")


def _compact_kids(kids: Sequence[Child], now: float, tool_cap: int) -> List[str]:
    """Many helpers: at most ``COMPACT_RUNNING`` running ones in full (name,
    two tool lines, stats), every finished one as a single line, and the ones
    still waiting grouped into one line."""
    quoted: List[str] = []
    running_shown = 0
    extra_running: List[Child] = []
    waiting: List[Child] = []
    for k in kids:
        if k.state == PENDING:
            waiting.append(k)
        elif k.state == RUNNING:
            if running_shown >= COMPACT_RUNNING:
                extra_running.append(k)
                continue
            running_shown += 1
            quoted += [_SPACER, f"{_CHILD_ICON[k.state]} **{k.label}**"]
            quoted += _tool_lines(k, min(tool_cap, RECENT_TOOLS_MULTI))
            stats = _stats_line(k.model, k.calls, k.started, k.ended, now)
            if stats:
                quoted.append(_italic(stats))
        else:
            line = f"{_CHILD_ICON[k.state]} **{k.label}**"
            if k.summary:
                line += " \u00b7 " + _cut_word(k.summary.replace("*", ""), COMPACT_RESULT_CHARS)
            quoted.append(line)
    if extra_running:
        quoted.append(f"{ACTIVE_ICON} {len(extra_running)} more running: {_names(extra_running)}")
    if waiting:
        quoted.append(f"{PENDING_ICON} {len(waiting)} waiting: {_names(waiting)}")
    return quoted


def _tool_lines(obj: Any, cap: int) -> List[str]:
    """The helper's open tool(s) then its last finished ones, newest first."""
    lines = [f"\U0001f527 {_italic(phrase)}" for _, _, phrase in reversed(obj.open)]
    lines += [
        f"{_TOOL_OK if ok else _TOOL_ERR} {_italic(phrase)}" for phrase, ok in reversed(obj.recent)
    ]
    return [ln for ln in lines if _italic("x") and ln.strip()][:cap]


def _after_icon(header: str) -> str:
    """``header`` without its leading status icon (the rest keeps its space)."""
    for icon in (ACTIVE_ICON, DONE_ICON, FAILED_ICON, STOPPED_ICON):
        if header.startswith(icon):
            return header[len(icon):]
    return " " + header


_RESULT_LINE_RE = re.compile(
    r"(?im)^\s*[*_]*\s*result\s*[*_]*\s*:\s*[*_]*\s*(done|not\s+done)\b"
)


def helper_outcome(text: Any) -> str:
    """``"done"`` / ``"not_done"`` from the helper's own ``Result:`` line
    (its last one wins), or ``""`` when it wrote none."""
    found = _RESULT_LINE_RE.findall(str(text or ""))
    if not found:
        return ""
    return "not_done" if found[-1].lower().startswith("not") else "done"


def end_state_for(status: Any) -> str:
    """Checklist end state for a ``subagent.complete`` status string."""
    s = str(status or "").strip().lower()
    if s in {"completed", "ok", "success", ""}:
        return DONE
    if s in {"interrupted", "cancelled", "canceled", "stopped"}:
        return STOPPED
    return FAILED


def todos_from_result(result: Any) -> Optional[List[Dict[str, Any]]]:
    """The ``todos`` list out of a ``todo`` tool result (JSON text or dict)."""
    data = result
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except (ValueError, TypeError):
            return None
    todos = data.get("todos") if isinstance(data, dict) else None
    return [t for t in todos if isinstance(t, dict)] if isinstance(todos, list) else None


def seed_todos(steps: Sequence[str]) -> List[Dict[str, str]]:
    """The helper's starting todo list: first step active, the rest pending."""
    return [
        {"id": f"s{i + 1}", "content": text,
         "status": "in_progress" if i == 0 else "pending"}
        for i, text in enumerate(steps)
    ]


def child_instruction(steps: Sequence[str]) -> str:
    """What the helper is told about its pre-seeded todo list."""
    listed = "; ".join(f"{i + 1}) {s}" for i, s in enumerate(steps))
    return (
        "Your todo list is already set up with these steps, which the user "
        f"watches live: {listed}. Keep it current with the todo tool as you "
        "work (merge=true): mark a step completed when it is done and the "
        "next one in_progress. Do not rename, add or remove steps."
    )


# ── parts, cards and the registry ──────────────────────────────────────────
# A *part* is one dispatch's checklist, registered by delegation id the moment
# the dispatch is accepted -- so helper events that arrive before the ack is
# even sent are not lost.  A *card* is the ack message: one or more parts
# rendered together, bound (by the gateway) to the chat message id it was sent
# as, and edited in place whenever a part changes.

_LOCK = threading.RLock()
_PARTS: Dict[str, "Part"] = {}
_CARDS: List["Card"] = []
_EARLY: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}  # events before the part
_MAX_PARTS = 64
_MAX_CARDS = 32
# Delegation ids promised a part: the dispatch is about to start its helpers,
# whose first events can fire before the receipt opens the part.
_RESERVED: Dict[str, float] = {}
BIND_WINDOW_SECONDS = 180.0


@dataclass
class Part:
    delegation_id: str
    checklist: Checklist
    expected: int = 1  # children in the dispatch; it ends when all reported
    ended: List[str] = field(default_factory=list)  # end state per child
    card: Optional["Card"] = None

    def _changed(self) -> None:
        card = self.card
        if card is not None:
            card.touch()


def open_part(
    delegation_id: str, header: str, steps: Sequence[str], expected: int = 1, say: str = "",
    goals: Sequence[Any] = (),
) -> Optional[Part]:
    did = str(delegation_id or "").strip()
    if not did or not str(header or "").strip():
        return None
    with _LOCK:
        _RESERVED.pop(did, None)
        part = _PARTS.pop(did, None)
        if part is None:
            n = max(1, int(expected or 1))
            # Several helpers share the one card: a block each, no step list.
            part = Part(did, Checklist.start(header, steps if n == 1 else (), say), n)
            if n > 1:
                part.checklist.kids = [Child(index=i, title=_goal_title(_nth(goals, i))) for i in range(n)]
        _PARTS[did] = part
        while len(_PARTS) > _MAX_PARTS:
            _PARTS.pop(next(iter(_PARTS)))
        # The helper starts running at dispatch, before the receipt that opens
        # this part exists: replay what it already reported.
        for et, kw in _EARLY.pop(did, []):
            if et == "subagent.todo":
                part.checklist.apply_todos(kw.get("todos"))
            else:
                _record_complete(part, kw)
        return part


def reserve(delegation_id: Any) -> bool:
    """Claim ``delegation_id`` for the unified card before its helpers start,
    so ``owns_delegation`` is true from their very first event (and the
    gateway never posts a separate 🔀 card).  ``open_part`` fulfils the
    reservation; ``release`` drops it when the dispatch is not accepted."""
    did = str(delegation_id or "").strip()
    if not did:
        return False
    with _LOCK:
        _RESERVED[did] = time.monotonic()
        while len(_RESERVED) > _MAX_PARTS:
            _RESERVED.pop(next(iter(_RESERVED)))
    return True


def release(delegation_id: Any) -> None:
    did = str(delegation_id or "").strip()
    with _LOCK:
        _RESERVED.pop(did, None)


def _nth(items: Any, i: int) -> Any:
    try:
        return list(items or ())[i]
    except (IndexError, TypeError):
        return ""


def _goal_title(goal: Any) -> str:
    if not str(goal or "").strip():
        return ""
    try:
        from agent.delegation_activity import derive_task_title

        return derive_task_title(goal)
    except Exception:
        return ""


def on_child_event(event_type: Any, **kw: Any) -> bool:
    """Fold one relayed ``subagent.*`` event into its dispatch's checklist.

    ``subagent.todo`` carries the helper's todo list; ``subagent.complete``
    its end status and summary.  True when something visible changed.
    Never raises.
    """
    try:
        et = str(event_type or "")
        if et in _ACTIVITY_EVENTS:
            return _on_activity(et, kw)
        if et not in ("subagent.todo", "subagent.complete"):
            return False
        did = str(kw.get("delegation_id") or "")
        with _LOCK:
            part = _PARTS.get(did)
            if part is None:
                if did:
                    early = _EARLY.setdefault(did, [])
                    if et == "subagent.todo":
                        early[:] = [e for e in early if e[0] != "subagent.todo"]
                    early.append((et, {k: kw.get(k) for k in ("todos", "status", "exit_reason", "outcome", "summary", "preview",
                                                             "task_index", "subagent_id", "title", "goal")}))
                    while len(_EARLY) > _MAX_PARTS:
                        _EARLY.pop(next(iter(_EARLY)))
                return False
            if et == "subagent.todo":
                changed = part.checklist.apply_todos(kw.get("todos"))
            else:
                changed = _record_complete(part, kw)
        if changed:
            part._changed()
        return changed
    except Exception:
        return False


_ACTIVITY_EVENTS = frozenset({"subagent.start", "subagent.tool", "subagent.tool_done"})


def _child_for(cl: Checklist, kw: Dict[str, Any], *, ending: bool = False) -> Optional[Child]:
    """The helper an event belongs to: ``task_index``, else ``subagent_id``.
    A completion that names neither closes the first helper still open."""
    kids = cl.kids
    ti = kw.get("task_index")
    if isinstance(ti, int) and not isinstance(ti, bool) and 0 <= ti < len(kids):
        return kids[ti]
    sid = str(kw.get("subagent_id") or "")
    if sid:
        for k in kids:
            if k.sid == sid:
                return k
        for k in kids:
            if not k.sid and k.state in (PENDING, RUNNING):
                k.sid = sid
                return k
    if ending:
        for k in kids:
            if k.state == RUNNING:
                return k
        for k in kids:
            if k.state == PENDING:
                return k
    return None


def _snapshot(obj: Any) -> Tuple[Any, ...]:
    return (
        obj.model, obj.calls, tuple(obj.open), tuple(obj.recent),
        getattr(obj, "state", ""), getattr(obj, "title", ""),
    )


def _note_identity(kid: Child, kw: Dict[str, Any]) -> None:
    title = " ".join(str(kw.get("title") or "").replace("*", "").split())
    if title:
        kid.title, kid.titled = title, True
    elif not kid.titled and str(kw.get("goal") or "").strip():
        kid.title = _goal_title(kw.get("goal")) or kid.title


def _on_activity(et: str, kw: Dict[str, Any]) -> bool:
    """Live activity for the unified card.  Recorded whatever the style (it
    is cheap), so switching styles mid-run shows the right numbers."""
    did = str(kw.get("delegation_id") or "")
    with _LOCK:
        part = _PARTS.get(did)
        if part is None:
            return False
        cl = part.checklist
        if cl.state != RUNNING:
            return False
        obj: Any = cl
        if cl.kids:
            obj = _child_for(cl, kw)
            if obj is None or obj.state not in (PENDING, RUNNING):
                return False
            _note_identity(obj, kw)
            obj.state = RUNNING
        before = _snapshot(obj)
        if obj.started is None:
            obj.started = time.monotonic()
        model = kw.get("model")
        if model and not obj.model:
            try:
                from agent.delegation_activity import pretty_model

                obj.model = pretty_model(str(model)) or ""
            except Exception:
                obj.model = ""
        if et == "subagent.tool" and str(kw.get("tool_name") or "") in _HIDDEN_TOOLS:
            call_id = str(kw.get("tool_call_id") or "")
            if not call_id or call_id not in obj.hidden_ids:
                obj.hidden += 1
                if call_id:
                    obj.hidden_ids.add(call_id)
        try:
            # ``tool_count`` counts every call, the hidden todo ones too.
            obj.calls = max(obj.calls, int(kw.get("tool_count") or 0) - obj.hidden)
        except (TypeError, ValueError):
            pass
        if et == "subagent.tool":
            _tool_started(obj, kw)
        elif et == "subagent.tool_done":
            _tool_finished(obj, kw)
        changed = _snapshot(obj) != before
        if hasattr(obj, "doing"):
            obj.doing = obj.open[-1][2] if obj.open else ""
    if changed and unified():
        part._changed()
    return changed


# The helper's own todo bookkeeping: the steps already show it.
_HIDDEN_TOOLS = frozenset({"todo"})


# A tool line must fit one phone line: "✓ " + the phrase, about 44 characters.
TOOL_LINE_MAX = 42
_HOME_RE = re.compile(r"(?:/home/[^/\s]+|/Users/[^/\s]+)(?=/|\s|$)")
_PATH_TOKEN_RE = re.compile(r"[~\w.\-]*(?:/[\w.\-@+]+){2,}/?")


def _short_path(match: "re.Match[str]") -> str:
    tok = match.group(0)
    if len(tok) <= 20:
        return tok
    parts = [p for p in tok.split("/") if p]
    for keep in (2, 1):
        short = "\u2026/" + "/".join(parts[-keep:])
        if len(short) <= 22 or keep == 1:
            return short
    return tok


def short_tool_phrase(phrase: str, limit: int = TOOL_LINE_MAX) -> str:
    """One phone line: ``/home/<user>/`` becomes ``~/``, long paths keep their
    last one or two segments (``\u2026/claude-code``), and the rest is cut on a
    word with an ellipsis."""
    text = " ".join(str(phrase or "").split())
    text = _HOME_RE.sub("~", text)
    text = _PATH_TOKEN_RE.sub(_short_path, text)
    # The summary upstream may already have cut the line ("... rev-parse -\u2026"):
    # drop that stub so the cut below lands on a whole word.
    cut_upstream = text.endswith("\u2026")
    if cut_upstream:
        words = text[:-1].rstrip().split(" ")
        while len(words) > 1 and not any(ch.isalnum() for ch in words[-1]):
            words.pop()
        text = " ".join(words)
    if len(text) <= limit:
        return text + ("\u2026" if cut_upstream else "")
    return _cut_word(text, limit)


def _tool_started(obj: Any, kw: Dict[str, Any]) -> None:
    name = str(kw.get("tool_name") or "tool")
    if name in _HIDDEN_TOOLS:
        return
    call_id = str(kw.get("tool_call_id") or "")
    if call_id and any(c == call_id for c, _, _ in obj.open):
        return  # a repeated start for a call that is already open
    try:
        from agent.delegation_activity import _tool_phrase, summarize_tool_call

        phrase = _tool_phrase(name, summarize_tool_call(name, kw.get("preview"), kw.get("args")))
    except Exception:
        phrase = name
    phrase = short_tool_phrase(phrase)
    obj.open.append((call_id, name, phrase))
    del obj.open[:-_MAX_OPEN_TOOLS]


def _tool_finished(obj: Any, kw: Dict[str, Any]) -> None:
    """Move the open call this result belongs to into the finished ones: by
    ``tool_call_id`` when there is one, else the newest open call of the same
    tool, else the newest open call."""
    if not obj.open:
        return
    call_id = str(kw.get("tool_call_id") or "")
    name = str(kw.get("tool_name") or "")
    if name in _HIDDEN_TOOLS:
        return
    pick = -1
    if call_id:
        pick = next((i for i, (c, _, _) in enumerate(obj.open) if c == call_id), -1)
    if pick < 0 and name:
        pick = next((i for i in range(len(obj.open) - 1, -1, -1) if obj.open[i][1] == name), -1)
    if pick < 0:
        pick = len(obj.open) - 1
    _, _, phrase = obj.open.pop(pick)
    obj.recent.append((phrase, not kw.get("is_error")))


def _record_complete(part: Part, kw: Dict[str, Any]) -> bool:
    cl = part.checklist
    if cl.state != RUNNING:
        return False
    summary = kw.get("summary") or kw.get("preview")
    # The relay sends the helper's own "Plain summary:" paragraph, extracted
    # from its FULL answer (``summary`` is cut to 500 chars, which drops it).
    plain = " ".join(str(kw.get("plain_summary") or "").split())
    if not plain and summary:
        try:
            from agent.delegation_activity import plain_summary

            plain = plain_summary(summary)
        except Exception:
            plain = ""
    state = end_state_for(kw.get("status"))
    if str(kw.get("exit_reason") or "").lower() in {"max_iterations", "max_turns"}:
        state = FAILED
    if state == DONE and kw.get("outcome") == "not_done":
        # The run ended cleanly but the helper says the job is not done.
        state = FAILED
    now = time.monotonic()
    if cl.kids:
        kid = _child_for(cl, kw, ending=True)
        if kid is None or kid.state not in (PENDING, RUNNING):
            return False
        _note_identity(kid, kw)
        kid.state = state
        kid.open.clear()
        if plain and not kid.summary:
            kid.summary = plain
        if kid.started is not None:
            kid.ended = now
    elif plain and not cl.summary:
        cl.summary = plain
    part.ended.append(state)
    if len(part.ended) < part.expected:
        return bool(cl.kids)
    final = FAILED if FAILED in part.ended else STOPPED if STOPPED in part.ended else DONE
    if cl.started is not None or any(k.started is not None for k in cl.kids):
        cl.ended = now
    cl.open.clear()
    part.checklist.finish(final)
    return True


# (summary chars, tool lines per running helper) tried in turn until it fits.
_BUDGET_STEPS = ((SUMMARY_CHARS, RECENT_TOOLS_MULTI), (80, RECENT_TOOLS_MULTI), (0, RECENT_TOOLS_MULTI), (0, 1), (0, 0))


class Card:
    """The ack message: its parts, and (once sent) where it lives."""

    def __init__(self, parts: List[Part]) -> None:
        self.parts = parts
        self.created = time.monotonic()
        self.initial_text = self.render()
        self.target: Any = None  # set by the gateway: where the message is
        self.on_change: Any = None  # set by the gateway: schedule an edit

    def render(self) -> str:
        with _LOCK:
            # Unified cards are multi-line blocks: keep two on one message apart.
            sep = "\n\n" if unified() else "\n"
            text = ""
            # Over Telegram's limit: cut the helpers' summaries first, then
            # their tool lines.
            for summary_chars, tool_cap in _BUDGET_STEPS:
                text = sep.join(p.checklist.render(summary_chars, tool_cap) for p in self.parts)
                if len(text) <= CARD_BUDGET:
                    break
            return text

    @property
    def finished(self) -> bool:
        with _LOCK:
            return all(p.checklist.state != RUNNING for p in self.parts)

    def touch(self) -> None:
        cb = self.on_change
        if cb is not None:
            try:
                cb()
            except Exception:
                pass


def card_for(receipts: Sequence[Any]) -> Optional[Card]:
    """The card for the dispatches in ``receipts`` (those that carry a header
    summary), or None.  Calling it again for the same dispatches returns the
    same card."""
    with _LOCK:
        parts: List[Part] = []
        for r in receipts:
            header = getattr(r, "summary", "")
            if not header:
                continue
            part = _PARTS.get(r.delegation_id) or open_part(
                r.delegation_id, header, getattr(r, "steps", ()) or (),
                expected=max(1, len(getattr(r, "goals", ()) or ())),
                say=getattr(r, "say", ""),
                goals=getattr(r, "goals", ()) or (),
            )
            if part is not None:
                parts.append(part)
        if not parts:
            return None
        for existing in _CARDS:
            if [id(p) for p in existing.parts] == [id(p) for p in parts]:
                return existing
        card = Card(parts)
        for p in parts:
            p.card = card
        _CARDS.append(card)
        while len(_CARDS) > _MAX_CARDS:
            _CARDS.pop(0)
        return card


def match_unbound(content: Any) -> Optional[Card]:
    """The newest card not yet placed in a chat whose first text is ``content``.

    A streaming cursor or a trailing space after the text is tolerated, so the
    send of the ack by any delivery path finds its card.
    """
    text = str(content or "")
    if not text:
        return None
    now = time.monotonic()
    with _LOCK:
        for card in reversed(_CARDS):
            if card.target is not None or now - card.created > BIND_WINDOW_SECONDS:
                continue
            base = card.initial_text
            # Delivery may add a cursor or a footer; the card text is the bulk.
            if base in text and len(text) - len(base) <= 160:
                return card
    return None


def owns_delegation(delegation_id: Any) -> bool:
    """True when the unified card is on and this dispatch has a checklist
    message (one helper or several): the gateway then skips its separate 🔀
    roster card for it."""
    did = str(delegation_id or "")
    if not did or not unified():
        return False
    with _LOCK:
        return did in _PARTS or did in _RESERVED


def reset_for_tests() -> None:
    _style_override[0] = None
    _style_cache[0] = None
    with _LOCK:
        _PARTS.clear()
        _CARDS.clear()
        _EARLY.clear()
        _RESERVED.clear()
