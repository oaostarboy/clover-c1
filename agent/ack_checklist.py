"""The live checklist under a helper acknowledgement.

The ack a root's turn ends with is::

    on it, sorting the card bugs out now.

    🔄 fixing the helper-card bugs → test build + PR, ~10–25 min
    🍀 found the bugs
    🔄 writing the fix
    ⬜ tests
    ⬜ test bot

The first line is the root's own words (``handoff.say``) and is left out when
it gave none.  When the helper reports, the header icon becomes 🍀 / 🍂 / 🥀 and
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

import html
import json
import re
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from typing import Any, Deque, Dict, List, Optional, Sequence, Set, Tuple

MAX_STEPS = 6
MAX_STEP_CHARS = 60
MAX_SAY_CHARS = 320

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
PARTIAL = "partial"
BLOCKED = "blocked"
UNVERIFIED = "unverified"
UNFINISHED = "unfinished"
_INCOMPLETE = (PARTIAL, BLOCKED, UNVERIFIED, UNFINISHED)
_TERMINAL = (DONE, FAILED, STOPPED) + _INCOMPLETE


def _status_icon(state: str) -> str:
    from agent.clover_flavor import outcome_mark

    return outcome_mark(state, {DONE: DONE_ICON, FAILED: FAILED_ICON,
                               STOPPED: STOPPED_ICON}.get(state, "☘️"))


PENDING = "pending"  # a helper of a multi dispatch that has not started
RECENT_TOOLS_SINGLE = 3  # tool lines on a one-helper card
RECENT_TOOLS_MULTI = 2  # tool lines per running helper on a several-helper card
SUMMARY_CHARS = 140  # a helper's summary on a several-helper card
COMPACT_OVER = 6  # more helpers than this: the compact layout
COMPACT_RUNNING = 3  # ...shows at most this many running helpers in full
COMPACT_RESULT_CHARS = 48  # ...and a done helper's result in this many
ROW_CHARS = 44  # a name + its stats fit one phone row up to this many visible chars
RESULT_LINE_CHARS = 40  # a subagent's result line on a several-subagent card: one row
COMPACT_NAMES = 5  # names listed on a grouped "waiting" / "more running" line
CARD_BUDGET = 3900  # Telegram's message limit, with room to spare

PENDING_ICON = "\u2b1c"  # ⬜
ACTIVE_ICON = "\U0001f504"  # 🔄
DONE_ICON = "\U0001f340"  # 🍀
FAILED_ICON = "\U0001f940"  # 🥀
TITLE_ICON = "\U0001f500"  # 🔀 leads a running unified card's title
STOPPED_ICON = "⏹"  # stopped, never a failure

_STEP_ICON = {
    "completed": DONE_ICON,
    "in_progress": ACTIVE_ICON,
    "pending": PENDING_ICON,
    "cancelled": STOPPED_ICON,
    "failed": FAILED_ICON,
    "unfinished": "☘️",
}
_END_ICON = {DONE: DONE_ICON, FAILED: FAILED_ICON, STOPPED: STOPPED_ICON,
             **{state: "☘️" for state in _INCOMPLETE}}
# Progress/tool marks stay plain. Terminal task marks use the active skin:
# success, true failure, incomplete work and stopped work stay distinct.
_STEP_MARK = {
    "completed": "\u2713",  # ✓
    "in_progress": "\u25b8",  # ▸
    "pending": "\u25cb",  # ○
    "failed": "\u2717",  # ✗
    "cancelled": "\u2013",  # –
    "unfinished": "☘️",
}
# Tool phrases are cut so the WHOLE line (indent + "\U0001f527 " + phrase) fits one
# phone row: a few characters more on the single card (no indent).
TOOL_PHRASE_MULTI = 32  # tool lines nest two levels deep on a several-subagent card
TOOL_PHRASE_SINGLE = 38  # ...and one level deep on the single card
_MAX_OPEN_TOOLS = 8


def _step_icon(status: str, *, unified_step: bool = False) -> str:
    state = {"completed": DONE, "failed": FAILED, "cancelled": STOPPED,
             "unfinished": UNFINISHED}.get(status)
    if state is not None and (not unified_step or state in (FAILED, UNFINISHED)):
        return _status_icon(state)
    return (_STEP_MARK if unified_step else _STEP_ICON)[status]


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


_PUBLIC_ASSIGNMENT_RE = re.compile(
    r"(?<![\w])(?:password|passwd|pwd|token|secret|api[_ -]?key|"
    r"access[_ -]?token|refresh[_ -]?token|client[_ -]?secret)"
    r"[\"'`]*\s*[:=]\s*"
    r'''(?:"(?:\\.|[^"\\])*(?:"|$)|'(?:\\.|[^'\\])*(?:'|$)|`[^`]*(?:`|$)|[^\s,;}\]]+)''',
    re.IGNORECASE,
)


def _public_step_text(raw: str) -> str:
    """Strict public-label boundary, separate from the general log redactor.

    Normalize display disguises before finding assignments; remove full quoted
    values (including whitespace), before any line extraction or truncation.
    Ordinary labels about password/token handling remain useful.
    """
    from agent.delegation_activity import _redact

    raw = unicodedata.normalize("NFKC", html.unescape(raw[:16000]))
    raw = raw.translate(str.maketrans({'“': '"', '”': '"', '‘': "'", '’': "'"}))
    raw = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw)
    raw = "".join(ch if ch.isspace() or unicodedata.category(ch) not in {"Cc", "Cf"}
                  else "" for ch in raw)
    raw = re.sub(r"<[^>]*>", "", raw)
    raw = re.sub(r"[*]|_{2,}|~{2,}", "", raw)
    raw = _PUBLIC_ASSIGNMENT_RE.sub("[redacted]", raw)
    raw = _redact(raw)
    raw = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", raw)
    return re.sub(r"(?:https?://|www\.)\S+", "", raw)


def clean_steps(value: Any) -> Tuple[str, ...]:
    """1-6 short single-line steps from the model's ``handoff.steps``.

    Anything that is not a list of non-empty strings is dropped; extras past
    ``MAX_STEPS`` are dropped and each step is cut at a word boundary.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    out: List[str] = []
    for raw in value[:100]:
        if not isinstance(raw, str):
            continue
        from agent.delegation_checkpoint import _normalize_card_text

        # Redact BEFORE truncation so a cut credential never becomes visible.
        raw = _public_step_text(raw)
        text = _normalize_card_text(raw, MAX_STEP_CHARS) or ""
        text = re.sub(r"[<>\[\]\\\\|_*~]", " ", text)
        text = " ".join(text.split())
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


def dispatch_steps(goal: Any, steps: Any = ()) -> Tuple[str, ...]:
    """Use the task's structured public plan, else one truthful goal row.

    Never mine parent todos, history or private worker context for UI labels.
    The fallback describes the assigned work, not invented verification stages.
    """
    explicit = clean_steps(steps)
    if explicit:
        return explicit
    from agent.delegation_checkpoint import _goal_title

    public_goal = _goal_title(_public_step_text(goal)) if isinstance(goal, str) else ""
    return clean_steps([public_goal]) or ("Work on the assigned task",)


def _apply_todos_to(steps: List[List[str]], todos: Any, step_ids: List[str]) -> bool:
    """Merge observed rows by stable id, then exact public label; never position.

    Seeded stores use s1..sN. Old status-only events can tick those ids but
    cannot invent labels. New rows require actual public todo content.
    """
    if not isinstance(todos, list):
        return False
    before = [row[:] for row in steps]
    before_ids = step_ids[:]
    del step_ids[len(steps):]
    while len(step_ids) < len(steps):
        step_ids.append(f"s{len(step_ids) + 1}")
    incoming_ids = {t['id'] for t in todos[:100] if isinstance(t, dict)
                    and isinstance(t.get('id'), str) and 0 < len(t['id']) <= 128}
    seen = set()
    for todo in todos[:100]:
        if not isinstance(todo, dict):
            continue
        status = str(todo.get("status") or "").strip().lower()
        if status not in _STEP_ICON:
            continue
        raw_id = todo.get("id")
        tid = raw_id if isinstance(raw_id, str) and 0 < len(raw_id) <= 128 else ""
        if tid and tid in seen:
            continue
        if tid:
            seen.add(tid)
        labels = clean_steps([todo.get("content")])
        text = labels[0] if labels else ""
        index = step_ids.index(tid) if tid and tid in step_ids else None
        if index is None and text:
            # Reserve rows for ids still present in the snapshot, including
            # ones later in a reordered list. Equal/truncated labels are not
            # evidence that two distinct tasks share an identity.
            matches = [i for i, row in enumerate(steps) if row[0].casefold() == text.casefold()
                       and step_ids[i] not in incoming_ids]
            index = matches[0] if len(matches) == 1 else None
        if index is None:
            if not text or len(steps) >= MAX_STEPS:
                continue
            steps.append([text, status])
            step_ids.append(tid)
        else:
            if text:
                steps[index][0] = text
            steps[index][1] = status
            if tid:
                step_ids[index] = tid
    return steps != before or step_ids != before_ids


def checklist_todos(steps: Sequence[Any], step_ids: Sequence[str] = ()) -> List[Dict[str, str]]:
    """Bounded public snapshot, with private ids kept out of rendered labels."""
    return [
        {"id": step_ids[i] if i < len(step_ids) and step_ids[i] else f"s{i + 1}",
         "content": row[0], "status": row[1]}
        for i, row in enumerate(steps[:MAX_STEPS])
    ]


