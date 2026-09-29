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
