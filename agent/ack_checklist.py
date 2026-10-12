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
FAILED_ICON = "\U0001f342"  # 🍂
TITLE_ICON = "\U0001f500"  # 🔀 leads a unified card's title (🍂 instead when it ended badly)
STOPPED_ICON = "\u23f9"  # ⏹

_STEP_ICON = {
    "completed": DONE_ICON,
    "in_progress": ACTIVE_ICON,
    "pending": PENDING_ICON,
    "cancelled": STOPPED_ICON,
    "failed": FAILED_ICON,
}
_END_ICON = {DONE: DONE_ICON, FAILED: FAILED_ICON, STOPPED: STOPPED_ICON}
# The unified card uses plain marks, not emojis: the only emojis on it are the
# 🔀 in front of the title (🍂 in its place when the card ended badly), the 🍀 in
# the "tap to ..." hint, a 🍂 in front of a failed subagent's name, and the
# 🛠 / ⏱ leading each stats line (calls, time).
_STEP_MARK = {
    "completed": "\u2713",  # ✓
    "in_progress": "\u25b8",  # ▸
    "pending": "\u25cb",  # ○
    "failed": "\u2717",  # ✗
    "cancelled": "\u2013",  # –
}
# Tool phrases are cut so the WHOLE line (indent + "before: " + phrase) fits one
# phone row: a few characters more on the single card (no indent).
TOOL_PHRASE_MULTI = 34
TOOL_PHRASE_SINGLE = 40
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


def _apply_todos_to(steps: List[List[str]], todos: Any) -> bool:
    """Mirror a helper's todo list onto ``steps`` by position.  True when changed."""
    if not steps or not isinstance(todos, list):
        return False
    changed = False
    for i, row in enumerate(steps):
        if i >= len(todos) or not isinstance(todos[i], dict):
            break
        status = str(todos[i].get("status") or "").strip().lower()
        if status in _STEP_ICON and row[1] != status:
            row[1] = status
            changed = True
    return changed


def _finish_steps_for(steps: List[List[str]], state: str) -> None:
    if state == DONE:
        for row in steps:
            if row[1] in ("pending", "in_progress"):
                row[1] = "completed"
    else:
        # The step that was running is where it stopped / went wrong;
        # steps never reached stay as they were.
        mark = "failed" if state == FAILED else "cancelled"
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
    hidden: int = 0  # its todo-list calls: hidden, so not counted either
    hidden_ids: Set[str] = field(default_factory=set)
    steps: List[List[str]] = field(default_factory=list)  # its own [text, status] rows
    open: List[Tuple[str, str, str]] = field(default_factory=list)  # (call id, name, phrase)
    recent: Deque[Tuple[str, bool]] = field(default_factory=lambda: deque(maxlen=RECENT_TOOLS_SINGLE))

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
    recent: Deque[Tuple[str, bool]] = field(default_factory=lambda: deque(maxlen=RECENT_TOOLS_SINGLE))
    # A dispatch of several helpers: one entry each, in dispatch order.  The
    # card then shows a block per helper instead of one helper's steps.
    kids: List[Child] = field(default_factory=list)
    hidden: int = 0  # the helper's todo-list calls: hidden, so not counted either
    hidden_ids: Set[str] = field(default_factory=set)
    # An outside agent job with no tool visibility (parser "none"): the card
    # says "running" instead of tool lines.
    lifecycle: bool = False

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
        if self.state != RUNNING:
            return False
        return _apply_todos_to(self.steps, todos)

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

    def _goal_line(self, outcome: str) -> str:
        return f"Goal: {outcome}" if outcome else ""

    def _title_line(self, title: str, tail: str) -> str:
        return _title_line(title, tail, self.state in (FAILED, STOPPED))

    def render_unified(self, now: Optional[float] = None, include_say: bool = True) -> str:
        title, outcome = self._title_outcome()
        tail = ""
        if self.state != RUNNING:
            tail = " \u00b7 " + self.state
        head = self._title_line(title, tail)
        stats = _stats_line(self.model, self.calls, self.started, self.ended, now)
        blocks: List[List[str]] = [
            [ln for ln in (self._goal_line(outcome), stats) if ln],
            [f"{_STEP_MARK[status]} {text}" for text, status in self.steps],
        ]
        if self.state == RUNNING:
            blocks.append(_live_lines(self, RECENT_TOOLS_SINGLE, TOOL_PHRASE_SINGLE))
        elif self.summary:
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
        title, outcome = self._title_outcome()
        kids = self.kids
        head = self._title_line(title, _count_tail(self.state == RUNNING, [k.state for k in kids]))
        blocks: List[List[str]] = [[ln for ln in (self._goal_line(outcome),) if ln]]
        if len(kids) > COMPACT_OVER:
            blocks += _compact_kids(kids, now, tool_cap)
        else:
            blocks += _full_kids(kids, now, summary_chars, tool_cap)
        started = [k.started for k in kids if k.started is not None]
        ended = [k.ended for k in kids if k.ended is not None]
        calls = sum(k.calls for k in kids)
        foot = [f"{len(kids)} subagent{'s' if len(kids) != 1 else ''}"]
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
            head = _END_ICON[self.state] + _after_icon(head)
        lines = [head]
        lines += [f"{_STEP_ICON[status]} {text}" for text, status in self.steps]
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
    return [f"{_STEP_MARK[status]} {text}" for text, status in kid.steps]


