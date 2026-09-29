"""Per-turn flavor for the built-in ``clover`` skin ("Clo" the clover sprite).

The skin itself is pure data (faces, verbs, tool emojis).  This module holds
the small amount of *behavior* that needs per-turn state: the once-per-turn
lucky roll, time-of-day / long-task picks, and the bad-luck / luck's-back
status after a failed tool.  Randomness and the clock are injectable so tests
never depend on real time or chance.
"""

from __future__ import annotations

import random
import sys
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional, Tuple

LUCKY_ODDS = 50  # 1 in 50 turns is a lucky turn
MIX_ODDS = 3  # special statuses show ~1/3 of the time when they apply
LONG_TASK_SECONDS = 180.0
GROWTH_THRESHOLDS = (5.0, 20.0, 60.0)
GROWTH_FRAMES = ("🫘", "🌱", "🌿", "☘️")

NORMAL_LEAF = "☘️"
LUCKY_LEAF = "🍀"

NIGHT_STATUS = ("☘️(－_－)zzZ", "night shift")
MORNING_STATUS = ("☘️(◕ᴗ◕)☀️", "morning dew")
LONG_TASK_STATUS = ("☘️(；￣Д￣)", "still digging")
BAD_LUCK_STATUS = ("☘️(╥﹏╥)", "bad luck, retrying")
LUCK_BACK_STATUS = ("☘️(ﾉ◕ヮ◕)ﾉ", "luck's back!")
LUCKY_TURN_VERB = "lucky turn!"

_ENCODE_PROBE = "☘️🫘🍀"


def emoji_safe(stream: Any = None) -> bool:
    """True when *stream* (default ``sys.stdout``) can encode the skin's emoji.

    Legacy consoles (cp1252 and friends) cannot; callers fall back to the
    classic faces instead of crashing or printing garbage.
    """
    stream = stream if stream is not None else sys.stdout
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return True  # StringIO / pipes without a declared encoding: nothing to break
    try:
        _ENCODE_PROBE.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def skin_flavor_enabled(skin: Any) -> bool:
    """True when *skin* opts into the clover flavor and the console can show it."""
    spinner = getattr(skin, "spinner", None) or {}
    return spinner.get("flavor") == "clover" and emoji_safe()


def growth_frame(elapsed: float) -> str:
    """Growing-clover frame for *elapsed* seconds of turn time."""
    for limit, frame in zip(GROWTH_THRESHOLDS, GROWTH_FRAMES):
        if elapsed < limit:
            return frame
    return GROWTH_FRAMES[-1]


def _with_leaf(face: str, lucky: bool) -> str:
    if lucky and face.startswith(NORMAL_LEAF):
        return LUCKY_LEAF + face[len(NORMAL_LEAF):]
    return face


class TurnFlavor:
    """Mutable state for one turn."""

    def __init__(
        self,
        rng: Optional[Any] = None,
        clock: Optional[Callable[[], datetime]] = None,
        monotonic: Optional[Callable[[], float]] = None,
    ) -> None:
        self.rng = rng if rng is not None else random.Random()
        self._clock = clock or datetime.now
        self._monotonic = monotonic or time.monotonic
        self.started = self._monotonic()
        # The one and only lucky roll for this turn.
        self.lucky = self.rng.randrange(LUCKY_ODDS) == 0
        self._lucky_announced = False
        self._pending: Optional[str] = None  # "bad_luck" | "luck_back"
        self._after_error = False

    def elapsed(self) -> float:
        return self._monotonic() - self.started

    def note_tool_result(self, is_error: bool) -> None:
        if is_error:
            self._after_error = True
            self._pending = "bad_luck"
        elif self._after_error:
            self._after_error = False
            self._pending = "luck_back"

    def _special(self, *, consume_pending: bool = True) -> Optional[Tuple[str, str]]:
        if self._pending and consume_pending:
            kind, self._pending = self._pending, None
            return BAD_LUCK_STATUS if kind == "bad_luck" else LUCK_BACK_STATUS
        if self.elapsed() > LONG_TASK_SECONDS and self.rng.randrange(MIX_ODDS) == 0:
            return LONG_TASK_STATUS
        hour = self._clock().hour
        if hour < 6 and self.rng.randrange(MIX_ODDS) == 0:
            return NIGHT_STATUS
        if 6 <= hour < 10 and self.rng.randrange(MIX_ODDS) == 0:
            return MORNING_STATUS
        return None

    def status(self, faces: list, verbs: list) -> Tuple[str, str]:
        """Pick ``(face, verb)`` for the next status line."""
        special = self._special()
        if special is not None:
            face, verb = special
        else:
            face, verb = self.rng.choice(faces), self.rng.choice(verbs)
        if self.lucky and not self._lucky_announced:
            self._lucky_announced = True
            verb = LUCKY_TURN_VERB
        return _with_leaf(face, self.lucky), verb

    def face(self, faces: list) -> str:
        """Pick a face only (waiting lines carry no verb)."""
        # Waiting lines never consume the error status; the next thinking
        # status owns "bad luck" / "luck's back".
        special = self._special(consume_pending=False)
        face = special[0] if special is not None else self.rng.choice(faces)
        return _with_leaf(face, self.lucky)


