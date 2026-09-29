"""Clo's busy / stop acknowledgements (clover skin only).

Each ack is ``"<face> <cute line><status_detail>. <functional sentence>"``.
Face and line are picked independently from the type's pool, and the same
face+line pair is never used twice in a row for one chat.  Other skins (and
non-English sessions) keep the stock text untouched.
"""

from __future__ import annotations

import random
import re
import threading
from typing import Any, Dict, Optional, Tuple

from agent import clover_flavor

ACK_POOLS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "steer": (
        ("☘️(•̀ᴗ•́)و", "☘️(｀・ω・´)ゞ", "☘️(ง •̀_•́)ง", "☘️(๑•̀ㅂ•́)و✧"),
        ("got it, adding that in", "noted, slipping it in",
         "ooh, good call, adding it", "on it, mixing that in"),
    ),
    "redirect": (
        ("☘️(・・ )?", "☘️(°ー°〃)", "☘️(⊙_⊙)ゞ", "☘️( •́ •̀ )"),
        ("oh, changing course", "turning around", "new direction, got it",
         "okay okay, switching paths"),
    ),
    "interrupt": (
        ("☘️(°ロ°)!", "☘️(⊙_⊙)", "☘️(ﾟДﾟ)", "☘️(｡•́︿•̀｡)"),
        ("stopping to listen", "dropping everything", "ears up", "pausing for you"),
    ),
    "queued": (
        ("☘️(っ˘ω˘ς)", "☘️( ˘▽˘)っ", "☘️(◕ᴗ◕✿)", "☘️(ᵔᴥᵔ)"),
        ("saved for next", "tucked in my pocket", "on the list", "right after this one"),
    ),
    "stop": (
        ("☘️(￣▽￣)ゞ", "☘️(・ω・)ノ", "☘️(´• ω •`)ﾉ", "☘️(ᵔᴥᵔ)ノ"),
        ("stopped", "okay, all stopped", "paused right here", "done for now"),
    ),
}

_MAX_REMEMBERED_CHATS = 1024
_last_pick: Dict[str, Tuple[str, str]] = {}
_lock = threading.Lock()


def active() -> bool:
    """True when the clover skin is active, the console can show it, and the language is English."""
    try:
        from agent.display import _get_skin
        from agent.i18n import get_language

        return clover_flavor.skin_flavor_enabled(_get_skin()) and get_language() == "en"
    except Exception:
        return False


def _pick(kind: str, chat_key: str, rng: Any) -> Tuple[str, str]:
    faces, lines = ACK_POOLS[kind]
    with _lock:
        last = _last_pick.get(chat_key)
        face, line = rng.choice(faces), rng.choice(lines)
        for _ in range(20):
            if (face, line) != last:
                break
            face, line = rng.choice(faces), rng.choice(lines)
        else:  # a stubborn RNG: force a different line so we never repeat
            line = next(x for x in lines if (face, x) != last)
        if len(_last_pick) >= _MAX_REMEMBERED_CHATS and chat_key not in _last_pick:
            _last_pick.pop(next(iter(_last_pick)))
        _last_pick[chat_key] = (face, line)
    turn = clover_flavor.current_turn()
    if turn is not None and turn.lucky:
        face = clover_flavor.LUCKY_LEAF + face[len(clover_flavor.NORMAL_LEAF):]
    return face, line


def build_ack(
    kind: str,
    chat_key: str,
    functional: str,
    status_detail: str = "",
    rng: Optional[Any] = None,
) -> str:
    """``"<face> <line><status_detail>. <functional>"`` for *kind* in *chat_key*."""
    face, line = _pick(kind, chat_key, rng if rng is not None else random)
    return f"{face} {line}{status_detail}. {functional}"


def stop_ack(stock: str, chat_key: str, rng: Optional[Any] = None) -> str:
    """Re-skin a stock ``"⚡ Stopped. <functional>"`` reply; unchanged unless clover is active."""
    if not active():
        return stock
    match = re.match(r"^\S+\s+[^.]*\.\s+(?P<rest>.+)$", stock, re.S)
    if not match:
        return stock
    return build_ack("stop", chat_key, match.group("rest"), rng=rng)


def reset() -> None:
    with _lock:
        _last_pick.clear()
