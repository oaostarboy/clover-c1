"""Clo's busy / stop acknowledgements and gateway lifecycle notices (clover skin only).

Each ack is ``"<face> <cute line><status_detail>. <functional sentence>"``.
Face and line are picked independently from the type's pool, and the same
face+line pair is never used twice in a row for one chat.  Other skins (and
non-English sessions) keep the stock text untouched.
"""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
import threading
from typing import Any, Dict, Optional, Tuple

from agent import clover_flavor

# Gateway status families for the chat-status pools the agent renders itself
# (busy / rate_limited / error / model_substitute) live in
# agent/clover_flavor.STATUS_POOLS; the adapters below need only the hello.

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
    # Gateway lifecycle notices (restart / shutdown / back online / ...).
    "restarting": (
        ("☘️(￣▽￣)ゞ", "☘️(・ω・)ノ", "☘️(｀・ω・´)ゞ", "☘️(◕ᴗ◕✿)"),
        ("restarting, be right back", "quick nap, back in a sec",
         "brb, freshening up", "stepping out for a moment"),
    ),
    "shutting_down": (
        ("☘️(´• ω •`)ﾉ", "☘️(・ω・)ノ", "☘️(ᵔᴥᵔ)ノ"),
        ("heading out", "shutting down for now", "signing off"),
    ),
    "restart_requested": (
        ("☘️(•̀ᴗ•́)و", "☘️(◕ᴗ◕✿)", "☘️(｀・ω・´)ゞ"),
        ("restarting now", "okay, one quick restart", "be right back"),
    ),
    "restart_in_progress": (
        ("☘️(っ˘ω˘ς)", "☘️(°ー°〃)"),
        ("already restarting, hang tight", "on it already, one sec"),
    ),
    "draining": (
        ("☘️(っ˘ω˘ς)", "☘️( ˘▽˘)っ"),
        ("wrapping up before I restart", "just finishing up"),
    ),
    "back_online": (
        ("🍀(ﾉ◕ヮ◕)ﾉ*:･ﾟ✧", "🍀(◕ᴗ◕✿)", "🍀(≧◡≦)", "🍀ヾ(＾∇＾)"),
        ("I'm back!", "back and ready", "all fresh, ready when you are",
         "online again"),
    ),
    "job_interrupted": (
        ("☘️(｡•́︿•̀｡)", "☘️(´･_･`)"),
        ("oops", "sorry about that", "bad timing"),
    ),
    "update_rolled_back": (
        ("☘️(´･_･`)", "☘️(｡•́︿•̀｡)", "☘️(・・ )?"),
        ("hmm, that didn't work", "update hiccup", "not this time"),
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


def _pick(kind: str, chat_key: str, rng: Any, always_lucky: bool = False) -> Tuple[str, str]:
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
    lucky = always_lucky or (turn is not None and turn.lucky)
    if lucky and face.startswith(clover_flavor.NORMAL_LEAF):
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


def notice(
    kind: str,
    chat_key: str,
    stock: str,
    tail: str = "",
    rng: Optional[Any] = None,
) -> str:
    """Clo-style gateway notice: ``"<face> <line>. <tail>"`` (no tail: ``"<face> <line>"``).

    Returns *stock* untouched unless the clover skin is active (English only).
    Picks are remembered per (kind, chat) so a chat never sees the same
    face+line pair twice in a row.  ``back_online`` is good news, so it is
    always the lucky 🍀 face.
    """
    if not active() or kind not in ACK_POOLS:
        return stock
    face, line = _pick(kind, f"notice:{kind}:{chat_key}",
                       rng if rng is not None else random,
                       always_lucky=kind == "back_online")
    return f"{face} {line}. {tail}" if tail else f"{face} {line}"


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


# --- first hello to a brand-new chat -------------------------------------------------

HELLO_LINES = (
    "🍀(◕ᴗ◕✿) hi! I'm Clo, your clover. ask me anything.",
    "🍀(ﾉ◕ヮ◕)ﾉ hello! I'm Clo, your clover. what shall we grow today?",
    "🍀(＾▽＾) hey, I'm Clo, your clover. what's on your mind?",
)
# Never greet on machine-facing surfaces.
_NO_HELLO_PLATFORMS = frozenset({
    "local", "api_server", "webhook", "msgraph_webhook", "relay", "homeassistant", "cron",
})
_MAX_HELLO_KEYS = 5000


def _hello_path(home: Any) -> str:
    return os.path.join(str(home), "clo_hello_seen.json")


def _load_hello_seen(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [str(x) for x in data] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def with_first_hello(
    response: str,
    platform: str,
    chat_type: str,
    chat_id: str,
    home: Any,
    rng: Optional[Any] = None,
) -> str:
    """Prepend one Clo hello to the first-ever reply in a DM chat; else *response* untouched.

    Clover skin only, DMs only (never groups/channels), never machine-facing
    platforms.  The chat is remembered in ``clo_hello_seen.json`` so the hello
    is sent once, not on later turns or session resets.
    """
    if not response or not active():
        return response
    if str(chat_type or "dm") != "dm" or str(platform or "").lower() in _NO_HELLO_PLATFORMS:
        return response
    key = f"{platform}:{chat_id}"
    path = _hello_path(home)
    with _lock:
        seen = _load_hello_seen(path)
        if key in seen:
            return response
        seen.append(key)
        try:
            os.makedirs(str(home), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(home), prefix=".clo_hello_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(seen[-_MAX_HELLO_KEYS:], fh)
            os.replace(tmp, path)
        except OSError:
            return response  # can't remember -> don't risk greeting every turn
    hello = (rng if rng is not None else random).choice(HELLO_LINES)
    return f"{hello}\n\n{response}"