def clean_todos(todos: Any) -> List[Dict[str, str]]:
    """Validate persisted/observed public rows, without inferring progress."""
    rows: List[List[str]] = []
    ids: List[str] = []
    _apply_todos_to(rows, todos, ids)
    return checklist_todos(rows, ids)


def _persist_todos(did: str, index: int, todos: Any, *, merge: bool = False) -> None:
    try:
        from tools.delegation_inflight import note_checklist

        note_checklist(did, index, todos, merge=merge)
    except Exception:
        pass  # card updates remain best-effort; no live record is normal


def _finish_steps_for(steps: List[List[str]], state: str) -> None:
    if state == DONE:
        for row in steps:
            if row[1] in ("pending", "in_progress"):
                row[1] = "completed"
    else:
        # The step that was running is where it stopped / went wrong;
        # steps never reached stay as they were.
        mark = "failed" if state == FAILED else "unfinished" if state in _INCOMPLETE else "cancelled"
        for row in steps:
            if row[1] == "in_progress":
                row[1] = mark
                break


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
    reason: str = ""  # a failed helper's short plain reason (never raw error text)
    hidden: int = 0  # its todo-list calls: hidden, so not counted either
    hidden_ids: Set[str] = field(default_factory=set)
    steps: List[List[str]] = field(default_factory=list)  # its own [text, status] rows
    open: List[Tuple[str, str, str]] = field(default_factory=list)  # (call id, name, phrase)
    recent: Deque[Tuple[str, bool, int]] = field(default_factory=lambda: deque(maxlen=RECENT_TOOLS_SINGLE))  # (phrase, ok, repeats)
    step_ids: List[str] = field(default_factory=list)  # private identity, never rendered

    @property
    def label(self) -> str:
        return (self.title or "").replace("*", "").strip() or f"subagent {self.index + 1}"


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
    recent: Deque[Tuple[str, bool, int]] = field(default_factory=lambda: deque(maxlen=RECENT_TOOLS_SINGLE))  # (phrase, ok, repeats)
    # A dispatch of several helpers: one entry each, in dispatch order.  The
    # card then shows a block per helper instead of one helper's steps.
    kids: List[Child] = field(default_factory=list)
    hidden: int = 0  # the helper's todo-list calls: hidden, so not counted either
    hidden_ids: Set[str] = field(default_factory=set)
    # An outside agent job with no tool visibility (parser "none"): the card
    # says "running" instead of tool lines.
    lifecycle: bool = False
    reason: str = ""  # why it failed, in a few plain words (never raw error text)
    step_ids: List[str] = field(default_factory=list)

    @classmethod
    def start(cls, header: str, steps: Sequence[str], say: str = "") -> "Checklist":
        rows = [[s, "pending"] for s in clean_steps(steps)]
        if rows:
            rows[0][1] = "in_progress"
        return cls(header=header, steps=rows, say=say)

    # -- state -----------------------------------------------------------

    def apply_todos(self, todos: Any) -> bool:
        """Merge the helper's observed todos, including new rows, by identity.

        Finished cards are immutable; late events cannot reopen them.
        """
        if self.state != RUNNING:
            return False
        return _apply_todos_to(self.steps, todos, self.step_ids)

    def finish(self, state: str) -> bool:
        """Close the checklist.  A second call changes nothing."""
        if self.state != RUNNING or state not in _END_ICON:
            return False
        self.state = state
        _finish_steps_for(self.steps, state)
        return True

    # -- text ------------------------------------------------------------

    def render(
        self, summary_chars: int = SUMMARY_CHARS, tool_cap: int = RECENT_TOOLS_MULTI,
        include_say: bool = True,
    ) -> str:
        if unified():
            if self.kids:
                return self.render_unified_multi(
                    summary_chars=summary_chars, tool_cap=tool_cap, include_say=include_say,
                )
            return self.render_unified(include_say=include_say)
        return self.render_classic(include_say=include_say)

    def _title_outcome(self) -> Tuple[str, str]:
        """(title, outcome) of the header line: no status icon, no markdown."""
        rest = self.header
        for ic in (ACTIVE_ICON, DONE_ICON, FAILED_ICON, STOPPED_ICON):
            if rest.startswith(ic):
                rest = rest[len(ic):].strip()
                break
        work, _, outcome = rest.partition(" \u2192 ")
        outcome = outcome.replace(", ~", " \u00b7 ~", 1)
        if self.state != RUNNING and " \u00b7 ~" in outcome:
            outcome = outcome.split(" \u00b7 ~", 1)[0]
        return work.replace("*", "").strip(), outcome.replace("*", "").strip()

    def shown_kids(self) -> List["Child"]:
        """The helpers the card shows: one stopped on purpose is gone from it."""
        return [k for k in self.kids if k.state != STOPPED]

    @property
    def all_stopped(self) -> bool:
        """Every subagent of this job was stopped on purpose (it has no place on a card)."""
        if self.kids:
            return all(k.state == STOPPED for k in self.kids)
        return self.state == STOPPED

    def _stopped_title(self, say: bool = True) -> str:
        title, _ = self._title_outcome()
        body = _quoted([_title_line(title, " \u00b7 stopped", True, stopped=True)])
        return f"{self.say}\n\n{body}" if (self.say and say) else body

    def _goal_line(self, outcome: str) -> str:
        return _one_row_goal(outcome)

    def _title_line(self, title: str, tail: str, single: bool = False) -> str:
        if self.state in _INCOMPLETE or self.state == DONE:
            return f"{_status_icon(self.state)} **{title}**{tail}"
        return _title_line(title, tail, self.state in (FAILED, STOPPED),
                           stopped=single and self.state == STOPPED)

    def render_unified(self, now: Optional[float] = None, include_say: bool = True) -> str:
        if self.state == STOPPED:
            return self._stopped_title(include_say)
        title, outcome = self._title_outcome()
        tail = ""
        if self.state != RUNNING:
            tail = " \u00b7 " + self.state
        stats = _stats_line(self.model, self.calls, self.started, self.ended, now)
        steps = [f"{_step_icon(status, unified_step=True)} {text}" for text, status in self.steps]
        if self.state == RUNNING:
            # One live subagent, in this order (no indent, no \u25a0): line 1 is
            # the model / calls / time (visible when the quote is collapsed,
            # with the hint under it), then the bold title, the Goal, the
            # subtasks and the tool calls.  Before any stats exist the title
            # leads instead.
            lines = [f"**{title}**"] if stats else []
            lines += [self._goal_line(outcome)]
            lines += _steps_with_tools(
                self.steps, _live_lines(self, RECENT_TOOLS_SINGLE, TOOL_PHRASE_SINGLE), "", _INDENT,
            )
            head = f"{TITLE_ICON} {stats}" if stats else self._title_line(title, tail, single=True)
            body = "\n".join([head] + [f"> {q}" for q in lines if q])
            return f"{self.say}\n\n{body}" if (self.say and include_say) else body
        head = self._title_line(title, tail, single=True)
        blocks: List[List[str]] = [[ln for ln in (self._goal_line(outcome), stats) if ln], steps]
        if self.summary:
            blocks.append([no_command_links(self.summary.replace("*", ""))])
        body = "\n".join([head] + [f"> {q}" for q in _join_blocks(blocks)])
        return f"{self.say}\n\n{body}" if (self.say and include_say) else body

    def render_unified_multi(
        self, now: Optional[float] = None, summary_chars: int = SUMMARY_CHARS,
        tool_cap: int = RECENT_TOOLS_MULTI, include_say: bool = True,
    ) -> str:
        """Goal, then one block per subagent (name + status + model + calls +
        time, its steps, its tool lines or its summary); a totals line last."""
        now = time.monotonic() if now is None else now
        kids = self.shown_kids()
        if not kids:
            return self._stopped_title(include_say)
        title, outcome = self._title_outcome()
        if len(kids) >= 2:
            # Two or more subagents: the header says how many; the job's own
            # title (and its Goal) moves inside, above the sections.
            head = self._title_line(
                _joined_title(len(kids)),
                _count_tail(self.state == RUNNING, [k.state for k in kids]),
            )
            blocks: List[List[str]] = [[f"**{title}**", self._goal_line(outcome)]]
        else:
            head = self._title_line(
                title, _count_tail(self.state == RUNNING, [k.state for k in kids]),
            )
            blocks = [[ln for ln in (self._goal_line(outcome),) if ln]]
        if len(kids) > COMPACT_OVER:
            blocks += _compact_kids(kids, now, tool_cap)
        else:
            blocks += _full_kids(kids, now, summary_chars, tool_cap)
        started = [k.started for k in kids if k.started is not None]
        ended = [k.ended for k in kids if k.ended is not None]
        calls = sum(k.calls for k in kids)
        foot = _footer_counts([k.state for k in kids], self.state == RUNNING)
        if calls:
            foot.append(_calls(calls))
        if started:
            end = max(ended) if self.state != RUNNING and ended else now
            foot.append(_elapsed(max(0.0, end - min(started))))
        blocks.append([_italic(" \u00b7 ".join(foot))])
        body = "\n".join([head] + [f"> {q}" for q in _join_blocks(blocks)])
        return f"{self.say}\n\n{body}" if (self.say and include_say) else body

    def render_classic(self, include_say: bool = True) -> str:
        head = self.header
        if self.state != RUNNING:
            head = _status_icon(self.state) + _after_icon(head)
            if self.state in _INCOMPLETE:
                head += " · " + self.state
        states = [kid.state for kid in self.kids]
        if len(set(states)) > 1:
            head += _count_tail(self.state == RUNNING, states)
        lines = [head]
        lines += [f"{_step_icon(status)} {text}" for text, status in self.steps]
        body = "\n".join(lines)
        return f"{self.say}\n\n{body}" if (self.say and include_say) else body


