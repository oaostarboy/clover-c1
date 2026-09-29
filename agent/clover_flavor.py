"""Per-turn flavor for the built-in ``clover`` skin ("Clo" the clover sprite).

The skin itself is pure data (faces, verbs, tool emojis).  This module holds
the small amount of *behavior* that needs per-turn state: the once-per-turn
lucky roll, time-of-day / long-task picks, and the bad-luck / luck's-back
status after a failed tool.  Randomness and the clock are injectable so tests
never depend on real time or chance.

It also owns the *message pack* machinery: every chat message Clo words
(busy acks, lifecycle notices, status lines, the review notice, the cron
header, the first hello, the /model + clarify + approval picker strings) is
data in a pack dict.  ``CLOVER_PACK`` is the built-in clover skin's pack; any
other skin can ship its own via a top-level ``messages:`` block (see the
skin_engine docstring).  ``active_pack()`` returns the pack for the active
skin, or None (stock text everywhere).  A kind a pack has no ``lines`` for
always renders the stock text -- one skin's lines never leak into another.
"""

from __future__ import annotations

import random
import re
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


# --- message packs ---------------------------------------------------------------------

# Every kind a pack may re-skin via ``lines`` (ack / lifecycle / status / review).
PACK_KINDS = (
    "steer", "redirect", "interrupt", "queued", "stop",
    "restarting", "shutting_down", "restart_requested", "restart_in_progress",
    "draining", "back_online", "job_interrupted", "update_rolled_back",
    "busy", "rate_limited", "error", "model_substitute",
    "user", "memory", "skill", "mixed", "tidy",
    "new_session",
)
_ACK_KINDS = PACK_KINDS[:13]
_STATUS_KINDS = PACK_KINDS[13:17]
_REVIEW_KINDS = PACK_KINDS[17:22]

_REVIEW_ITEM_DEFAULTS = {
    "about_you": "🪪 about you",
    "note": "🧠",
    "note_updated": "🧠 updated",
    "new_skill": "🧬 new skill",
    "improved": "🧬 improved",
    "removed": "🧹",
}