_lock = threading.Lock()
_turn: Optional[TurnFlavor] = None


def begin_turn(**kwargs: Any) -> TurnFlavor:
    """Start a new turn (rolls the lucky die exactly once)."""
    global _turn
    with _lock:
        _turn = TurnFlavor(**kwargs)
        return _turn


def current_turn() -> Optional[TurnFlavor]:
    return _turn


def reset() -> None:
    global _turn
    with _lock:
        _turn = None


def note_tool_result(is_error: bool) -> None:
    turn = _turn
    if turn is not None:
        turn.note_tool_result(is_error)


# --- background self-improvement notice ---------------------------------------------

REVIEW_FACES = ("☘️(◕ᴗ◕✿)", "☘️(｀・ω・´)ゞ", "☘️(・ω・)ノ", "☘️(๑•̀ㅂ•́)و✧")
REVIEW_SAVED_LINES = (
    "🍀 saved something new", "🍀 learned something",
    "🍀 noted for next time", "🍀 filed that away",
)
REVIEW_TIDY_LINES = (
    "🧹 tidied up my notes", "🧹 cleaned up a little", "🧹 swept out an old note",
)
_MAX_REMEMBERED_REVIEW_CHATS = 1024
_review_last_pick: dict = {}
_review_lock = threading.Lock()


def _pick_review_header(saved: bool, chat_key: str, rng: Any) -> str:
    """``"<face> <line>"``, never the same pair twice in a row for *chat_key*."""
    lines = REVIEW_SAVED_LINES if saved else REVIEW_TIDY_LINES
    with _review_lock:
        last = _review_last_pick.get(chat_key)
        pair = (rng.choice(REVIEW_FACES), rng.choice(lines))
        for _ in range(20):
            if pair != last:
                break
            pair = (rng.choice(REVIEW_FACES), rng.choice(lines))
        else:  # a stubborn RNG: force a different line
            pair = (pair[0], next(x for x in lines if (pair[0], x) != last))
        if len(_review_last_pick) >= _MAX_REMEMBERED_REVIEW_CHATS and chat_key not in _review_last_pick:
            _review_last_pick.pop(next(iter(_review_last_pick)))
        _review_last_pick[chat_key] = pair
    return f"{pair[0]} {pair[1]}"


def _italic(text: str, markdown: bool = True) -> str:
    body = " ".join((text or "").split()).strip("*").strip()
    return f"*{body}*" if markdown else body


def _review_item_line(group: str, op: str, text: str, markdown: bool) -> str:
    """One Clo-style line for a structured ``(group, op, text)`` review action."""
    it = _italic(text, markdown)
    if group == "skill":
        if op == "create":
            return f"🧬 new skill: {it}" if text else f"🧬 {_italic('new skill', markdown)}"
        if op == "improve":
            return f"🧬 improved: {it}"
        if op == "delete":
            return f"🧹 removed skill: {it}" if text else f"🧹 {_italic('skill removed', markdown)}"
        return f"🧬 {_italic('skill updated', markdown)}"
    if op == "remove":
        if text:
            return f"🧹 {it}"
        return f"🧹 {_italic('profile tidied' if group == 'user' else 'memory tidied', markdown)}"
    if group == "user":
        if op in ("add", "replace") and text:
            return f"🧠 about you: {it}"
        return f"🧠 {_italic('profile updated', markdown)}"
    if op == "replace" and text:
        return f"🧠 updated: {it}"
    if op == "add" and text:
        return f"🧠 {it}"
    return f"🧠 {_italic('memory updated', markdown)}"