_SPACER = "\u2800"
# Two EM SPACEs: Telegram keeps these at the start of a quoted line (an
# ordinary space is trimmed), so a subagent's lines sit under its name.
_INDENT = "\u2003\u2003"


def _indent(lines: Sequence[str]) -> List[str]:
    return [_INDENT + ln for ln in lines if ln]


def _join_blocks(blocks: Sequence[Sequence[str]]) -> List[str]:
    """The non-empty blocks with exactly one spacer between them."""
    out: List[str] = []
    for block in blocks:
        lines = [ln for ln in block if ln]
        if not lines:
            continue
        if out:
            out.append(_SPACER)
        out += lines
    return out


def _quoted(lines: Sequence[str]) -> str:
    """``lines`` in a plain ``>`` blockquote: the card's bar.  A finished card
    is short and NOT expandable (no ``**>`` opener, no ``||`` closer, no hint)."""
    return "\n".join(f"> {ln}" for ln in lines if ln)


def _italic(text: str) -> str:
    text = (text or "").replace("*", "").strip()
    return f"*{text}*" if text else ""


def _duration(seconds: float) -> str:
    try:
        from agent.delegation_activity import short_duration

        return short_duration(seconds)
    except Exception:
        return f"{int(seconds)}s"


CALLS_ICON = "\U0001f6e0"  # 🛠 leads every calls stat
TIME_ICON = "\u23f1"  # ⏱ leads every time stat


def _calls(n: int) -> str:
    return f"{CALLS_ICON} {n} call{'s' if n != 1 else ''}"


def _elapsed(seconds: float) -> str:
    return f"{TIME_ICON} {_duration(seconds)}"


def _stats_parts(
    model: str, calls: int, started: Optional[float], ended: Optional[float], now: Optional[float],
) -> List[str]:
    parts: List[str] = []
    if model:
        parts.append(model)
    if calls:
        parts.append(_calls(calls))
    if started is not None:
        end = ended if ended is not None else (now if now is not None else time.monotonic())
        parts.append(_elapsed(max(0.0, end - started)))
    return parts


def _stats_line(
    model: str, calls: int, started: Optional[float], ended: Optional[float], now: Optional[float],
) -> str:
    return " \u00b7 ".join(_stats_parts(model, calls, started, ended, now))