CLOVER_PACK: dict = {
    "mark": NORMAL_LEAF,
    "lucky_mark": LUCKY_LEAF,
    "done_mark": "🍀",
    "fail_mark": "🥀",
    'faces': {
        'steer': [
            '(•̀ᴗ•́)و',
            '(｀・ω・´)ゞ',
            '(ง •̀_•́)ง',
            '(๑•̀ㅂ•́)و✧',
        ],
        'redirect': [
            '(・・ )?',
            '(°ー°〃)',
            '(⊙_⊙)ゞ',
            '( •́ •̀ )',
        ],
        'interrupt': [
            '(°ロ°)!',
            '(⊙_⊙)',
            '(ﾟДﾟ)',
            '(｡•́︿•̀｡)',
        ],
        'queued': [
            '(っ˘ω˘ς)',
            '( ˘▽˘)っ',
            '(◕ᴗ◕✿)',
            '(ᵔᴥᵔ)',
        ],
        'stop': [
            '(￣▽￣)ゞ',
            '(・ω・)ノ',
            '(´• ω •`)ﾉ',
            '(ᵔᴥᵔ)ノ',
        ],
        'restarting': [
            '(￣▽￣)ゞ',
            '(・ω・)ノ',
            '(｀・ω・´)ゞ',
            '(◕ᴗ◕✿)',
        ],
        'shutting_down': [
            '(´• ω •`)ﾉ',
            '(・ω・)ノ',
            '(ᵔᴥᵔ)ノ',
        ],
        'restart_requested': [
            '(•̀ᴗ•́)و',
            '(◕ᴗ◕✿)',
            '(｀・ω・´)ゞ',
        ],
        'restart_in_progress': [
            '(っ˘ω˘ς)',
            '(°ー°〃)',
        ],
        'draining': [
            '(っ˘ω˘ς)',
            '( ˘▽˘)っ',
        ],
        'back_online': [
            '(ﾉ◕ヮ◕)ﾉ*:･ﾟ✧',
            '(◕ᴗ◕✿)',
            '(≧◡≦)',
            'ヾ(＾∇＾)',
        ],
        'job_interrupted': [
            '(｡•́︿•̀｡)',
            '(´･_･`)',
        ],
        'update_rolled_back': [
            '(´･_･`)',
            '(｡•́︿•̀｡)',
            '(・・ )?',
        ],
        'busy': [
            '(｡•́︿•̀｡)',
            '(´･_･`)',
            '(っ˘ω˘ς)',
        ],
        'rate_limited': [
            '(￣ω￣;)',
            '(´･_･`)',
        ],
        'error': [
            '(╥﹏╥)',
            '(｡•́︿•̀｡)',
        ],
        'model_substitute': [
            '(・・ )?',
            '(°ー°〃)',
        ],
        'user': [
            '(◍•ᴗ•◍)',
            '(˶ᵔ ᵕ ᵔ˶)',
            '(◕ᴗ◕✿)',
        ],
        'memory': [
            '(｀・ω・´)ゞ',
            '(・ω・)ノ',
            '(•̀ᴗ•́)و',
        ],
        'skill': [
            '(๑•̀ㅂ•́)و✧',
            '(ง •̀_•́)ง',
            '(ﾉ◕ヮ◕)ﾉ',
        ],
        'mixed': [
            '(◕ᴗ◕✿)',
            '(๑•̀ㅂ•́)و✧',
            '(｀・ω・´)ゞ',
        ],
        'tidy': [
            '(・ω・)ノ',
            '( ˘▽˘)っ',
            '(￣▽￣)ゞ',
        ],
        'new_session': [
            '(◕ᴗ◕✿)',
            '(｡•̀ᴗ-)✧',
            '(ﾉ◕ヮ◕)ﾉ',
            '(•ᴗ•)ﾉ',
            '(´• ω •`)ﾉ',
        ],
    },
    'lines': {
        'steer': [
            'got it, adding that in',
            'noted, slipping it in',
            'ooh, good call, adding it',
            'on it, mixing that in',
        ],
        'redirect': [
            'oh, changing course',
            'turning around',
            'new direction, got it',
            'okay okay, switching paths',
        ],
        'interrupt': [
            'stopping to listen',
            'dropping everything',
            'ears up',
            'pausing for you',
        ],
        'queued': [
            'saved for next',
            'tucked in my pocket',
            'on the list',
            'right after this one',
        ],
        'stop': [
            'stopped',
            'okay, all stopped',
            'paused right here',
            'done for now',
        ],
        'restarting': [
            'restarting, be right back',
            'quick nap, back in a sec',
            'brb, freshening up',
            'stepping out for a moment',
        ],
        'shutting_down': [
            'heading out',
            'shutting down for now',
            'signing off',
        ],
        'restart_requested': [
            'restarting now',
            'okay, one quick restart',
            'be right back',
        ],
        'restart_in_progress': [
            'already restarting, hang tight',
            'on it already, one sec',
        ],
        'draining': [
            'wrapping up before I restart',
            'just finishing up',
        ],
        'back_online': [
            "I'm back!",
            'back and ready',
            'all fresh, ready when you are',
            'online again',
        ],
        'job_interrupted': [
            'oops',
            'sorry about that',
            'bad timing',
        ],
        'update_rolled_back': [
            "hmm, that didn't work",
            'update hiccup',
            'not this time',
        ],
        'busy': [
            "the model's busy",
            'a bit crowded right now',
            'the model needs a sec',
        ],
        'rate_limited': [
            'hit the usage limit',
            'slowing down a little',
        ],
        'error': [
            "that didn't work",
            'hit a snag',
        ],
        'model_substitute': [
            "couldn't find *{requested}*",
            'no *{requested}* on {provider}',
        ],
        'user': [
            'got to know you a little better',
            'noted something about you',
            "I'll remember that about you",
        ],
        'memory': [
            'noted for next time',
            'filed that away',
            'jotted that down',
        ],
        'skill': [
            'learned a new trick',
            'got a little better at something',
            'sharpened a skill',
        ],
        'mixed': [
            'learned a few things',
            'picked up a few things',
            'a good little lesson',
        ],
        'tidy': [
            'tidied up my notes',
            'cleaned up a little',
            'swept out an old note',
        ],
        'new_session': [
            "fresh patch of clover. what's next?",
            'new sprout, clean slate',
            'fresh start! ask me anything',
            'a clean patch, ready to grow',
            'all tidied up. what shall we plant?',
            'turning over a new leaf',
            "slate's clean. what's on your mind?",
            'fresh dew, fresh start',
        ],
    },
    "review_items": dict(_REVIEW_ITEM_DEFAULTS),
    "hello": [
        "🍀(◕ᴗ◕✿) hi! I'm Clo, your clover. ask me anything.",
        "🍀(ﾉ◕ヮ◕)ﾉ hello! I'm Clo, your clover. what shall we grow today?",
        "🍀(＾▽＾) hey, I'm Clo, your clover. what's on your mind?",
    ],
    "ui": {
        "model_title": "🍀 *Pick a model*",
        "using": "Using",
        "choose_provider": "Choose a provider",
        "choose_model": "Choose a model",
        "switched": "🍀 switched!",
        "switch_failed": "🥀 switch failed",
        "expired": "☘️ this menu expired, send /model again",
        "clarify_mark": "🌼",
        "other": "✏️ something else",
        "type_it": "✏️ type it in the chat",
        "waiting_for": "waiting for {user} to type…",
        "new_titled": "new patch: *{title}*",
        "new_model_icon": "🤖",
        "new_context_icon": "📏",
        "new_local_icon": "🏠",
        "new_tip": "🍀 tip:",
        "chosen_mark": "🍀",
        "allow_mark": "🍀",
        "deny_mark": "🥀",
    },
}