def render_review_notice(
    items: Any, chat_key: str = "", rng: Optional[Any] = None
) -> Tuple[list, str]:
    """Clo-style background-review notice: ``(cli_lines, gateway_message)``.

    *items* are ``(group, op, text)`` tuples from
    ``summarize_background_review_actions(structured=...)``.  The header shows
    🍀 when anything was saved and 🧹 when the pass only removed things.  The
    gateway message uses markdown ``*italics*``; CLI lines drop the asterisks.
    """
    items = list(items or [])
    saved = any(op not in ("remove", "delete") for _, op, _ in items)
    header = _pick_review_header(saved, chat_key, rng if rng is not None else random)

    def _lines(markdown: bool) -> list:
        return list(dict.fromkeys(_review_item_line(g, o, t, markdown) for g, o, t in items))

    return [header] + _lines(False), "\n".join([header] + _lines(True))


def reset_review_notices() -> None:
    with _review_lock:
        _review_last_pick.clear()


# --- chat status lines (retry / busy / rate limit / error / model substitute) --------
# Agent-side copy of the pool machinery in gateway/clover_acks.py (agent/ never
# imports gateway code).  Each line is "<face> <line>. <tail>"; the tail keeps
# every functional detail (retry timing, model names, error summary).

STATUS_POOLS = {
    "busy": (
        ("☘️(｡•́︿•̀｡)", "☘️(´･_･`)", "☘️(っ˘ω˘ς)"),
        ("the model's busy", "a bit crowded right now", "the model needs a sec"),
    ),
    "rate_limited": (
        ("☘️(￣ω￣;)", "☘️(´･_･`)"),
        ("hit the usage limit", "slowing down a little"),
    ),
    "error": (
        ("☘️(╥﹏╥)", "☘️(｡•́︿•̀｡)"),
        ("that didn't work", "hit a snag"),
    ),
    "model_substitute": (
        ("☘️(・・ )?", "☘️(°ー°〃)"),
        ("couldn't find *{requested}*", "no *{requested}* on {provider}"),
    ),
}
_MAX_REMEMBERED_STATUS_CHATS = 1024
_status_last_pick: dict = {}
_status_lock = threading.Lock()


def skin_active() -> bool:
    """True when the clover skin is active, the console can show it, and the language is English."""
    try:
        from agent.display import _get_skin
        from agent.i18n import get_language

        return skin_flavor_enabled(_get_skin()) and get_language() == "en"
    except Exception:
        return False


def _pick_status(kind: str, chat_key: str, rng: Any) -> Tuple[str, str]:
    faces, lines = STATUS_POOLS[kind]
    key = f"{kind}:{chat_key}"
    with _status_lock:
        last = _status_last_pick.get(key)
        face, line = rng.choice(faces), rng.choice(lines)
        for _ in range(20):
            if (face, line) != last:
                break
            face, line = rng.choice(faces), rng.choice(lines)
        else:  # a stubborn RNG: force a different line
            line = next(x for x in lines if (face, x) != last)
        if len(_status_last_pick) >= _MAX_REMEMBERED_STATUS_CHATS and key not in _status_last_pick:
            _status_last_pick.pop(next(iter(_status_last_pick)))
        _status_last_pick[key] = (face, line)
    turn = _turn
    return _with_leaf(face, bool(turn is not None and turn.lucky)), line


def status_line(
    kind: str,
    chat_key: str,
    stock: str,
    tail: str = "",
    rng: Optional[Any] = None,
    joiner: str = ". ",
    **fields: Any,
) -> str:
    """Clo-style status ``"<face> <line><joiner><tail>"``; *stock* untouched off the clover skin.

    *fields* fill ``{placeholders}`` in the line (e.g. ``requested``).  The same
    face+line pair is never used twice in a row for one (kind, chat).
    """
    if kind not in STATUS_POOLS or not skin_active():
        return stock
    face, line = _pick_status(kind, str(chat_key or ""), rng if rng is not None else random)
    try:
        line = line.format(**fields)
    except (KeyError, IndexError):
        pass
    return f"{face} {line}{joiner}{tail}" if tail else f"{face} {line}"


def reset_status_lines() -> None:
    with _status_lock:
        _status_last_pick.clear()


# --- cron result header ----------------------------------------------------------------

def cron_header(name: str, failed: bool = False, rng: Optional[Any] = None) -> Optional[str]:
    """Clo-style cron delivery header, or None off the clover skin (keep the stock header).

    ``"☘️ <name>"`` (lucky 1/LUCKY_ODDS: ``"🍀 <name>"``); a failed job is
    ``"🥀 <name> didn't finish"``.
    """
    if not skin_active():
        return None
    if failed:
        return f"🥀 {name} didn't finish"
    rng = rng if rng is not None else random
    lucky = rng.randrange(LUCKY_ODDS) == 0
    return f"{LUCKY_LEAF if lucky else NORMAL_LEAF} {name}"

