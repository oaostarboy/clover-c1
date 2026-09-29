"""Busy / stop acknowledgements and gateway lifecycle notices from the active skin's message pack.

Each ack is ``"<face> <cute line><status_detail>. <functional sentence>"``.
Face and line are picked independently from the type's pool, and the same
face+line pair is never used twice in a row for one chat.  Clo's pack is the
built-in clover skin's (``clover_flavor.CLOVER_PACK``); a user skin can ship
its own via ``messages:``.  A skin with no pack, a kind the pack has no lines
for, and non-English sessions keep the stock text untouched.
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
    kind: clover_flavor.clover_pool(
        clover_flavor.CLOVER_PACK, kind,
        mark=clover_flavor.CLOVER_PACK["done_mark"] if kind == "back_online" else None,
    )
    for kind in clover_flavor._ACK_KINDS
}

_MAX_REMEMBERED_CHATS = 1024
_last_pick: Dict[str, Tuple[str, str]] = {}
_lock = threading.Lock()


def active(kind: Optional[str] = None) -> bool:
    """True when the active skin ships a message pack (and, with *kind*, lines for it).

    The console must be able to show the emoji and the language must be English.
    """
    pack = clover_flavor.active_pack()
    if pack is None:
        return False
    return clover_flavor.has_lines(pack, kind) if kind else True


def ui(key: str, stock: Optional[str] = None) -> Optional[str]:
    """The active pack's picker string for *key* (``stock`` off-pack / when unset)."""
    return clover_flavor.ui(key, stock)


def _pick(kind: str, chat_key: str, rng: Any, always_lucky: bool = False,
          pack: Optional[dict] = None) -> Tuple[str, str]:
    pack = pack or clover_flavor.CLOVER_PACK
    turn = clover_flavor.current_turn()
    lucky = turn is not None and turn.lucky
    # back_online is good news: always the pack's done mark.
    mark = pack["done_mark"] if always_lucky else None
    return clover_flavor.pick_pair(
        pack, kind, _last_pick, _lock, chat_key, rng,
        lucky=lucky, mark=mark, max_keep=_MAX_REMEMBERED_CHATS,
    )


def build_ack(
    kind: str,
    chat_key: str,
    functional: str,
    status_detail: str = "",
    rng: Optional[Any] = None,
) -> str:
    """``"<face> <line><status_detail>. <functional>"`` for *kind* in *chat_key*."""
    pack = clover_flavor.active_pack() or clover_flavor.CLOVER_PACK
    face, line = _pick(kind, chat_key, rng if rng is not None else random, pack=pack)
    return f"{clover_flavor._join(face, line)}{status_detail}. {functional}"


def notice(
    kind: str,
    chat_key: str,
    stock: str,
    tail: str = "",
    rng: Optional[Any] = None,
) -> str:
    """Message-pack gateway notice: ``"<face> <line>. <tail>"`` (no tail: ``"<face> <line>"``).

    Returns *stock* untouched unless the active skin's pack has lines for
    *kind* (English only).  Picks are remembered per (kind, chat) so a chat
    never sees the same face+line pair twice in a row.  ``back_online`` is
    good news, so it always wears the pack's done mark (Clo: the lucky 🍀).
    """
    if not active():
        return stock
    pack = clover_flavor.active_pack() or clover_flavor.CLOVER_PACK
    if not clover_flavor.has_lines(pack, kind):
        return stock
    face, line = _pick(kind, f"notice:{kind}:{chat_key}",
                       rng if rng is not None else random,
                       always_lucky=kind == "back_online", pack=pack)
    head = clover_flavor._join(face, line)
    return f"{head}. {tail}" if tail else head


def stop_ack(stock: str, chat_key: str, rng: Optional[Any] = None) -> str:
    """Re-skin a stock ``"⚡ Stopped. <functional>"`` reply; unchanged without a pack ``stop`` line."""
    if not active() or not clover_flavor.has_lines(
        clover_flavor.active_pack() or clover_flavor.CLOVER_PACK, "stop"
    ):
        return stock
    match = re.match(r"^\S+\s+[^.]*\.\s+(?P<rest>.+)$", stock, re.S)
    if not match:
        return stock
    return build_ack("stop", chat_key, match.group("rest"), rng=rng)


def reset() -> None:
    with _lock:
        _last_pick.clear()


# --- first hello to a brand-new chat -------------------------------------------------

HELLO_LINES = tuple(clover_flavor.CLOVER_PACK["hello"])  # back-compat alias
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
    """Prepend one pack hello to the first-ever reply in a DM chat; else *response* untouched.

    Only a pack with ``hello`` lines greets (Clo's does), DMs only (never groups/channels), never machine-facing
    platforms.  The chat is remembered in ``clo_hello_seen.json`` so the hello
    is sent once, not on later turns or session resets.
    """
    if not response or not active():
        return response
    hellos = (clover_flavor.active_pack() or clover_flavor.CLOVER_PACK).get("hello")
    if not hellos:
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
    hello = (rng if rng is not None else random).choice(hellos)
    return f"{hello}\n\n{response}"

