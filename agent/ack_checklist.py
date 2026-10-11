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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

MAX_STEPS = 6
MAX_STEP_CHARS = 60
MAX_SAY_CHARS = 200

RUNNING = "running"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"

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
class Checklist:
    """One dispatch's header line + steps + (once over) its result."""

    header: str
    steps: List[List[str]] = field(default_factory=list)  # [text, status]
    state: str = RUNNING
    say: str = ""

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

    def render(self) -> str:
        head = self.header
        if self.state != RUNNING:
            head = _END_ICON[self.state] + _after_icon(head)
        lines = [head]
        lines += [f"{_STEP_ICON[status]} {text}" for text, status in self.steps]
        body = "\n".join(lines)
        return f"{self.say}\n\n{body}" if self.say else body


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
) -> Optional[Part]:
    did = str(delegation_id or "").strip()
    if not did or not str(header or "").strip():
        return None
    with _LOCK:
        part = _PARTS.pop(did, None)
        if part is None:
            part = Part(did, Checklist.start(header, steps, say), max(1, int(expected or 1)))
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


def on_child_event(event_type: Any, **kw: Any) -> bool:
    """Fold one relayed ``subagent.*`` event into its dispatch's checklist.

    ``subagent.todo`` carries the helper's todo list; ``subagent.complete``
    its end status and summary.  True when something visible changed.
    Never raises.
    """
    try:
        et = str(event_type or "")
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
                    early.append((et, {k: kw.get(k) for k in ("todos", "status", "exit_reason")}))
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


def _record_complete(part: Part, kw: Dict[str, Any]) -> bool:
    if part.checklist.state != RUNNING:
        return False
    state = end_state_for(kw.get("status"))
    if str(kw.get("exit_reason") or "").lower() in {"max_iterations", "max_turns"}:
        state = FAILED
    if state == DONE and kw.get("outcome") == "not_done":
        # The run ended cleanly but the helper says the job is not done.
        state = FAILED
    part.ended.append(state)
    if len(part.ended) < part.expected:
        return False
    final = FAILED if FAILED in part.ended else STOPPED if STOPPED in part.ended else DONE
    return part.checklist.finish(final)


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
            return "\n".join(p.checklist.render() for p in self.parts)

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


def reset_for_tests() -> None:
    with _LOCK:
        _PARTS.clear()
        _CARDS.clear()
        _EARLY.clear()