def _str_list(value: Any) -> list:
    if isinstance(value, (list, tuple)):
        return [x for x in value if isinstance(x, str)]
    return []


def _str_map(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items() if isinstance(v, str)}


def normalize_pack(raw: Any) -> Optional[dict]:
    """A skin's raw ``messages:`` mapping as a pack dict (None when empty/invalid)."""
    if not isinstance(raw, dict) or not raw:
        return None

    def _mark(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    faces_raw = raw.get("faces") if isinstance(raw.get("faces"), dict) else {}
    lines_raw = raw.get("lines") if isinstance(raw.get("lines"), dict) else {}
    lines = {str(k): _str_list(v) for k, v in lines_raw.items()}
    return {
        "mark": _mark("mark"),
        "lucky_mark": _mark("lucky_mark"),
        "done_mark": _mark("done_mark"),
        "fail_mark": _mark("fail_mark"),
        "faces": {str(k): _str_list(v) for k, v in faces_raw.items()},
        "lines": {k: v for k, v in lines.items() if v},
        "review_items": {**_REVIEW_ITEM_DEFAULTS, **_str_map(raw.get("review_items"))},
        "hello": _str_list(raw.get("hello")),
        "ui": _str_map(raw.get("ui")),
    }


def pack_for_skin(skin: Any) -> Optional[dict]:
    """The message pack *skin* ships: its own ``messages`` block, else Clo's for the clover skin."""
    own = normalize_pack(getattr(skin, "messages", None))
    if own is not None:
        return own
    spinner = getattr(skin, "spinner", None) or {}
    if spinner.get("flavor") == "clover":
        return CLOVER_PACK
    return None


def active_pack() -> Optional[dict]:
    """The active skin's message pack, or None (stock text).

    Same guards as the clover flavor always had: the console must be able to
    show the emoji and the session language must be English.
    """
    try:
        from agent.display import _get_skin
        from agent.i18n import get_language

        if get_language() != "en" or not emoji_safe():
            return None
        return pack_for_skin(_get_skin())
    except Exception:
        return None


def has_lines(pack: Optional[dict], kind: str) -> bool:
    return bool(pack and pack["lines"].get(kind))


def _join(*parts: str) -> str:
    return " ".join(p for p in parts if p)


def ui(key: str, stock: Optional[str] = None) -> Optional[str]:
    """The active pack's picker string for *key*; *stock* when unset / no pack."""
    pack = active_pack()
    if pack is not None:
        value = pack["ui"].get(key)
        if isinstance(value, str):
            return value
    return stock


def pick_pair(
    pack: dict,
    kind: str,
    store: dict,
    lock: Any,
    key: str,
    rng: Any,
    *,
    lucky: bool = False,
    mark: Optional[str] = None,
    max_keep: int = 1024,
) -> Tuple[str, str]:
    """``(face, line)`` for *kind*; never the same pair twice in a row for *key*.

    Face and line are picked independently.  The face is *mark* (default: the
    pack's mark, or its lucky mark on a lucky turn) plus a face body; a kind
    with no faces is just the mark.
    """
    faces = pack["faces"].get(kind) or [""]
    lines = pack["lines"][kind]
    with lock:
        last = store.get(key)
        body, line = rng.choice(faces), rng.choice(lines)
        for _ in range(20):
            if (body, line) != last:
                break
            body, line = rng.choice(faces), rng.choice(lines)
        else:  # a stubborn RNG: force a different line (a one-line pack just repeats)
            line = next((x for x in lines if (body, x) != last), line)
        if len(store) >= max_keep and key not in store:
            store.pop(next(iter(store)))
        store[key] = (body, line)
    if mark is None:
        mark = pack["lucky_mark"] if lucky and pack["lucky_mark"] else pack["mark"]
    return mark + body, line


def clover_pool(pack: dict, kind: str, mark: Optional[str] = None, line_prefix: str = "") -> tuple:
    """Legacy ``(faces, lines)`` pool view of *kind* (faces carry the mark)."""
    mark = pack["mark"] if mark is None else mark
    return (
        tuple(mark + body for body in pack["faces"][kind]),
        tuple(_join(line_prefix, line) for line in pack["lines"][kind]),
    )


def fill_line(line: str, fields: dict) -> str:
    try:
        return line.format(**fields)
    except (KeyError, IndexError, ValueError):
        return line


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

# Header pools by what the pass did, so "learned about you", "saved a note",
# "learned a trick" and "tidied up" each read differently at a glance.
# Back-compat views of Clo's pack (older callers/tests import these).
REVIEW_POOLS = {
    kind: clover_pool(
        CLOVER_PACK, kind,
        line_prefix=_REVIEW_ITEM_DEFAULTS["removed"] if kind == "tidy" else CLOVER_PACK["done_mark"],
    )
    for kind in _REVIEW_KINDS
}
REVIEW_FACES = tuple(dict.fromkeys(f for faces, _ in REVIEW_POOLS.values() for f in faces))
REVIEW_SAVED_LINES = REVIEW_POOLS["memory"][1]
REVIEW_TIDY_LINES = REVIEW_POOLS["tidy"][1]
_MAX_REMEMBERED_REVIEW_CHATS = 1024
_review_last_pick: dict = {}
_review_lock = threading.Lock()


def _review_kind(items: list) -> str:
    saved = {g for g, op, _ in items if op not in ("remove", "delete")}
    if not saved:
        return "tidy"
    if len(saved) > 1:
        return "mixed"
    return next(iter(saved)) if next(iter(saved)) in _REVIEW_KINDS else "memory"


def _icon(pack: dict, key: str) -> str:
    """Leading icon of a ``review_items`` value (``"🧬 new skill"`` -> ``"🧬"``)."""
    return pack["review_items"].get(key, "").partition(" ")[0]


def _label(pack: dict, key: str) -> str:
    return pack["review_items"].get(key, "").partition(" ")[2]


def _pick_review_header(kind, chat_key: str, rng: Any, pack: Optional[dict] = None) -> Optional[str]:
    """``"<face> <line>"``, never the same pair twice in a row for *chat_key*.

    None when *pack* has no lines for *kind* (the caller keeps the stock notice).
    """
    pack = pack or CLOVER_PACK
    if isinstance(kind, bool):  # legacy signature: saved flag
        kind = "memory" if kind else "tidy"
    if not has_lines(pack, kind):
        return None
    face, line = pick_pair(
        pack, kind, _review_last_pick, _review_lock, chat_key, rng,
        mark=pack["mark"], max_keep=_MAX_REMEMBERED_REVIEW_CHATS,
    )
    lead = _icon(pack, "removed") if kind == "tidy" else pack["done_mark"]
    return _join(face, lead, line)


def _italic(text: str, markdown: bool = True) -> str:
    body = " ".join((text or "").split()).strip("*").strip()
    return f"*{body}*" if markdown else body


def _review_item_line(group: str, op: str, text: str, markdown: bool, pack: Optional[dict] = None) -> str:
    """One line for a structured ``(group, op, text)`` review action (Clo's icons by default)."""
    pack = pack or CLOVER_PACK
    items = pack["review_items"]
    it = _italic(text, markdown)
    if group == "skill":
        if op == "create":
            if text:
                return f"{items['new_skill']}: {it}"
            return _join(_icon(pack, "new_skill"), _italic(_label(pack, "new_skill") or "new skill", markdown))
        if op == "improve":
            return f"{items['improved']}: {it}"
        if op == "delete":
            if text:
                return _join(items["removed"], f"removed skill: {it}")
            return _join(items["removed"], _italic("skill removed", markdown))
        return _join(_icon(pack, "improved"), _italic("skill updated", markdown))
    if op == "remove":
        if text:
            return _join(items["removed"], it)
        return _join(items["removed"], _italic("an old note about you" if group == "user" else "an old note", markdown))
    if group == "user":
        if op in ("add", "replace") and text:
            return f"{items['about_you']}: {it}"
        return _join(_icon(pack, "about_you"), _italic("something about you", markdown))
    if op == "replace" and text:
        return f"{items['note_updated']}: {it}"
    if op == "add" and text:
        return _join(items["note"], it)
    return _join(items["note"], _italic("a note for later", markdown))


def render_review_notice(
    items: Any, chat_key: str = "", rng: Optional[Any] = None, pack: Optional[dict] = None
) -> Optional[Tuple[list, str]]:
    """Message-pack background-review notice: ``(cli_lines, gateway_message)``.

    *items* are ``(group, op, text)`` tuples from
    ``summarize_background_review_actions(structured=...)``.  The header is
    picked by what changed (about you / a note / a skill / a mix) or by the
    removed icon when the pass only removed things.  The gateway message uses
    markdown ``*italics*``; CLI lines drop the asterisks.  *pack* defaults to
    Clo's; None comes back when the pack has no header lines for this kind of
    change (keep the stock notice).
    """
    pack = pack or CLOVER_PACK
    items = list(items or [])
    header = _pick_review_header(_review_kind(items), chat_key, rng if rng is not None else random, pack)
    if header is None:
        return None

    def _lines(markdown: bool) -> list:
        return list(dict.fromkeys(_review_item_line(g, o, t, markdown, pack) for g, o, t in items))

    # Chat surfaces get a quote bar (like the turn card) so the notice can't be
    # mistaken for a real reply; the item text stays italic.
    return [header] + _lines(False), "\n".join("> " + ln for ln in [header] + _lines(True))


def reset_review_notices() -> None:
    with _review_lock:
        _review_last_pick.clear()


# --- chat status lines (retry / busy / rate limit / error / model substitute) --------
# Agent-side copy of the pool machinery in gateway/clover_acks.py (agent/ never
# imports gateway code).  Each line is "<face> <line>. <tail>"; the tail keeps
# every functional detail (retry timing, model names, error summary).

STATUS_POOLS = {kind: clover_pool(CLOVER_PACK, kind) for kind in _STATUS_KINDS}
_MAX_REMEMBERED_STATUS_CHATS = 1024
_status_last_pick: dict = {}
_status_lock = threading.Lock()


def skin_active() -> bool:
    """True when the active skin ships a message pack the console can show (English only)."""
    return active_pack() is not None


def status_line(
    kind: str,
    chat_key: str,
    stock: str,
    tail: str = "",
    rng: Optional[Any] = None,
    joiner: str = ". ",
    **fields: Any,
) -> str:
    """Pack-styled status ``"<face> <line><joiner><tail>"``; *stock* untouched without a pack line.

    *fields* fill ``{placeholders}`` in the line (e.g. ``requested``).  The same
    face+line pair is never used twice in a row for one (kind, chat).
    """
    pack = active_pack()
    if not has_lines(pack, kind):
        return stock
    turn = _turn
    face, line = pick_pair(
        pack, kind, _status_last_pick, _status_lock, f"{kind}:{chat_key or ''}",
        rng if rng is not None else random,
        lucky=bool(turn is not None and turn.lucky),
        max_keep=_MAX_REMEMBERED_STATUS_CHATS,
    )
    head = _join(face, fill_line(line, fields))
    return f"{head}{joiner}{tail}" if tail else head


def reset_status_lines() -> None:
    with _status_lock:
        _status_last_pick.clear()


# --- cron result header ----------------------------------------------------------------

def cron_header(name: str, failed: bool = False, rng: Optional[Any] = None) -> Optional[str]:
    """Message-pack cron delivery header, or None without a pack (keep the stock header).

    Clo: ``"☘️ <name>"`` (lucky 1/LUCKY_ODDS: ``"🍀 <name>"``); a failed job is
    ``"🥀 <name> didn't finish"``.  Packs without a ``lucky_mark`` never roll.
    """
    pack = active_pack()
    if pack is None:
        return None
    if failed:
        return _join(pack["fail_mark"], f"{name} didn't finish")
    mark = pack["mark"]
    if pack["lucky_mark"]:
        rng = rng if rng is not None else random
        if rng.randrange(LUCKY_ODDS) == 0:
            mark = pack["lucky_mark"]
    return _join(mark, name)


# --- /new and /reset reply ------------------------------------------------------------
# "<face> <line>" headline, a quote-bar info block (model / context / local
# endpoint) and an italic tip.  Pretty names, not raw ids.

_MAX_REMEMBERED_NEW_CHATS = 1024
_new_last_pick: dict = {}
_new_lock = threading.Lock()

# Provider ids -> the short name people say out loud (provider_label() is the
# picker's long form, e.g. "xAI Grok OAuth (SuperGrok / Premium+)").
_SHORT_PROVIDERS = {
    "xai": "xAI", "xai-oauth": "xAI", "grok": "xAI",
    "openai": "OpenAI", "openai-codex": "OpenAI", "codex": "OpenAI",
    "anthropic": "Anthropic", "claude": "Anthropic", "claude-code": "Anthropic",
    "openrouter": "OpenRouter", "nous": "Nous", "gemini": "Google",
    "google": "Google", "deepseek": "DeepSeek", "custom": "custom",
    "ollama": "custom", "lmstudio": "custom", "vllm": "custom",
}
_FAMILY_NAMES = {
    "grok": "Grok", "gemini": "Gemini", "gemma": "Gemma", "deepseek": "DeepSeek",
    "kimi": "Kimi", "glm": "GLM", "llama": "Llama", "mistral": "Mistral",
    "mixtral": "Mixtral", "qwen": "Qwen",
}
_MODEL_ID = re.compile(r"^[A-Za-z0-9.]+(?:-[A-Za-z0-9.]+)*$")


def pretty_provider_name(provider: Any) -> str:
    """``xai-oauth`` -> ``xAI``; unknown ids fall back to the picker label without its parenthetical."""
    raw = str(provider or "").strip()
    if not raw:
        return ""
    short = _SHORT_PROVIDERS.get(raw.lower())
    if short:
        return short
    try:
        from clover_cli.models import provider_label

        label = provider_label(raw)
    except Exception:
        label = raw
    return re.sub(r"\s*\(.*?\)\s*", " ", label).strip() or raw


def pretty_model_name(model: Any) -> str:
    """``grok-4.7`` -> ``Grok 4.7``, ``claude-opus-5-5`` -> ``Opus 5.5``; ids we don't know stay as they are."""
    raw = str(model or "").strip()
    if not raw:
        return ""
    try:
        from agent.delegation_activity import pretty_model

        nice = pretty_model(raw)
    except Exception:
        nice = raw
    if nice != raw.rsplit("/", 1)[-1] or not _MODEL_ID.match(nice):
        return nice or raw
    head, *rest = nice.split("-")
    family = _FAMILY_NAMES.get(head.lower())
    if family is None:
        return nice
    words = [w.upper() if re.fullmatch(r"v\d[\d.]*", w) else (w if w[:1].isdigit() else w.capitalize())
             for w in rest]
    return " ".join([family, *words])


def _one_line(text: Any) -> str:
    """Single line with no ``*`` (a stray asterisk would break the surrounding italics)."""
    return " ".join(str(text or "").replace("*", "").split())


def _ui_or(pack: dict, key: str, default: str) -> str:
    value = pack["ui"].get(key)
    return value if isinstance(value, str) else default


def new_session_active() -> bool:
    """True when the active skin's pack has ``new_session`` lines (else /new keeps the stock reply)."""
    return has_lines(active_pack(), "new_session")


def render_new_session(
    *,
    chat_key: str = "",
    title: str = "",
    topic_header: str = "",
    model: str = "",
    provider: str = "",
    context: str = "",
    context_guess: bool = False,
    local_endpoint: str = "",
    tip: str = "",
    rng: Optional[Any] = None,
    pack: Optional[dict] = None,
) -> Optional[Tuple[str, str, str]]:
    """``(headline, info_block, tip_line)`` for the pack's /new reply; None = keep the stock reply.

    *headline*: ``"<mark><face> <line>"`` (Telegram topic lanes keep their own
    header text via *topic_header*; a titled session reads ``"<face> new patch:
    *<title>*"``).  *info_block* is a quote bar (``> `` lines) with the model,
    context size and, for local setups, the endpoint; empty when nothing is
    known.  *tip_line* is the italic ``"<mark> tip: *<tip>*"`` (empty without a
    tip).  Face and line are never the same pair twice in a row for a chat; a
    pack with a lucky mark swaps it in 1 turn in LUCKY_ODDS.
    """
    pack = pack or active_pack()
    if not has_lines(pack, "new_session"):
        return None
    rng = rng if rng is not None else random
    lucky = bool(pack["lucky_mark"]) and rng.randrange(LUCKY_ODDS) == 0
    face, line = pick_pair(
        pack, "new_session", _new_last_pick, _new_lock, f"new:{chat_key}", rng,
        lucky=lucky, max_keep=_MAX_REMEMBERED_NEW_CHATS,
    )
    if topic_header:
        headline = topic_header
    elif title:
        body = _italic(_one_line(title))
        headline = _join(face, _ui_or(pack, "new_titled", "new session: *{title}*").replace("*{title}*", body).replace("{title}", body))
    else:
        headline = _join(face, line)

    m_icon = _ui_or(pack, "new_model_icon", "🤖")
    c_icon = _ui_or(pack, "new_context_icon", "📏")
    l_icon = _ui_or(pack, "new_local_icon", "🏠")
    rows = []
    who = pretty_model_name(model)
    prov = pretty_provider_name(provider)
    if who:
        rows.append(_join(m_icon, f"{who} · {prov}" if prov else who))
    if context:
        hint = " *(default guess — set model.context_length to change)*" if context_guess else ""
        rows.append(_join(c_icon, f"{context} context{hint}"))
    if local_endpoint:
        rows.append(_join(l_icon, f"local: {local_endpoint}"))
    info = "\n".join("> " + r for r in rows)

    tip_text = _one_line(tip)
    tip_line = f"{_ui_or(pack, 'new_tip', (pack['done_mark'] + ' tip:').strip())} {_italic(tip_text)}" if tip_text else ""
    return headline, info, tip_line


def reset_new_session_picks() -> None:
    with _new_lock:
        _new_last_pick.clear()