def _joined_title(titles: Sequence[str]) -> str:
    """The title of a card that joined several jobs: ``3 jobs``.  The names stay
    in the sections below (each ``■ **name**`` line and its ``Goal:``)."""
    return f"{len(titles)} jobs"


JOINED_GOAL_CHARS = 40  # a goal inside a joined card's section: one phone row


def _joined_goal(outcome: str) -> str:
    """``Goal: <outcome>`` for a section of a joined card: no ETA, cut on a
    word to one phone row.  ``""`` when the part has no goal."""
    text = (outcome or "").split(" \u00b7 ~", 1)[0].strip()
    return f"Goal: {_cut_word(text, JOINED_GOAL_CHARS)}" if text else ""


def _title_line(title: str, tail: str, lost: bool) -> str:
    """``🔀 **title** · k of N done``; 🍂 takes the 🔀's place when it ended badly."""
    return f"{FAILED_ICON if lost else TITLE_ICON} **{title}**{tail}"


def _count_tail(running: bool, states: Sequence[str]) -> str:
    """`` · k of N done`` while running; ``· done`` when all ended OK; else the
    count plus `` · 1 failed`` / `` · 1 stopped``."""
    n = len(states)
    done = sum(1 for s in states if s == DONE)
    failed = sum(1 for s in states if s == FAILED)
    stopped = sum(1 for s in states if s == STOPPED)
    if running:
        return f" \u00b7 {done} of {n} done"
    if done == n:
        return " \u00b7 done"
    bits = [f"{done} of {n} done"]
    if failed:
        bits.append(f"{failed} failed")
    if stopped:
        bits.append(f"{stopped} stopped")
    return " \u00b7 " + " \u00b7 ".join(bits)


SECTION_MARK = "\u25a0"  # ■ leads each subagent's name on a several-subagent card


def _name_label(name: str, state: str) -> str:
    """``■ **Name**`` (🍂 instead of the ■ for a failed subagent)."""
    return f"{FAILED_ICON if state == FAILED else SECTION_MARK} **{name}**"


def _visible_len(text: str) -> int:
    """Visible width of ``text`` on a phone row: markdown marks are free, an
    emoji counts 2."""
    n = 0
    for ch in (text or "").replace("*", ""):
        o = ord(ch)
        n += 2 if (o >= 0x1F000 or ch in "\u23f1\u2b1c\u23f9") else 1
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


def _section_header(name: str, obj: Any, now: float) -> List[str]:
    """``■ **Name** · done · Sonnet 5.5 · 🛠 7 calls · ⏱ 35s`` (``🍂`` before a
    failed name); a long name puts the stats on the next indented line."""
    label = _name_label(name, obj.state)
    parts: List[str] = []
    if obj.state == PENDING:
        parts.append("waiting")
    else:
        if obj.state != RUNNING:
            parts.append(obj.state)
        parts += _stats_parts(obj.model, obj.calls, obj.started, obj.ended, now)
    return _name_and_stats(label, name, parts)


class _Block(list):
    """A section's lines; ``head`` is how many of them are its header (the name,
    plus the stats line when a long name moved its stats down)."""

    head = 1


def _section_block(
    name: str, obj: Any, now: float, summary_chars: int, tool_cap: int,
) -> List[str]:
    """One subagent's section: header, then its steps, tool lines or summary
    (indented under the name).  ``obj`` is a ``Child`` or a one-subagent part."""
    header = _section_header(name, obj, now)
    block = _Block(header)
    block.head = len(header)
    if obj.state == PENDING:
        return block
    under = _kid_step_lines(obj)
    if obj.state == RUNNING:
        under += _live_lines(obj, tool_cap, TOOL_PHRASE_MULTI)
    elif obj.summary and summary_chars > 0:
        # one result line, cut on a word so it never wraps and loses its indent
        under.append(no_command_links(
            _cut_word(obj.summary.replace("*", ""), min(summary_chars, RESULT_LINE_CHARS))
        ))
    block.extend(_indent(under))
    return block


def _kid_block(k: Child, now: float, summary_chars: int, tool_cap: int) -> List[str]:
    return _section_block(k.label, k, now, summary_chars, tool_cap)