def _cut_word(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "\u2026"


def _kid_step_lines(kid: Any) -> List[str]:
    """The helper's own checklist (nothing when it has none)."""
    return [f"{_step_icon(status, unified_step=True)} {text}" for text, status in kid.steps]


def _steps_with_tools(
    steps: Sequence[Tuple[str, str]], tools: Sequence[str], step_indent: str, tool_indent: str,
) -> List[str]:
    """The checklist with the tool lines NESTED under the step in progress
    (``tool_indent``, one level deeper than ``step_indent``), so a step (``\u2713``
    / ``\u25b8`` / ``\u25cb``) never reads like a tool call (``\U0001f527`` / ``\u21b3`` / ``\u2717``).
    The steps after it (the ``\u25cb`` ones) follow the tool lines.  With no step
    in progress the tool lines sit at the step indent, just before the first
    step still to do (after all of them when there is none)."""
    rows = list(steps)
    nest = tool_indent
    at = next((i for i, (_, st) in enumerate(rows) if st == "in_progress"), None)
    if at is None:
        nest = step_indent
        at = next((i - 1 for i, (_, st) in enumerate(rows) if st == "pending"), len(rows) - 1)
    out: List[str] = []
    if at < 0:
        out += [nest + t for t in tools]
    for i, (text, status) in enumerate(rows):
        out.append(f"{step_indent}{_step_icon(status, unified_step=True)} {text}")
        if i == at:
            out += [nest + t for t in tools]
    return out


def _joined_title(subagents: int) -> str:
    """The title of a card that joined several jobs: ``3 subagents`` (all the
    subagents on the card, not the number of jobs).  The names stay in the
    sections below (each ``■ **name**`` line and its ``Goal:``)."""
    return f"{subagents} subagent{'s' if subagents != 1 else ''}"


JOINED_GOAL_CHARS = 40  # a goal inside a joined card's section: one phone row


GOAL_ROW_CHARS = 40  # the outcome on the Goal line: one phone row with the ETA


def _one_row_goal(outcome: str) -> str:
    """``Goal: <outcome> \u00b7 ~ETA`` held to one phone row: the outcome is cut
    on a word with an ellipsis, the ETA (running cards only) stays whole."""
    text, sep, eta = str(outcome or "").partition(" \u00b7 ~")
    text = _cut_word(text, GOAL_ROW_CHARS)
    if not text:
        return ""
    return f"Goal: {text}" + (f" \u00b7 ~{eta.strip()}" if sep and eta.strip() else "")


def _joined_goal(outcome: str) -> str:
    """``Goal: <outcome>`` for a section of a joined card: no ETA, cut on a
    word to one phone row.  ``""`` when the part has no goal."""
    text = (outcome or "").split(" \u00b7 ~", 1)[0].strip()
    return f"Goal: {_cut_word(text, JOINED_GOAL_CHARS)}" if text else ""


def _title_line(title: str, tail: str, lost: bool, stopped: bool = False) -> str:
    """Running title, or the skin's distinct failure/stopped mark."""
    icon = _status_icon(STOPPED) if stopped else _status_icon(FAILED) if lost else TITLE_ICON
    return f"{icon} **{title}**{tail}"


def _aggregate_state(states: Sequence[str]) -> str:
    for state in (FAILED, PARTIAL, BLOCKED, UNVERIFIED, UNFINISHED, STOPPED):
        if state in states:
            return state
    return DONE if states and all(s == DONE for s in states) else RUNNING


def _outstanding_counts(states: Sequence[str]) -> List[str]:
    return [f"{states.count(state)} {state}" for state in (FAILED,) + _INCOMPLETE + (STOPPED,)
            if state in states]


def _footer_counts(states: Sequence[str], running: bool) -> List[str]:
    """The footer's count parts.  Running: live counts (``2 running``,
    ``1 waiting``, ``3 done``, ``1 failed``, ``1 stopped``; a zero is left out).
    Finished: ``5 subagents`` plus `` · 1 failed`` / `` · 1 stopped`` if any."""
    n = len(states)
    failed = sum(1 for s in states if s == FAILED)
    stopped = sum(1 for s in states if s == STOPPED)
    if running:
        counts = (
            ("running", sum(1 for s in states if s == RUNNING)),
            ("waiting", sum(1 for s in states if s == PENDING)),
            ("done", sum(1 for s in states if s == DONE)),
            ("failed", failed),
            *((state, states.count(state)) for state in _INCOMPLETE),
            ("stopped", stopped),
        )
        parts = [f"{c} {word}" for word, c in counts if c]
        if parts:
            return parts
    return [f"{n} subagent{'s' if n != 1 else ''}"] + _outstanding_counts(states)


def _count_tail(running: bool, states: Sequence[str]) -> str:
    """`` · k of N done`` while running; ``· done`` when all ended OK; else the
    count plus `` · 1 failed`` / `` · 1 stopped``."""
    n = len(states)
    done = sum(1 for s in states if s == DONE)
    if running:
        return f" \u00b7 {done} of {n} done"
    if done == n:
        return " \u00b7 done"
    bits = [f"{done} of {n} done"] + _outstanding_counts(states)
    return " \u00b7 " + " \u00b7 ".join(bits)


SECTION_MARK = "\u25a0"  # ■ leads each subagent's name on a several-subagent card


def _name_label(name: str, state: str) -> str:
    """Section name with a distinct terminal outcome mark when needed."""
    return f"{_status_icon(state) if state in (FAILED,) + _INCOMPLETE else SECTION_MARK} **{name}**"


def _visible_len(text: str) -> int:
    """Visible width of ``text`` on a phone row: markdown marks are free, an
    emoji counts 2."""
    n = 0
    for ch in (text or "").replace("*", ""):
        o = ord(ch)
        n += 2 if (o >= 0x1F000 or ch in "\u23f1\u2b1c") else 1
    return n


def _name_and_stats(label: str, name: str, parts: Sequence[str]) -> List[str]:
    """``label · parts`` on one line, or (when the name plus the stats would
    pass ``ROW_CHARS``) the label alone with the stats on their own indented
    line under it, so a long name never wraps its stats to the left edge."""
    stats = " \u00b7 ".join(p for p in parts if p)
    if not stats:
        return [label]
    if _visible_len(name) + 3 + _visible_len(stats) <= ROW_CHARS:
        return [f"{label} \u00b7 {stats}"]
    return [label, _INDENT + stats]


def _section_header(name: str, obj: Any, now: float) -> Tuple[List[str], str]:
    """``(header lines, trailing stats)`` of a section.  A short name carries its
    stats: ``■ **Name** · done · Sonnet 5.5 · 🛠 7 calls · ⏱ 35s`` (``🍂`` / ``🥀``
    before a failed / stopped name).  A name too long for the row stands alone;
    its stats come back as the second item, for the section's LAST line, so the
    Goal is always right under the name."""
    label = _name_label(name, obj.state)
    parts: List[str] = []
    if obj.state == PENDING:
        parts.append("waiting")
    else:
        if obj.state != RUNNING:
            parts.append(obj.state)
        parts += _stats_parts(obj.model, obj.calls, obj.started, obj.ended, now)
    stats = " \u00b7 ".join(p for p in parts if p)
    if not stats:
        return [label], ""
    if _visible_len(name) + 3 + _visible_len(stats) <= ROW_CHARS:
        return [f"{label} \u00b7 {stats}"], ""
    return [label], stats


class _Block(list):
    """A section's lines; ``head`` is how many of them are its header (the name,
    plus the stats line when a long name moved its stats down)."""

    head = 1


def _section_block(
    name: str, obj: Any, now: float, summary_chars: int, tool_cap: int,
) -> List[str]:
    """One RUNNING (or waiting) subagent's section: header, then its steps with
    the tool lines nested under the step in progress, then its stats if the name
    was too long.  ``obj`` is a ``Child`` or a one-subagent part.  A subagent that
    ended is NOT a section: see ``_ended_line``."""
    header, trailing = _section_header(name, obj, now)
    block = _Block(header)
    block.head = len(header)
    if obj.state == PENDING:
        # Accepted/queued helpers already have their public plan. Show it at
        # once, without pretending an unstarted child's active seed is observed
        # execution. Explicit completed/cancelled todo states remain truthful.
        waiting_steps = [(text, "pending" if status == "in_progress" else status)
                         for text, status in obj.steps]
        block.extend(_steps_with_tools(waiting_steps, [], _INDENT, _INDENT * 2))
        block.extend(_indent([_italic(trailing)]))
        return block
    tools = _live_lines(obj, tool_cap, TOOL_PHRASE_MULTI) if obj.state == RUNNING else []
    under = _steps_with_tools(obj.steps, tools, _INDENT, _INDENT * 2)
    if obj.state != RUNNING and obj.summary and summary_chars > 0:
        # one result line, cut on a word so it never wraps and loses its indent
        under.append(_INDENT + no_command_links(
            _cut_word(obj.summary.replace("*", ""), min(summary_chars, RESULT_LINE_CHARS))
        ))
    if trailing:
        under.append(_INDENT + _italic(trailing))  # a long name's stats close its section
    block.extend(under)
    return block


REASON_CHARS = 40  # a failed subagent's one reason line
_RAW_TEXT_RE = re.compile(
    r"(?i)traceback|operation interrupted|interrupted\s*:|\b\w*(?:error|exception)\b\s*:|^\s*error\b|errno|^\s*[\[{]"
)


def _is_raw(text: str) -> bool:
    return bool(_RAW_TEXT_RE.search(text or ""))


def plain_reason(kw: Dict[str, Any], plain: str) -> str:
    """A short plain reason a subagent failed: never an interrupt notice, error
    text or a traceback."""
    exit_reason = str(kw.get("exit_reason") or "").lower()
    status = str(kw.get("status") or "").lower()
    if exit_reason in {"max_iterations", "max_turns"}:
        return "ran out of steps"
    if "timeout" in status or "timed_out" in status or "timeout" in exit_reason:
        return "timed out"
    if plain and not _is_raw(plain):
        return _cut_word(plain.replace("*", ""), REASON_CHARS)
    if kw.get("outcome") == "not_done":
        return "said the job was not done"
    return "hit an error"


class _LostBlock(list):
    """The one-line forms of the subagents of a card that ended (done / failed)."""


def _ended_line(name: str, obj: Any) -> List[str]:
    """A subagent that ended, as exactly ONE line in its place in the list,
    whether or not the rest of its job is over::

        \U0001f340 **name** \u00b7 done \u00b7 Opus 5.5 \u00b7 \U0001f6e0 34 \u00b7 \u23f1 30m
        \U0001f342 **name** \u00b7 failed \u00b7 a short reason

    (A subagent stopped on purpose is not on the card at all.)"""
    if obj.state == FAILED:
        reason = no_command_links(_cut_word(getattr(obj, "reason", "") or "hit an error", REASON_CHARS))
        return [f"{_status_icon(FAILED)} **{name}** \u00b7 failed \u00b7 {reason}"]
    parts = [f"{_status_icon(obj.state)} **{name}** \u00b7 {obj.state}"]
    if obj.state in _INCOMPLETE and getattr(obj, "reason", ""):
        parts.append(no_command_links(_cut_word(obj.reason, REASON_CHARS)))
    if obj.model:
        parts.append(obj.model)
    if obj.calls:
        parts.append(f"{CALLS_ICON} {obj.calls}")
    if obj.started is not None and obj.ended is not None:
        parts.append(_elapsed(max(0.0, obj.ended - obj.started)))
    return [" \u00b7 ".join(parts)]


def _is_lost(k: Any) -> bool:
    return k.state == FAILED


def _is_ended(k: Any) -> bool:
    return k.state in _TERMINAL


def _kid_block(k: Child, now: float, summary_chars: int, tool_cap: int) -> List[str]:
    return _section_block(k.label, k, now, summary_chars, tool_cap)


def _full_kids(kids: Sequence[Child], now: float, summary_chars: int, tool_cap: int) -> List[List[str]]:
    """Up to ``COMPACT_OVER`` helpers, in their own order: a full section for each
    one still working; a helper that ended is ONE line (a run of them is one
    block), wherever its siblings are."""
    kids = [k for k in kids if k.state != STOPPED]
    blocks: List[List[str]] = []
    run: List[str] = []
    for k in kids:
        if _is_ended(k):
            run += _ended_line(k.label, k)
            continue
        if run:
            blocks.append(_LostBlock(run))
            run = []
        blocks.append(_kid_block(k, now, summary_chars, tool_cap))
    if run:
        blocks.append(_LostBlock(run))
    return blocks


def _names(kids: Sequence[Child]) -> str:
    shown = [k.label for k in kids[:COMPACT_NAMES]]
    more = len(kids) - len(shown)
    return ", ".join(shown) + ("\u2026" if more > 0 else "")


def _compact_kids(kids: Sequence[Child], now: float, tool_cap: int) -> List[List[str]]:
    """Many helpers: at most ``COMPACT_RUNNING`` running ones in full, every
    finished one as a single line (``**Name** · done · result``; a run of them
    is one block), and the ones still waiting grouped into one line."""
    kids = [k for k in kids if k.state != STOPPED]
    blocks: List[List[str]] = []
    run: List[str] = []  # consecutive finished helpers
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
            if run:
                blocks.append(run)
                run = []
            blocks.append(_kid_block(k, now, 0, min(tool_cap, RECENT_TOOLS_MULTI)))
        else:
            run += _ended_line(k.label, k)
    if run:
        blocks.append(run)
    tail: List[str] = []
    if extra_running:
        tail.append(f"more running: {_names(extra_running)}")
    if waiting:
        tail.append(f"waiting: {_names(waiting)}")
    if tail:
        blocks.append(tail)
    return blocks


TOOL_RUNNING_MARK = "\U0001f527"  # 🔧
# \u21b3 marks a FINISHED tool call (never \u2713: that only ever means a finished step); \u2717 a failed one.
TOOL_OK_MARK, TOOL_ERR_MARK = "\u21b3", "\u2717"  # ↳ ✗


def _tool_lines(obj: Any, cap: int, phrase_max: int = TOOL_PHRASE_MULTI) -> List[str]:
    """``🔧 *tool arg*`` for the open tool(s), then ``↳ *tool arg*`` (``✗`` when
    it failed) for the last finished ones, newest first.  Identical neighbours
    collapse into one line with `` ×N``; each phrase is cut to fit a row."""
    entries: List[List[Any]] = []  # [mark, phrase, repeats]
    for _, _, phrase in reversed(obj.open):
        if entries and entries[-1][:2] == [TOOL_RUNNING_MARK, phrase]:
            entries[-1][2] += 1
        else:
            entries.append([TOOL_RUNNING_MARK, phrase, 1])
    for phrase, ok, repeats in reversed(obj.recent):
        entries.append([TOOL_OK_MARK if ok else TOOL_ERR_MARK, phrase, repeats])
    lines: List[str] = []
    for mark, phrase, repeats in entries:
        suffix = f" \u00d7{repeats}" if repeats > 1 else ""
        text = short_tool_phrase(phrase, max(8, phrase_max - len(suffix)))
        if text:
            lines.append(f"{mark} {_italic(text + suffix)}")
    return lines[:cap]


def _live_lines(obj: Any, cap: int, phrase_max: int) -> List[str]:
    """A running subagent's tool lines; an outside job without tool
    visibility says ``running`` instead."""
    lines = _tool_lines(obj, cap, phrase_max)
    if not lines and getattr(obj, "lifecycle", False):
        return [_italic("running")]
    return lines


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
        "next one in_progress. Keep each item's id when you rename or reorder "
        "it. Add concrete steps you discover; do not mark work completed until "
        "you have done it."
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
_RESERVED_CHECKLISTS: Dict[str, List[List[Dict[str, str]]]] = {}
# Same reducer state as an accepted helper, never a visible part or raw history.
# Dispatch/helper cardinality is bounded by reservations and the trusted launch
# count. hidden_ids retains the live reducer's exact, unbounded legacy semantics.
_RESERVED_ACTIVITY: Dict[str, Checklist] = {}
# Only a trusted count/snapshot authorizes rejecting legacy early identities.
_RESERVED_EXPLICIT: Set[str] = set()
BIND_WINDOW_SECONDS = 180.0


@dataclass
class Part:
    delegation_id: str
    checklist: Checklist
    expected: int = 1  # children in the dispatch; it ends when all reported
    ended: List[str] = field(default_factory=list)  # end state per child
    card: Optional["Card"] = None
    ended_at: Optional[float] = None  # when the whole part ended (monotonic)
    ended_seq: int = 0  # order in which parts ended
    opened_seq: int = 0  # order in which parts were dispatched
    liner_done: bool = False  # its one-line message was posted (or it needs none)

    def _changed(self) -> None:
        card = self.card
        if card is not None:
            card.touch()


def _seed_rows(steps: Any) -> List[List[str]]:
    rows = [[s, "pending"] for s in clean_steps(steps)] if steps else []
    if rows:
        rows[0][1] = "in_progress"
    return rows


def open_part(
    delegation_id: str, header: str, steps: Sequence[str], expected: int = 1, say: str = "",
    goals: Sequence[Any] = (), child_steps: Sequence[Any] = (),
) -> Optional[Part]:
    did = str(delegation_id or "").strip()
    if not did or not str(header or "").strip():
        return None
    with _LOCK:
        _RESERVED.pop(did, None)
        _RESERVED_EXPLICIT.discard(did)
        part = _PARTS.pop(did, None)
        initial = _RESERVED_CHECKLISTS.pop(did, [])
        if part is None:
            n = max(1, int(expected or 1))
            # Seed the first-event/legacy receipt path too. Only public goals
            # and this dispatch's structured plan are eligible sources.
            single_steps = dispatch_steps(_nth(goals, 0), steps) if unified() and goals else steps
            saved = _nth(initial, 0) if n == 1 else []
            checklist = (Checklist(header=header, say=say,
                                   steps=[[t['content'], t['status']] for t in saved],
                                   step_ids=[t['id'] for t in saved]) if saved else
                         Checklist.start(header, single_steps if n == 1 else (), say))
            part = Part(did, checklist, n)
            global _OPEN_SEQ
            _OPEN_SEQ += 1
            part.opened_seq = _OPEN_SEQ
            if n > 1:
                part.checklist.kids = [
                    Child(
                        index=i, title=_goal_title(_nth(goals, i)),
                        steps=([[t['content'], t['status']] for t in initial[i]]
                               if i < len(initial) and initial[i] else
                               _seed_rows(dispatch_steps(_nth(goals, i), _nth(child_steps, i))
                                          if unified() and goals else _nth(child_steps, i))),
                        step_ids=([t['id'] for t in initial[i]] if i < len(initial) else []),
                    )
                    for i in range(n)
                ]
        _handover_activity(_RESERVED_ACTIVITY.pop(did, None), part.checklist)
        _PARTS[did] = part
        while len(_PARTS) > _MAX_PARTS:
            _PARTS.pop(next(iter(_PARTS)))
        # The helper starts running at dispatch, before the receipt that opens
        # this part exists: replay what it already reported.
        for et, kw in _EARLY.pop(did, []):
            if et == "subagent.todo":
                _apply_child_todos(part, kw)
            else:
                _record_complete(part, kw)
        return part


def open_agent_job_part(delegation_id: Any, title: Any, lifecycle: bool = False) -> Optional["Card"]:
    """Open the unified-card part of an outside agent job (``terminal`` with
    ``agent_job``) and return its own one-part card, or None (classic style,
    no title).  The gateway then joins that card to the chat's running one, or
    posts it as its own.  ``delegation_id`` must be unique per job."""
    if not unified():
        return None
    name = _cut_word(" ".join(str(title or "").replace("*", "").replace(" \u2192 ", " - ").split()), 60)
    name = name or "agent job"  # an untitled job still gets its card, never the 🔀 roster
    with _LOCK:
        part = open_part(str(delegation_id or ""), f"{ACTIVE_ICON} **{name}**", ())
        if part is None:
            return None
        part.checklist.lifecycle = bool(lifecycle)
        card = Card([part])
        card.split_opener = True  # no opener: later dispatches may join this card
        part.card = card
        return card


def open_dispatch_card(
    delegation_id: Any, goals: Sequence[Any], titles: Sequence[Any] = (), steps: Sequence[Any] = (),
    child_steps: Sequence[Any] = (), work: str = "",
) -> Optional["Card"]:
    """The unified card of a dispatch that came WITHOUT a ``handoff`` summary
    (every dispatch gets one: the 🔀 roster card is not used in unified mode).

    Title: the one task's own ``title`` or one built from its goal
    (``agent.handoff_title``), else ``N subagents``.  No opener line, no Goal
    line: no outcome is known, and the agent writes its own reply text.  Steps
    are the per-task ``steps`` when given.  Returns the new one-part card for
    the gateway to place (join to the chat's running card, or post), or None
    (classic style, nothing to show, or the dispatch already has a part).
    """
    if not unified():
        return None
    did = str(delegation_id or "").strip()
    goals = [str(g) for g in (goals or ()) if g]
    if not did or not goals:
        return None
    try:
        from agent.handoff_title import dispatch_work_title

        work = work or dispatch_work_title(goals, titles)
    except Exception:
        work = work or (f"{len(goals)} subagents" if len(goals) > 1 else "background task")
    work = _cut_word(" ".join(str(work).replace("*", "").split()), 60) or "background task"
    with _LOCK:
        if did in _PARTS:
            return None
        part = open_part(
            did, f"{ACTIVE_ICON} **{work}**", steps if len(goals) == 1 else (),
            expected=len(goals), goals=goals, child_steps=child_steps,
        )
        if part is None:
            return None
        card = Card([part])
        card.split_opener = True  # no opener: later dispatches may join this card
        part.card = card
        return card


def abandon(delegation_id: Any) -> None:
    """Drop a part that never got a card, so its events fall back to the 🔀 card."""
    with _LOCK:
        did = str(delegation_id or "")
        _PARTS.pop(did, None)
        release(did)


def reserve(
    delegation_id: Any, *, checklists: Sequence[Any] = (), expected: Optional[int] = None,
) -> bool:
    """Claim ``delegation_id`` for the unified card before its helpers start,
    so ``owns_delegation`` is true from their very first event (and the
    gateway never posts a separate 🔀 card).  ``open_part`` fulfils the
    reservation; ``release`` drops it when the dispatch is not accepted.
    Multi-helper callers supply ``expected`` (or authorized ``checklists``);
    omission stages one provisional helper without limiting legacy early replay.
    """
    did = str(delegation_id or "").strip()
    if not did:
        return False
    with _LOCK:
        _RESERVED[did] = time.monotonic()
        if did not in _PARTS and did not in _RESERVED_ACTIVITY:
            # Count-free legacy reservations stage only one provisional helper.
            # Trusted counts/snapshots, never event payloads, authorize more.
            if expected is not None or checklists:
                _RESERVED_EXPLICIT.add(did)
            n = max(1, int(expected if expected is not None else len(checklists) or 1))
            _RESERVED_ACTIVITY[did] = Checklist(
                header="", kids=[Child(index=i) for i in range(n)] if n > 1 else [],
            )
        # Only the authorized dispatch passes these one-launch snapshots.
        # Initialize exact identities under the same lock as part creation,
        # before replay/render; never merge label-only s1..sN competitors.
        if checklists and did not in _PARTS and did not in _RESERVED_CHECKLISTS:
            _RESERVED_CHECKLISTS[did] = [clean_todos(todos) for todos in checklists]
        while len(_RESERVED) > _MAX_PARTS:
            expired = next(iter(_RESERVED))
            _RESERVED.pop(expired)
            _RESERVED_EXPLICIT.discard(expired)
            _RESERVED_CHECKLISTS.pop(expired, None)
            _RESERVED_ACTIVITY.pop(expired, None)
            _EARLY.pop(expired, None)
    return True


def release(delegation_id: Any) -> None:
    did = str(delegation_id or "").strip()
    with _LOCK:
        _RESERVED.pop(did, None)
        _RESERVED_EXPLICIT.discard(did)
        _RESERVED_CHECKLISTS.pop(did, None)
        _RESERVED_ACTIVITY.pop(did, None)
        _EARLY.pop(did, None)


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
                    staged = _RESERVED_ACTIVITY.get(did)
                    explicit = did in _RESERVED_EXPLICIT
                    if explicit and staged is not None and not staged.kids and not _single_child_event(kw):
                        return False
                    # Unknown-count siblings/unresolved IDs belong to legacy
                    # early replay, not the provisional child-0 activity state.
                    fence = explicit or (_single_child_event(kw) and
                                         (kw.get("task_index") == 0 or not kw.get("subagent_id")))
                    if staged is not None and fence and et == "subagent.complete":
                        obj = _child_for(staged, kw, ending=True) if staged.kids else staged
                        if obj is None or obj.state not in (PENDING, RUNNING):
                            return False
                        # A fence only, not task-outcome authority. Ordered
                        # todo/completion replay closes the authorized rows.
                        obj.state = STOPPED
                        if isinstance(obj, Child):
                            # Resolve anonymous completion now, not after later
                            # sibling activity changes which child is running.
                            kw = {**kw, "task_index": obj.index}
                        elif not explicit:
                            # Freeze anonymous legacy completion on child 0;
                            # later sibling events cannot retarget its fence.
                            kw = {**kw, "task_index": 0}
                    early = _EARLY.setdefault(did, [])
                    if et == "subagent.todo":
                        # A completion fences its helper even before the card
                        # exists. Never replace pre-completion progress with a
                        # late list and then replay it as new running work.
                        def same_child(payload):
                            ti, sid = kw.get("task_index"), kw.get("subagent_id")
                            if not explicit and ti is None and not sid:
                                return not payload.get("subagent_id") and _single_child_event(payload)
                            if ti is not None:
                                return ti == payload.get("task_index")
                            if sid:
                                return sid == payload.get("subagent_id")
                            return payload.get("task_index") is None and not payload.get("subagent_id")

                        if any(e[0] == "subagent.complete" and same_child(e[1]) for e in early):
                            return False
                        ti = kw.get("task_index")
                        if isinstance(ti, int) and not isinstance(ti, bool):
                            _persist_todos(did, ti, kw.get("todos"), merge=True)
                        # the newest todo list of each helper is enough
                        early[:] = [
                            e for e in early
                            if not (e[0] == "subagent.todo" and same_child(e[1]))
                        ]
                    early.append((et, {k: kw.get(k) for k in ("todos", "status", "exit_reason", "outcome", "task_outcome", "summary", "preview",
                                                             "task_index", "subagent_id", "title", "goal")}))
                    while len(_EARLY) > _MAX_PARTS:
                        _EARLY.pop(next(iter(_EARLY)))
                return False
            if et == "subagent.todo":
                changed = _apply_child_todos(part, kw)
            else:
                changed = _record_complete(part, kw)
        if changed:
            part._changed()
        return changed
    except Exception:
        return False


def _apply_child_todos(part: Part, kw: Dict[str, Any]) -> bool:
    """A helper's todo list ticks ITS steps: by ``task_index`` on a several-helper
    dispatch, the dispatch's own steps otherwise."""
    cl = part.checklist
    if cl.state != RUNNING:
        return False
    if not cl.kids:
        changed = cl.apply_todos(kw.get("todos"))
        if changed:
            _persist_todos(part.delegation_id, 0, checklist_todos(cl.steps, cl.step_ids))
        return changed
    kid = _child_for(cl, kw, bind=False)
    if kid is None or kid.state not in (PENDING, RUNNING):
        return False
    changed = _apply_todos_to(kid.steps, kw.get("todos"), kid.step_ids)
    if changed:
        _persist_todos(part.delegation_id, kid.index, checklist_todos(kid.steps, kid.step_ids))
    return changed


_ACTIVITY_EVENTS = frozenset({"subagent.start", "subagent.tool", "subagent.tool_done"})


def _single_child_event(kw: Dict[str, Any]) -> bool:
    ti = kw.get("task_index")
    return ti is None or (isinstance(ti, int) and not isinstance(ti, bool) and ti == 0)


def _child_for(cl: Checklist, kw: Dict[str, Any], *, ending: bool = False, bind: bool = True) -> Optional[Child]:
    """The helper an event belongs to: ``task_index``, else ``subagent_id``.
    A completion that names neither closes the first helper still open."""
    kids = cl.kids
    ti = kw.get("task_index")
    if ti is not None:
        if not isinstance(ti, int) or isinstance(ti, bool) or not 0 <= ti < len(kids):
            return None
        if kids[ti].sid and kw.get("subagent_id") and kids[ti].sid != kw["subagent_id"]:
            return None
        return kids[ti]
    sid = str(kw.get("subagent_id") or "")
    if sid:
        for k in kids:
            if k.sid == sid:
                return k
        for k in kids if bind else ():
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
            staged = _RESERVED_ACTIVITY.get(did)
            if staged is not None:
                _reduce_activity(staged, et, kw, reserved=True)
            return False  # reservation is ownership, never visible acceptance
        changed = _reduce_activity(part.checklist, et, kw)
    if changed and unified():
        part._changed()
    return changed


def _reduce_activity(cl: Checklist, et: str, kw: Dict[str, Any], *, reserved: bool = False) -> bool:
    """The single activity reducer for both reserved and accepted helpers."""
    if cl.state != RUNNING:
        return False
    if reserved and not cl.kids and not _single_child_event(kw):
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

            obj.model = _CTX_SIZE_RE.sub("", pretty_model(str(model)) or "")
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
    return changed


_ACTIVITY_FIELDS = ("model", "calls", "hidden", "hidden_ids", "open", "recent", "doing", "started")


def _handover_activity(staged: Optional[Checklist], accepted: Checklist) -> None:
    """Move observations, never plans or terminal authority, into authorized rows.

    Container references move unchanged; the pending registry no longer owns
    them. Completion replay below supplies the terminal outcome and clears open
    calls exactly as it does after acceptance.
    """
    if staged is None or accepted.state != RUNNING:
        return
    sources = staged.kids or [staged]
    targets = accepted.kids or [accepted]
    if len(sources) != len(targets):
        return  # an inconsistent receipt cannot reinterpret helper identities
    for source, target in zip(sources, targets):
        for name in _ACTIVITY_FIELDS:
            setattr(target, name, getattr(source, name))
        if isinstance(source, Child) and isinstance(target, Child):
            target.sid = source.sid
            if source.title:
                target.title, target.titled = source.title, source.titled
            if source.started is not None:
                target.state = RUNNING


# "GPT-6.1 Sol 900k": the context size is not part of the model's name.
_CTX_SIZE_RE = re.compile(r"\s+\d+(?:\.\d+)?[kKmM]$")

# The helper's own todo bookkeeping: the steps already show it.
_HIDDEN_TOOLS = frozenset({"todo", "memory"})


# A tool line must fit one phone line: "✓ " + the phrase, about 44 characters.
TOOL_LINE_MAX = 42
_HOME_RE = re.compile(r"(?:/home/[^/\s]+|/Users/[^/\s]+)(?=/|\s|$)")
_PATH_TOKEN_RE = re.compile(r"[~\w.\-]*(?:/[\w.\-@+]+){2,}/?")


# Telegram turns "/word" at a word start into a tappable bot command.  A tool
# line must never contain one: a long path is cut to "\u2026 claude-code/docs"
# (no slash right after the ellipsis); any other "/name" gets an invisible
# WORD JOINER after the slash.
_WORD_JOINER = "\u2060"
_COMMAND_LIKE_RE = re.compile(r"(?<![\w/])/(?=[A-Za-z0-9_])")


def no_command_links(text: Any) -> str:
    """``text`` with no token that starts with ``/`` and a letter."""
    out = str(text or "").replace("\u2026/", "\u2026 ")
    return _COMMAND_LIKE_RE.sub("/" + _WORD_JOINER, out)


def _short_path(match: "re.Match[str]") -> str:
    tok = match.group(0)
    if len(tok) <= 20:
        return tok
    parts = [p for p in tok.split("/") if p]
    for keep in (2, 1):
        short = "\u2026 " + "/".join(parts[-keep:])  # no "/" after the "\u2026": see below
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
    if len(text) + (1 if cut_upstream else 0) <= limit:
        return no_command_links(text + ("\u2026" if cut_upstream else ""))
    return no_command_links(_cut_word(text + ("\u2026" if cut_upstream else ""), limit))


_SHELL_TOOLS = frozenset({"terminal", "Bash"})  # Bash: Claude's shell tool
_CD_PREFIX_RE = re.compile(r"^\s*cd\s+[^;&\n]*?\s*(?:&&|;|\n)\s*")
_ENV_PREFIX_RE = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+")
_ENV_ONLY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")


def simplify_shell_command(command: Any) -> str:
    """The part of a shell command worth showing on a tool line: a leading
    ``cd <dir> &&`` / ``cd <dir>;`` and leading ``VAR=value`` assignments are
    dropped (repeatedly).  When nothing meaningful is left, the first command
    word."""
    text = str(command or "").strip()
    rest = text
    while True:
        stripped = _CD_PREFIX_RE.sub("", rest, count=1) or rest
        if stripped == rest:
            stripped = _ENV_PREFIX_RE.sub("", rest, count=1)
        if stripped == rest:
            break
        rest = stripped
    rest = rest.strip()
    core = rest.rstrip("\u2026").strip()
    if core and not _ENV_ONLY_RE.match(core) and not re.fullmatch(r"cd(\s.*)?", core):
        return rest
    words = text.split()
    for w in words:
        if not _ENV_ONLY_RE.match(w):
            return w
    return words[0] if words else ""


def _arg(args: Any, *keys: str) -> str:
    """First non-empty string among ``keys`` in ``args``, redacted, one line."""
    if not isinstance(args, dict):
        return ""
    for key in keys:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            from agent.delegation_activity import sanitize_text

            return sanitize_text(val, 200).replace('"', "'")
        if isinstance(val, (list, tuple)) and val and isinstance(val[0], str) and val[0].strip():
            from agent.delegation_activity import sanitize_text

            return sanitize_text(val[0], 200).replace('"', "'")
    return ""


def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _domain(url: str) -> str:
    text = url.strip()
    if "://" not in text:
        text = "//" + text
    host = (urlsplit(text).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _with(verb: str, thing: str) -> str:
    return f"{verb} {thing}" if thing else verb


# Another agent's tool names (an outside job's card) read like ours.
_TOOL_ALIAS = {
    "read": "read_file", "edit": "write_file", "write": "write_file",
    "multiedit": "write_file", "grep": "search_files", "glob": "search_files",
    "websearch": "web_search", "webfetch": "web_extract", "bash": "terminal",
}
_PREVIEW_KEY = {
    "read_file": ("path",), "write_file": ("path",), "patch": ("path",),
    "search_files": ("pattern",), "web_search": ("query",), "web_extract": ("url",),
    "skill_view": ("name",),
}


_CODE_SKIP_RE = re.compile(r"^(?:import\s|from\s+\S+\s+import\s|#|[A-Za-z_][A-Za-z0-9_]*=\S*$)")


def _first_code_line(code: str) -> str:
    """The first meaningful line of a script: not blank, an import, a comment
    or a ``PY=...`` style setup line."""
    for raw in str(code or "").splitlines():
        line = raw.strip()
        if line and not _CODE_SKIP_RE.match(line):
            return line
    return ""


def tool_line_phrase(name: str, args: Any = None, preview: Any = None) -> str:
    """A tool call for a card's tool line: the REAL tool name and a short
    argument preview (``read_file config.yaml``, ``web_search "query"`` without
    the quotes, ``execute_code run_tests()``).  Shell commands are simplified by
    the caller."""
    from agent.delegation_activity import _tool_phrase, summarize_tool_call

    tool = str(name or "tool").strip() or "tool"
    low = _TOOL_ALIAS.get(tool.lower(), tool.lower())
    if tool.lower() in _TOOL_ALIAS:
        tool = low  # an outside agent's tool shows under our name for it (Read -> read_file)
    args = dict(args) if isinstance(args, dict) else {}
    if isinstance(preview, str) and preview.strip() and not preview.lstrip().startswith(("{", "[")):
        # No structured args (an outside job): the preview is the main argument.
        for key in _PREVIEW_KEY.get(low, ()):
            args.setdefault(key, preview.strip().splitlines()[0])
    if low == "mcp" or low.startswith("mcp__"):
        shown = tool.split("__")[-1].replace("_", " ").strip() or "tool"
        if low.startswith("mcp__browser") or low.startswith("mcp__playwright__"):
            dom = _domain(_arg(args, "url"))
            return f"{shown} {dom}" if dom else shown
        arg = summarize_tool_call(tool, preview, args)
        return _tool_phrase(shown, arg)
    if low == "execute_code":
        code = args.get("code")
        line = _first_code_line(code) if isinstance(code, str) else ""
        if not line and isinstance(preview, str) and not isinstance(code, str):
            line = _first_code_line(preview)
        from agent.delegation_activity import sanitize_text

        return _with(tool, sanitize_text(line, 200) if line else "")
    if low in ("read_file", "write_file", "patch"):
        return _with(tool, _basename(_arg(args, "path", "file_path")))
    if low == "web_search":
        return _with(tool, _arg(args, "query"))
    if low == "web_extract":
        urls = args.get("urls")
        first = urls[0] if isinstance(urls, (list, tuple)) and urls and isinstance(urls[0], str) else ""
        return _with(tool, _domain(first or _arg(args, "url")))
    if low == "skill_view":
        return _with(tool, _arg(args, "name"))
    if low == "browser" or low.startswith("browser_"):
        dom = _domain(_arg(args, "url"))
        return _with(tool, dom)
    return _tool_phrase(tool, summarize_tool_call(tool, preview, args))


def _tool_started(obj: Any, kw: Dict[str, Any]) -> None:
    name = str(kw.get("tool_name") or "tool")
    if name in _HIDDEN_TOOLS:
        return
    call_id = str(kw.get("tool_call_id") or "")
    if call_id and any(c == call_id for c, _, _ in obj.open):
        return  # a repeated start for a call that is already open
    try:
        from agent.delegation_activity import _tool_phrase, summarize_tool_call

        preview, args = kw.get("preview"), kw.get("args")
        if name in _SHELL_TOOLS:
            if isinstance(args, dict) and isinstance(args.get("command"), str):
                args = {**args, "command": simplify_shell_command(args["command"])}
            elif isinstance(preview, str):
                preview = simplify_shell_command(preview)
        if name in _SHELL_TOOLS:
            phrase = _tool_phrase("terminal", summarize_tool_call(name, preview, args))
        else:
            phrase = tool_line_phrase(name, args, preview)
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
    ok = not kw.get("is_error")
    if obj.recent and obj.recent[-1][:2] == (phrase, ok):
        last = obj.recent[-1]
        obj.recent[-1] = (phrase, ok, last[2] + 1)  # the same call again: ×N
    else:
        obj.recent.append((phrase, ok, 1))


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
    from agent.delegation_activity import completion_display_state

    state = completion_display_state(kw)
    if state == STOPPED or _is_raw(plain):
        plain = ""  # an interrupt notice or raw error text is never shown
    reason = plain_reason(kw, plain)
    if state in _INCOMPLETE:
        fallback = {PARTIAL: "some work remains", BLOCKED: "waiting on a dependency",
                    UNVERIFIED: "task success not verified", UNFINISHED: "work did not finish"}[state]
        # A task's prompt, paths and raw report stay off the short result card.
        safe = _public_step_text(plain)
        safe = re.sub(r"(?:[A-Za-z]:)?[/\\]\S+", "", safe)
        reason = (plain_reason(kw, safe) if kw.get("exit_reason") in {"max_iterations", "max_turns"}
                  else _cut_word(safe, REASON_CHARS) if safe and state != UNVERIFIED else fallback)
    now = time.monotonic()
    if cl.kids:
        kid = _child_for(cl, kw, ending=True)
        if kid is None or kid.state not in (PENDING, RUNNING):
            return False
        _note_identity(kid, kw)
        kid.state = state
        if state == FAILED or state in _INCOMPLETE:
            kid.reason = reason
        _finish_steps_for(kid.steps, state)
        kid.open.clear()
        if plain and not kid.summary:
            kid.summary = plain
        if kid.started is not None:
            kid.ended = now
    else:
        if plain and not cl.summary:
            cl.summary = plain
        if (state == FAILED or state in _INCOMPLETE) and not cl.reason:
            cl.reason = reason
    part.ended.append(state)
    if len(part.ended) < part.expected:
        return bool(cl.kids)
    final = _aggregate_state(part.ended)
    if cl.started is not None or any(k.started is not None for k in cl.kids):
        cl.ended = now
    cl.open.clear()
    part.checklist.finish(final)
    global _END_SEQ
    _END_SEQ += 1
    part.ended_seq, part.ended_at = _END_SEQ, now
    return True


_END_SEQ = 0
_OPEN_SEQ = 0

# (summary chars, tool lines per running helper) tried in turn until it fits.
_BUDGET_STEPS = ((SUMMARY_CHARS, RECENT_TOOLS_MULTI), (80, RECENT_TOOLS_MULTI), (0, RECENT_TOOLS_MULTI), (0, 1), (0, 0))


class Card:
    """The ack message: its parts, and (once sent) where it lives."""

    def __init__(self, parts: List[Part]) -> None:
        self.parts = parts
        self.created = time.monotonic()
        # True when the turn's reply is the opener line ALONE and the gateway
        # sends the card as its own message under it (see router_backstop).
        self.split_opener = False
        self.initial_text = self.render(include_say=True)
        self.target: Any = None  # set by the gateway: where the message is
        self.on_change: Any = None  # set by the gateway: schedule an edit

    def render(self, include_say: Optional[bool] = None) -> str:
        """The card message.  When the opener is its own message
        (``split_opener``) the card never contains it; otherwise the opener
        line leads the text (``include_say`` overrides)."""
        if include_say is None:
            include_say = not self.split_opener
        with _LOCK:
            parts = self.parts
            if unified():
                live = [p for p in parts if p.checklist.state == RUNNING]
                if live:
                    # A part that ended LEAVES the running card: it is (or will
                    # be) its own one-line message.  One part left: single layout.
                    parts = live
                else:
                    return self._render_ended(parts, include_say)
            if len(parts) > 1 and unified():
                # Dispatches that joined this card read as one card.
                text = ""
                for summary_chars, tool_cap in _BUDGET_STEPS:
                    text = self._render_joined(summary_chars, tool_cap, include_say, parts=parts)
                    if len(text) <= CARD_BUDGET:
                        break
                return text
            sep = "\n"
            text = ""
            # Over Telegram's limit: cut the helpers' summaries first, then
            # their tool lines.
            for summary_chars, tool_cap in _BUDGET_STEPS:
                text = sep.join(
                    p.checklist.render(summary_chars, tool_cap, include_say=include_say)
                    for p in parts
                )
                if len(text) <= CARD_BUDGET:
                    break
            return text

    def final_part(self) -> Optional[Part]:
        """The part whose one-liner IS the card's message once every part is
        over: the one that ended last."""
        with _LOCK:
            ended = [p for p in self.parts if p.checklist.state != RUNNING]
            return max(ended, key=lambda p: p.ended_seq) if ended else None

    def part_liner(self, part: Part, include_say: bool = False) -> str:
        """One ended part as its own one-line message (``\U0001f340 **title** \u00b7 done \u00b7
        \U0001f6e0 X \u00b7 \u23f1 t``; ``\U0001f342`` plus one short reason; ``\U0001f940`` when it was stopped)."""
        with _LOCK:
            if part.checklist.all_stopped:
                return part.checklist._stopped_title(include_say)
            return self._render_finished([part], include_say)

    def _render_ended(self, parts: Sequence[Part], include_say: bool) -> str:
        """The card once every part is over: the one-liner of the part that
        ended last (earlier parts are, or will be, their own messages)."""
        last = self.final_part() or parts[-1]
        return self.part_liner(last, include_say and len(parts) == 1)

    def _render_finished(self, parts: Sequence[Part], include_say: bool) -> str:
        """A card whose work is all over: SHORT, in a plain blockquote (the
        card's bar; not expandable, no hint, no Goal, steps or result -- the
        result is already in the chat as the agent's reply)::

            > \U0001f340 **title** \u00b7 done
            > *Opus 5.5 \u00b7 \U0001f6e0 66 \u00b7 \u23f1 8m42s*

        Several subagents: ``*3 done \u00b7 \U0001f6e0 X \u00b7 \u23f1 t*`` (``2 of 3 done
        \u00b7 1 failed`` when some failed).  A failure is ``\U0001f342 **title** \u00b7 failed``,
        the stats line, then ONE short reason line."""
        subs: List[Any] = []
        for p in parts:
            cl = p.checklist
            subs += cl.kids if cl.kids else [cl]
        n = len(subs)
        done = sum(1 for x in subs if x.state == DONE)
        failed = [x for x in subs if x.state == FAILED]
        joined = len(parts) > 1
        title = _joined_title(n) if (joined or n > 1) else parts[0].checklist._title_outcome()[0]
        stats: List[str] = []
        states = [x.state for x in subs]
        status = _aggregate_state(states)
        if n > 1:
            stats.append(f"{n} done" if done == n else
                         f"{done} of {n} done" + " · " + " · ".join(_outstanding_counts(states)))
        if n == 1 and subs[0].model:
            stats.append(subs[0].model)
        calls = sum(x.calls for x in subs)
        if calls:
            stats.append(f"{CALLS_ICON} {calls}")
        started = [x.started for x in subs if x.started is not None]
        ended = [x.ended for x in subs if x.ended is not None]
        if started and ended:
            stats.append(_elapsed(max(0.0, max(ended) - min(started))))
        lines = [f"{_status_icon(status)} **{title}** \u00b7 {status}"]
        if stats:
            lines.append(_italic(" \u00b7 ".join(stats)))
        reasons = failed or [x for x in subs if x.state in _INCOMPLETE]
        if reasons:
            reason = getattr(reasons[0], "reason", "") or "work did not finish"
            lines.append(no_command_links(_cut_word(reason, REASON_CHARS)))
        body = _quoted(lines)
        say = self.say_text()
        return f"{say}\n\n{body}" if (say and include_say) else body

    def _render_joined(
        self, summary_chars: int, tool_cap: int, include_say: bool, now: Optional[float] = None,
        parts: Optional[Sequence[Part]] = None,
    ) -> str:
        """Several dispatches on one card: one title (``N subagents``, ``k of N
        done`` across all subagents), a Goal line per part, every subagent's
        section in order, and one footer."""
        now = time.monotonic() if now is None else now
        cls = [p.checklist for p in (self.parts if parts is None else parts)]
        titles: List[str] = []
        goals: List[str] = []  # one per part ("" when it has none)
        for cl in cls:
            title, outcome = cl._title_outcome()
            titles.append(title)
            goals.append(_joined_goal(outcome))
        states: List[str] = []
        for cl in cls:
            states += [k.state for k in cl.shown_kids()] if cl.kids else [cl.state]
        running = any(cl.state == RUNNING for cl in cls)
        lost = not running and any(s == FAILED for s in states)
        head = _title_line(
            _joined_title(len(states)), _count_tail(running, states), lost,
        )
        blocks: List[List[str]] = []  # no Goal lines on top: each sits in its own section
        started: List[float] = []
        ended: List[float] = []
        calls = 0
        for cl, title, goal in zip(cls, titles, goals):
            # (parts that ended have left the card: they are their own messages)
            if cl.kids:
                shown = cl.shown_kids()
                if len(shown) > COMPACT_OVER:
                    part_blocks = _compact_kids(shown, now, tool_cap)
                else:
                    part_blocks = _full_kids(shown, now, summary_chars, tool_cap)
                subs = shown
            else:
                part_blocks = [_section_block(title, cl, now, summary_chars, tool_cap)]
                subs = [cl]
            if cl.kids and len(states) >= 2:
                # Several subagents on the card: each job's bold title (and
                # its Goal) sits above that job's own sections.
                part_blocks.insert(0, [ln for ln in (f"**{title}**", goal) if ln])
            elif goal and part_blocks and part_blocks[0]:
                # the part's goal: first line under its FIRST section's name
                first = part_blocks[0]
                if isinstance(first, _LostBlock):
                    part_blocks.insert(0, [goal])  # only collapsed lines so far
                else:
                    h = getattr(first, "head", 1)
                    part_blocks[0] = first[:h] + _indent([goal]) + first[h:]
            blocks += part_blocks
            for s in subs:
                calls += s.calls
                if s.started is not None:
                    started.append(s.started)
                if s.ended is not None:
                    ended.append(s.ended)
        foot = _footer_counts(states, running)
        if calls:
            foot.append(_calls(calls))
        if started:
            end = max(ended) if not running and ended else now
            foot.append(_elapsed(max(0.0, end - min(started))))
        blocks.append([_italic(" \u00b7 ".join(foot))])
        body = "\n".join([head] + [f"> {q}" for q in _join_blocks(blocks)])
        say = self.say_text()
        return f"{say}\n\n{body}" if (say and include_say) else body

    def say_text(self) -> str:
        """The root's opener line(s) of every part, in order ('' if none)."""
        with _LOCK:
            sep = "\n\n" if unified() else "\n"
            return sep.join(p.checklist.say for p in self.parts if p.checklist.say)

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


def pending_liner_parts(ids: Sequence[Any]) -> List[Part]:
    """Parts of these delegation ids that have ended and whose one-line
    message was not posted yet, in the order they were dispatched."""
    with _LOCK:
        found = [
            _PARTS[i] for i in dict.fromkeys(str(x) for x in ids or ())
            if i in _PARTS and _PARTS[i].checklist.state != RUNNING and not _PARTS[i].liner_done
        ]
    return sorted(found, key=lambda p: (p.opened_seq, p.ended_seq))


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
                child_steps=getattr(r, "child_steps", ()) or (),
            )
            if part is not None:
                parts.append(part)
        if not parts:
            return None
        for existing in _CARDS:
            if [id(p) for p in existing.parts] == [id(p) for p in parts]:
                return existing
        # A dispatch that joined a running card keeps that card.
        host = parts[0].card
        if host is not None and len(host.parts) > 1 and all(
            p.card is host and any(q is p for q in host.parts) for p in parts
        ):
            return host
        card = Card(parts)
        for p in parts:
            p.card = card
        _CARDS.append(card)
        while len(_CARDS) > _MAX_CARDS:
            _CARDS.pop(0)
        return card


def join(host: Card, new: Card) -> bool:
    """Add the dispatch(es) of ``new`` to the RUNNING card ``host``: one card per
    chat.  ``new`` is dropped (its opener went out as its own message; no card
    message is sent for it) and its parts report into ``host`` from now on.  A
    finished host never takes a part: the new dispatch keeps its own card."""
    with _LOCK:
        if host is new or not unified() or host.finished or not host.split_opener:
            return False
        for part in new.parts:
            if not any(q is part for q in host.parts):
                host.parts.append(part)
            part.card = host
        if new in _CARDS:
            _CARDS.remove(new)
        new.target = host.target
    host.touch()
    return True


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


_OPENER_TAIL_MAX = 8  # a streaming cursor / space after the opener, nothing more


def match_unbound_opener(content: Any) -> Optional[Card]:
    """The newest unplaced card whose opener line IS ``content`` (a cursor or
    trailing space tolerated).  Exact on purpose: an opener such as "on it" must
    never claim some later reply that merely contains those words."""
    text = str(content or "").strip()
    if not text:
        return None
    now = time.monotonic()
    with _LOCK:
        for card in reversed(_CARDS):
            if card.target is not None or now - card.created > BIND_WINDOW_SECONDS:
                continue
            if not card.split_opener:
                continue
            say = card.say_text()
            if say and text.startswith(say) and len(text) - len(say) <= _OPENER_TAIL_MAX:
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
        _RESERVED_EXPLICIT.clear()
        _RESERVED_CHECKLISTS.clear()
        _RESERVED_ACTIVITY.clear()