def _full_kids(kids: Sequence[Child], now: float, summary_chars: int, tool_cap: int) -> List[List[str]]:
    """Up to ``COMPACT_OVER`` helpers: a full block each."""
    return [_kid_block(k, now, summary_chars, tool_cap) for k in kids]


def _names(kids: Sequence[Child]) -> str:
    shown = [k.label for k in kids[:COMPACT_NAMES]]
    more = len(kids) - len(shown)
    return ", ".join(shown) + ("\u2026" if more > 0 else "")


def _compact_kids(kids: Sequence[Child], now: float, tool_cap: int) -> List[List[str]]:
    """Many helpers: at most ``COMPACT_RUNNING`` running ones in full, every
    finished one as a single line (``**Name** · done · result``; a run of them
    is one block), and the ones still waiting grouped into one line."""
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
            line = f"{_name_label(k.label, k.state)} \u00b7 {k.state}"
            if k.summary:
                line += " \u00b7 " + no_command_links(
                    _cut_word(k.summary.replace("*", ""), COMPACT_RESULT_CHARS)
                )
            run.append(line)
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


def _tool_lines(obj: Any, cap: int, phrase_max: int = TOOL_PHRASE_MULTI) -> List[str]:
    """``*now: ...*`` for the open tool, then ``*before: ...*`` for the last
    finished ones (newest first).  Each phrase is cut so the line fits a row."""
    def line(label: str, phrase: str, failed: bool = False) -> str:
        suffix = " (failed)" if failed else ""
        # A shell command reads on its own: "now: pytest -q", not "now: terminal pytest -q".
        for shell in ("terminal ", "Bash "):
            if phrase.startswith(shell):
                phrase = phrase[len(shell):]
                break
        text = short_tool_phrase(phrase, max(8, phrase_max - len(suffix)))
        return _italic(f"{label}: {text}{suffix}")

    lines: List[str] = []
    if obj.open:
        lines.append(line("now", obj.open[-1][2]))
    lines += [line("before", phrase, not ok) for phrase, ok in reversed(obj.recent)]
    return [ln for ln in lines if ln][:cap]


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
        part = _PARTS.pop(did, None)
        if part is None:
            n = max(1, int(expected or 1))
            # Several helpers share the one card: a block each, no step list.
            part = Part(did, Checklist.start(header, steps if n == 1 else (), say), n)
            if n > 1:
                part.checklist.kids = [
                    Child(
                        index=i, title=_goal_title(_nth(goals, i)),
                        steps=_seed_rows(_nth(child_steps, i)),
                    )
                    for i in range(n)
                ]
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
    if not name:
        return None
    with _LOCK:
        part = open_part(str(delegation_id or ""), f"{ACTIVE_ICON} **{name}**", ())
        if part is None:
            return None
        part.checklist.lifecycle = bool(lifecycle)
        card = Card([part])
        card.split_opener = True  # no opener: later dispatches may join this card
        part.card = card
        return card


def abandon(delegation_id: Any) -> None:
    """Drop a part that never got a card, so its events fall back to the 🔀 card."""
    with _LOCK:
        _PARTS.pop(str(delegation_id or ""), None)


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
                        # the newest todo list of each helper is enough
                        early[:] = [
                            e for e in early
                            if not (e[0] == "subagent.todo" and e[1].get("task_index") == kw.get("task_index"))
                        ]
                    early.append((et, {k: kw.get(k) for k in ("todos", "status", "exit_reason", "outcome", "summary", "preview",
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
    if not cl.kids:
        return cl.apply_todos(kw.get("todos"))
    kid = _child_for(cl, kw)
    if kid is None or kid.state not in (PENDING, RUNNING):
        return False
    return _apply_todos_to(kid.steps, kw.get("todos"))


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
    if changed and unified():
        part._changed()
    return changed


# "GPT-6.1 Sol 900k": the context size is not part of the model's name.
_CTX_SIZE_RE = re.compile(r"\s+\d+(?:\.\d+)?[kKmM]$")

# The helper's own todo bookkeeping: the steps already show it.
_HIDDEN_TOOLS = frozenset({"todo"})


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
    if len(text) <= limit:
        return no_command_links(text + ("\u2026" if cut_upstream else ""))
    return no_command_links(_cut_word(text, limit))


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
        phrase = _tool_phrase(name, summarize_tool_call(name, preview, args))
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
    if kw.get("exited_unverified") and str(kw.get("status") or "").lower() == "incomplete":
        # An outside job that exited 0 without a verdict line (parser "none"):
        # it ended cleanly, so it is done, not failed.
        state = DONE
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
        _finish_steps_for(kid.steps, state)
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


def _collapsed_part_lines(title: str, cl: "Checklist", subs: Sequence[Any]) -> List[str]:
    """A finished job of a joined card, as ONE line (its results are already in
    the chat): ``🍀 **title** · 3 done · 🛠 12 · ⏱ 2m10s``; ``🍂`` and the
    counts when something failed or stopped.  A long title puts the stats on the
    next indented line."""
    states = [s.state for s in subs]
    n = len(states)
    done = sum(1 for st in states if st == DONE)
    failed = sum(1 for st in states if st == FAILED)
    stopped = sum(1 for st in states if st == STOPPED)
    lost = failed or stopped
    if not lost:
        status = f"{n} done" if n > 1 else ""
    elif n == 1:
        status = FAILED if failed else STOPPED
    else:
        bits = [f"{done} of {n} done"]
        if failed:
            bits.append(f"{failed} failed")
        if stopped:
            bits.append(f"{stopped} stopped")
        status = " \u00b7 ".join(bits)
    calls = sum(s.calls for s in subs)
    started = [s.started for s in subs if s.started is not None]
    ended = [s.ended for s in subs if s.ended is not None]
    stats = []
    if calls:
        stats.append(f"{CALLS_ICON} {calls}")
    if started and ended:
        stats.append(_elapsed(max(0.0, max(ended) - min(started))))
    label = f"{FAILED_ICON if lost else DONE_ICON} **{title}**"
    if status:
        # the status stays with the name; only the stats may move down
        label_status = f"{label} \u00b7 {status}"
        if not stats:
            return [label_status]
        stats_text = " \u00b7 ".join(stats)
        if _visible_len(title) + 3 + _visible_len(status) + 3 + _visible_len(stats_text) <= ROW_CHARS:
            return [f"{label_status} \u00b7 {stats_text}"]
        return [label_status, _INDENT + stats_text]
    return _name_and_stats(label, title, stats)


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
            if len(self.parts) > 1 and unified():
                # Dispatches that joined this card read as one card.
                text = ""
                for summary_chars, tool_cap in _BUDGET_STEPS:
                    text = self._render_joined(summary_chars, tool_cap, include_say)
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
                    for p in self.parts
                )
                if len(text) <= CARD_BUDGET:
                    break
            return text

    def _render_joined(
        self, summary_chars: int, tool_cap: int, include_say: bool, now: Optional[float] = None,
    ) -> str:
        """Several dispatches on one card: one title (``N jobs``, ``k of M
        done`` across all subagents), a Goal line per part, every subagent's
        section in order, and one footer."""
        now = time.monotonic() if now is None else now
        cls = [p.checklist for p in self.parts]
        titles: List[str] = []
        goals: List[str] = []  # one per part ("" when it has none)
        for cl in cls:
            title, outcome = cl._title_outcome()
            titles.append(title)
            goals.append(_joined_goal(outcome))
        states: List[str] = []
        for cl in cls:
            states += [k.state for k in cl.kids] if cl.kids else [cl.state]
        running = any(cl.state == RUNNING for cl in cls)
        lost = not running and any(cl.state in (FAILED, STOPPED) for cl in cls)
        head = _title_line(
            _joined_title(titles), _count_tail(running, states), lost,
        )
        blocks: List[List[str]] = []  # no Goal lines on top: each sits in its own section
        collapsed: List[str] = []  # finished jobs: one line each, below the running ones
        started: List[float] = []
        ended: List[float] = []
        calls = 0
        for cl, title, goal in zip(cls, titles, goals):
            if cl.state != RUNNING:
                # a finished job of a joined card: its results are in the chat already
                subs = list(cl.kids) if cl.kids else [cl]
                collapsed += _collapsed_part_lines(title, cl, subs)
                for s in subs:
                    calls += s.calls
                    if s.started is not None:
                        started.append(s.started)
                    if s.ended is not None:
                        ended.append(s.ended)
                continue
            if cl.kids:
                if len(cl.kids) > COMPACT_OVER:
                    part_blocks = _compact_kids(cl.kids, now, tool_cap)
                else:
                    part_blocks = _full_kids(cl.kids, now, summary_chars, tool_cap)
                subs = list(cl.kids)
            else:
                part_blocks = [_section_block(title, cl, now, summary_chars, tool_cap)]
                subs = [cl]
            if goal and part_blocks and part_blocks[0]:
                # the part's goal: first line under its FIRST section's name
                first = part_blocks[0]
                h = getattr(first, "head", 1)
                part_blocks[0] = first[:h] + _indent([goal]) + first[h:]
            blocks += part_blocks
            for s in subs:
                calls += s.calls
                if s.started is not None:
                    started.append(s.started)
                if s.ended is not None:
                    ended.append(s.ended)
        if collapsed:
            blocks.append(collapsed)
        foot = [f"{len(states)} subagent{'s' if len(states) != 1 else ''}"]
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
