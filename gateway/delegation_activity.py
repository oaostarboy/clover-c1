"""Live delegation activity cards for gateway chats.

One editable card per delegation group (one ``delegate_task`` call, or the
external agent jobs registered in one turn), fed by the ``subagent.*`` events
the turn's ``TurnRunner.progress_callback`` already receives and by external
job observers bound to this turn's activity sink. Findings and alerts (worker
done / failed / cancelled / stalled) are sent once as short messages. Delivery reuses the adapter's edit-in-place transport
(``send_or_update_status`` — Telegram, Slack) or send+edit where only
``edit_message`` exists.

Delivery rules (each exists because the alternative is wrong for a chat):

* **Coalesced.** Events only mark a card dirty; a single flush renders the
  latest state. Bursts of tool calls become one edit, never a backlog.
* **Throttled + flood-aware.** At most one edit per ``min_interval`` per card;
  a server ``retry_after`` (Telegram flood control) is always honoured.
* **Never blocks or kills work.** ``observe`` runs on the child's thread and
  only mutates in-memory state; delivery happens on the gateway loop and every
  failure is swallowed. A dead transport degrades the view, never the worker.
* **Scoped to the turn that spawned it.** The publisher is created per turn
  with that turn's adapter, chat and thread metadata; events reach it only
  through that turn's agent callback, so nothing crosses chats or profiles.
  A turn that is no longer current may still *finish* a card it already
  posted (accurate final state) but never opens a new one.
* **Low-noise heartbeat.** Quiet groups are re-rendered at most once per
  ``heartbeat_seconds`` so elapsed time and waiting/stalled classification
  stay truthful; heartbeats edit, they never post new messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import itertools
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from agent.delegation_activity import DelegationActivityTracker, LivenessProbe

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_SECONDS = 60
DEFAULT_STALL_SECONDS = 600
DEFAULT_MIN_INTERVAL = 4.0
# Hard cap on how long a finished summary waits for a busy parent (the live
# card already shows the result in place while it waits).
DEFAULT_MAX_FINAL_DEFER = 1800.0
# Combined turn card: a result finished during the turn waits for the turn's
# card to absorb it; this caps the wait if the turn never produces a card.
COMBINED_MAX_FINAL_DEFER = 120.0
# A background worker that finishes AFTER its turn ended waits this long in
# the chat's inbox for the next turn (usually the completion wake-up) to fold
# it into that turn's card. Unclaimed, it posts on its own.
INBOX_GRACE_SECONDS = 90.0

# (adapter id, chat id) -> finished groups waiting for the next turn's card.
_INBOX: Dict[Tuple[int, str], List[Tuple["DelegationActivityPublisher", str]]] = {}
_INBOX_LOCK = threading.Lock()


def _inbox_key(adapter: Any, chat_id: Any) -> Tuple[int, str]:
    return (id(adapter), str(chat_id))


# (adapter id, chat id) -> publishers that may still own a LIVE card there.
# After the parent's reply lands, those cards are re-posted below it so the
# running roster always follows the latest message instead of scrolling away.
_LIVE: Dict[Tuple[int, str], List["DelegationActivityPublisher"]] = {}
_LIVE_LOCK = threading.Lock()
_LIVE_MAX_PER_CHAT = 32
# Newest outbound message id per chat (any message the bot sent), and the
# pending debounced follow per chat.
_LAST_OUT: Dict[Tuple[int, str], str] = {}
# Newest outbound message per chat that is NOT a helper (ack) card: the only
# kind of message a running unified card treats as "something newer".
_LAST_PLAIN: Dict[Tuple[int, str], str] = {}
_FOLLOW_TIMERS: Dict[Tuple[int, str], Any] = {}
FOLLOW_DEBOUNCE_SECONDS = 1.5
# True while a publisher itself is sending/editing cards, so its own posts
# never count as "a newer message" and trigger another move.
_CARD_SEND: "contextvars.ContextVar[bool]" = contextvars.ContextVar("_deleg_card_send", default=False)


def _is_newer(a: Optional[str], b: Optional[str]) -> bool:
    """True when message id ``a`` is newer than ``b`` (numeric ids only;
    unknown ordering counts as newer so the card still follows)."""
    if not a:
        return False
    if not b:
        return True
    try:
        return int(a) > int(b)
    except (TypeError, ValueError):
        return a != b


def _is_ack_card_text(content: Any) -> bool:
    try:
        from agent.ack_checklist import match_unbound

        return content is not None and match_unbound(content) is not None
    except Exception:
        return False


def _follow_key(adapter: Any, chat_id: Any, metadata: Any = None) -> Tuple[Any, ...]:
    # Native Telegram topics are separate conversations, not one shared board.
    key = _inbox_key(adapter, chat_id)
    thread = str(metadata.get("thread_id") or "") if isinstance(metadata, dict) else ""
    return (*key, thread) if thread and _adapter_is_telegram(adapter) and _unified_style() else key


def note_outbound(adapter: Any, chat_id: Any, message_id: Any = None, content: Any = None, metadata: Any = None) -> None:
    """Record that the bot just posted a message in this chat. When running
    subagent cards live there, move them below it after a short quiet window
    (coalesces streaming bursts into one move). Never raises.

    ``content`` (the text sent) lets a helper (ack) card's own first send be
    told apart: it never counts as "something newer" for another card."""
    try:
        if _CARD_SEND.get() or adapter is None or chat_id in (None, ""):
            return
        key = _follow_key(adapter, chat_id, metadata)
        if message_id:
            prev = _LAST_OUT.get(key)
            if prev is None or _is_newer(str(message_id), prev):
                _LAST_OUT[key] = str(message_id)
            if not _is_ack_card_text(content):
                prev = _LAST_PLAIN.get(key)
                if prev is None or _is_newer(str(message_id), prev):
                    _LAST_PLAIN[key] = str(message_id)
        with _LIVE_LOCK:
            pubs = [p for p in _LIVE.get(key, []) if not p._closed]
        if not pubs:
            return
        loop = pubs[-1]._loop
        def _arm() -> None:
            old = _FOLLOW_TIMERS.pop(key, None)
            if old is not None:
                old.cancel()
            _FOLLOW_TIMERS[key] = loop.call_later(
                FOLLOW_DEBOUNCE_SECONDS,
                lambda: (_FOLLOW_TIMERS.pop(key, None), loop.create_task(follow_latest_message(adapter, chat_id, metadata))),
            )
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _arm()
        else:
            loop.call_soon_threadsafe(_arm)
    except Exception:
        logger.debug("note_outbound failed", exc_info=True)


def _adapter_can_delete(adapter: Any) -> bool:
    deleter = getattr(type(adapter), "delete_message", None)
    if deleter is None:
        return False
    try:
        from gateway.platforms.base import BasePlatformAdapter

        return deleter is not BasePlatformAdapter.delete_message
    except Exception:
        return True


async def follow_latest_message(adapter: Any, chat_id: Any, metadata: Any = None) -> int:
    """Move every live (still running) delegation card in this chat below the
    message just delivered. Returns how many cards were moved.

    Only on adapters that can delete: without delete, re-posting would leave
    duplicate cards behind, which is worse than a card that stays put.
    """
    if adapter is None or chat_id in (None, "") or not _adapter_can_delete(adapter):
        return 0
    key = _follow_key(adapter, chat_id, metadata)
    with _LIVE_LOCK:
        pubs = [p for p in _LIVE.get(key, []) if not p._closed]
        if pubs:
            _LIVE[key] = pubs
        else:
            _LIVE.pop(key, None)
    moved = 0
    board = _BOARDS.get(key)
    if board is not None:
        try:
            moved += int(await board.refresh(move=True))
        except Exception:
            logger.debug("delegation board follow failed", exc_info=True)
    # A copy: an idle publisher drops itself from ``_LIVE[key]`` (this same
    # list) inside its repost, which would skip the card right after it -- a
    # running card then stayed ABOVE the opener line it should follow.
    for pub in list(pubs):
        try:
            moved += await pub.repost_live_cards()
        except Exception:
            logger.debug("delegation card follow failed", exc_info=True)
    return moved


def inbox_pending(adapter: Any, chat_id: Any) -> int:
    with _INBOX_LOCK:
        return len(_INBOX.get(_inbox_key(adapter, chat_id), ()))
_SUMMARY_POLL_SECONDS = 2.0
_MAX_ALERTS_PER_GROUP = 12
# Stop heartbeat refreshes for a group that has produced no event for this
# long (a worker lost without any observable signal). Observation continues.
_HEARTBEAT_MAX_QUIET = 6 * 3600
_MAX_ERROR_BACKOFF = 60.0


def resolve_delegation_activity(user_config: Any, platform_key: str) -> Tuple[bool, int]:
    """Return ``(enabled, heartbeat_seconds)`` for a platform.

    ``display.delegation_activity`` is ``auto`` by default: on wherever the
    platform shows tool progress in chat, off where the user (or the platform
    default) chose silence — so existing opt-outs are preserved on upgrade.
    ``on``/``off`` override that per platform or globally.
    """
    from gateway.display_config import resolve_display_setting

    cfg = user_config if isinstance(user_config, dict) else {}
    mode = resolve_display_setting(cfg, platform_key, "delegation_activity", "auto")
    if mode == "auto":
        progress = resolve_display_setting(cfg, platform_key, "tool_progress", "all")
        enabled = progress not in {"off", "log"}
    else:
        enabled = mode == "on"
    heartbeat = resolve_display_setting(
        cfg, platform_key, "delegation_heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS
    )
    if not isinstance(heartbeat, int) or heartbeat < 0:
        heartbeat = DEFAULT_HEARTBEAT_SECONDS
    return enabled, heartbeat


@dataclass
class _CardState:
    posted: bool = False
    suppressed: bool = False
    dirty: bool = False
    last_text: str = ""
    last_attempt: float = float("-inf")
    last_publish: float = float("-inf")
    backoff_until: float = float("-inf")
    flood_backoff: bool = False
    failures: int = 0
    message_id: Optional[str] = None
    alerts_sent: int = 0
    # Final summary: posted once as a NEW message at the bottom of the chat
    # (then the live card is removed), after the parent's own reply lands.
    summary_posted: bool = False
    summary_released: bool = False
    summary_waiting: bool = False
    summary_deadline: float = float("inf")
    # Combined mode: finished after its turn; parked for the next turn card.
    in_inbox: bool = False
    inbox_expired: bool = False
    # The unified helper card took this delegation over after a 🔀 card was
    # already (being) posted: that card is removed, nothing more is posted.
    retired: bool = False


# ── One live board per chat ───────────────────────────────────────────────
# Combined mode (Telegram default): instead of one live card per delegation
# group (two turns that each launched workers = two cards at two scroll
# positions), every running worker in the chat shows on ONE board message.
# The board follows the latest message; a finished worker leaves the board
# once its result is shown (turn card / summary), and the board is deleted
# when nothing is left on it.
_BOARDS: Dict[Tuple[int, str], "_ChatBoard"] = {}
BOARD_MIN_EDIT_SECONDS = 3.0


# Board message ids survive a gateway restart on disk, so a board left behind
# by the previous process is swept by the next board in that chat (otherwise
# a restart mid-run leaves an orphan board no one will ever delete).
_BOARD_STORE_LOCK = threading.Lock()


def _board_store_path():
    try:
        from clover_constants import get_clover_home

        return get_clover_home() / "state" / "delegation_boards.json"
    except Exception:
        return None


def _board_store_key(adapter: Any, chat_id: Any) -> str:
    name = getattr(adapter, "name", None) or type(adapter).__name__
    return f"{name}:{chat_id}"


def _board_store_load() -> Dict[str, List[str]]:
    path = _board_store_path()
    if path is None or not path.exists():
        return {}
    try:
        import json as _json

        data = _json.loads(path.read_text(encoding="utf-8"))
        return {str(k): [str(i) for i in v] for k, v in data.items() if isinstance(v, list)}
    except Exception:
        return {}


def _board_store_set(key: str, ids: List[str]) -> None:
    path = _board_store_path()
    if path is None:
        return
    with _BOARD_STORE_LOCK:
        try:
            data = _board_store_load()
            if ids:
                data[key] = list(ids)[-20:]
            else:
                data.pop(key, None)
            from utils import atomic_json_write

            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json_write(path, data)
        except Exception:
            logger.debug("delegation board store write failed", exc_info=True)


class _BoardGroup:
    """Duck-typed stand-in for DelegationGroup so _render_active can draw
    workers from several groups/turns as one card."""

    def __init__(self, children: List[Any], created_at: float) -> None:
        self._children = children
        self.created_at = created_at
        self.children = {str(i): c for i, c in enumerate(children)}

    def ordered(self) -> List[Any]:
        return list(self._children)


class _ChatBoard:
    def __init__(self, adapter: Any, chat_id: str, metadata: Any, loop: Any,
                 expandable: bool, clock: Callable[[], float]) -> None:
        self.adapter = adapter
        self.chat_id = chat_id
        self.metadata = metadata
        self.loop = loop
        self.expandable = expandable
        self.clock = clock
        self.message_id: Optional[str] = None
        self.last_text = ""
        self.last_edit = float("-inf")
        self._lock: Optional[asyncio.Lock] = None
        self._timer: Any = None
        # Every board message ever posted and not yet confirmed deleted. A
        # new post sweeps all older ones, so a failed edit/move can never
        # leave a second board behind.
        self._posted: List[str] = []
        self._store_key = _board_store_key(adapter, chat_id)
        # Boards left behind by a previous gateway process: swept on first post.
        try:
            self._posted.extend(_board_store_load().get(self._store_key, []))
        except Exception:
            pass

    @property
    def key(self) -> Tuple[int, str]:
        return _inbox_key(self.adapter, self.chat_id)

    async def _sweep_except(self, keep: Optional[str]) -> None:
        stale = [m for m in self._posted if m != keep]
        self._posted = [keep] if keep else []
        _board_store_set(self._store_key, self._posted)
        for mid in stale:
            await self._delete(mid)

    def _publishers(self) -> List["DelegationActivityPublisher"]:
        with _LIVE_LOCK:
            return [p for p in _LIVE.get(self.key, []) if not p._closed and p._combined]

    def children(self) -> Tuple[List[Any], float]:
        """Every worker still owned by the board, oldest turn first."""
        kids: List[Any] = []
        t0 = float("inf")
        for pub in self._publishers():
            with pub._state_lock:
                gids = [
                    gid for gid in pub.tracker.group_ids()
                    if not pub._cards.get(gid, _CardState()).summary_posted
                    and not pub._cards.get(gid, _CardState()).suppressed
                    and pub._cards.get(gid, _CardState()).posted
                ]
            if not gids:
                continue
            with pub.tracker._lock:
                for gid in gids:
                    grp = pub.tracker._groups.get(gid)
                    if grp is None:
                        continue
                    t0 = min(t0, grp.created_at)
                    kids.extend(grp.ordered())
        return kids, (t0 if t0 != float("inf") else self.clock())

    def render(self) -> str:
        kids, t0 = self.children()
        if not kids:
            return ""
        from agent.delegation_activity import _render_active

        try:
            stamp = time.strftime("%H:%M %Z", time.localtime()).strip()
        except Exception:
            stamp = ""
        text = _render_active(_BoardGroup(kids, t0), self.clock(), stamp)
        limit = 4096
        try:
            limit = int(getattr(self.adapter, "MAX_MESSAGE_LENGTH", 4096) or 4096)
        except Exception:
            pass
        text = _fit_to_limit(text, limit - (8 if self.expandable else 0))
        if self.expandable and text:
            text = _to_expandable(text, TAP_HINT_LIVE)
        return text

    def schedule(self, delay: float = 0.0) -> None:
        def _go() -> None:
            self._timer = None
            self.loop.create_task(self.refresh())
        try:
            if self._timer is not None:
                return
            self._timer = self.loop.call_later(max(0.0, delay), _go)
        except RuntimeError:
            pass

    async def refresh(self, *, move: bool = False) -> bool:
        """Post, edit, move or delete the board. Returns True if it moved."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        token = _CARD_SEND.set(True)
        try:
            async with self._lock:
                text = self.render()
                if not text:
                    await self._delete()
                    await self._sweep_except(None)
                    _BOARDS.pop(self.key, None)
                    return False
                last_out = _LAST_OUT.get(self.key)
                behind = bool(self.message_id and last_out and _is_newer(last_out, self.message_id))
                if self.message_id is None or (move and behind):
                    old = self.message_id
                    res = await _maybe_await(self.adapter.send(self.chat_id, text, metadata=self.metadata))
                    if getattr(res, "success", False) and getattr(res, "message_id", None):
                        self.message_id = str(res.message_id)
                        self.last_text = text
                        self.last_edit = self.clock()
                        self._posted.append(self.message_id)
                        await self._sweep_except(self.message_id)
                        return bool(old)
                    return False
                if text == self.last_text:
                    return False
                wait = self.last_edit + BOARD_MIN_EDIT_SECONDS - self.clock()
                if wait > 0:
                    self.schedule(wait)
                    return False
                editor = getattr(self.adapter, "edit_message", None)
                if callable(editor):
                    res = await _maybe_await(editor(
                        self.chat_id, self.message_id, text, finalize=True, metadata=self.metadata,
                    ))
                    if getattr(res, "success", False):
                        self.last_text = text
                        self.last_edit = self.clock()
                    elif _message_gone(res):
                        self.message_id = None
                        self.schedule(0.0)
                return False
        except Exception:
            logger.debug("delegation board refresh failed", exc_info=True)
            return False
        finally:
            _CARD_SEND.reset(token)

    async def _delete(self, message_id: Optional[str] = None) -> None:
        mid = message_id or self.message_id
        if not mid:
            return
        if message_id is None:
            self.message_id = None
            self.last_text = ""
        try:
            if _adapter_can_delete(self.adapter):
                await _maybe_await(self.adapter.delete_message(self.chat_id, mid))
        except Exception:
            logger.debug("delegation board delete failed", exc_info=True)


def _board_for(pub: "DelegationActivityPublisher") -> "_ChatBoard":
    key = _inbox_key(pub._adapter, pub._chat_id)
    board = _BOARDS.get(key)
    if board is None:
        board = _ChatBoard(pub._adapter, pub._chat_id, pub._metadata, pub._loop,
                           pub._expandable, pub._clock)
        _BOARDS[key] = board
    return board


# Only live work opens a card; a late "complete" must not revive a finished one.
_CARD_OPENING_EVENTS = frozenset({
    "subagent.start", "subagent.tool", "subagent.tool_done", "subagent.progress",
    "subagent.thinking", "subagent.text",
})


class DelegationActivityPublisher:
    """Per-turn publisher of delegation roster cards to one chat/thread."""

    def __init__(
        self,
        *,
        adapter: Any,
        chat_id: Any,
        loop: asyncio.AbstractEventLoop,
        metadata: Optional[Dict[str, Any]] = None,
        is_current: Optional[Callable[[], bool]] = None,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        liveness_probe: Optional[LivenessProbe] = None,
        clock: Callable[[], float] = time.monotonic,
        auto_heartbeat: bool = True,
        is_parent_busy: Optional[Callable[[], bool]] = None,
        defer_final: Optional[Callable[[Callable[[], None]], bool]] = None,
        max_final_defer: float = DEFAULT_MAX_FINAL_DEFER,
        expandable: bool = False,
        combined: bool = False,
    ) -> None:
        self._adapter = adapter
        self._chat_id = str(chat_id)
        self._metadata = metadata
        self._loop = loop
        self._is_current = is_current or (lambda: True)
        self._heartbeat_seconds = max(0.0, float(heartbeat_seconds or 0))
        self._min_interval = max(0.0, float(min_interval or 0))
        self._probe = liveness_probe
        self._clock = clock
        self._auto_heartbeat = auto_heartbeat
        # Telegram: render cards as a collapsible quote, like the main
        # agent's turn card (header visible, details behind a tap).
        self._expandable = bool(expandable)
        # When the parent turn is still running, the final summary waits for
        # its reply (so tool bubbles, the reply and the summary never
        # interleave). ``defer_final(release)`` registers ``release`` to run
        # after the parent's reply is delivered; returns False if it cannot.
        # Combined mode (display.turn_card: combined): results that finish
        # while the turn runs are folded into the turn's own summary card,
        # so a turn is one card + one reply. Until the turn ends they are
        # held (shown in place on the live card).
        self._combined = bool(combined)
        # One board per chat instead of one card per group (needs delete so
        # the board can move and vanish cleanly).
        self._board_mode = self._combined and _adapter_can_delete(adapter)
        self._turn_over = False
        # Results from EARLIER turns in this chat, claimed at this turn's
        # start so they fold into this turn's card.
        self._adopted: List[Tuple["DelegationActivityPublisher", str]] = []
        if self._combined:
            is_parent_busy = lambda: not self._turn_over  # noqa: E731
            max_final_defer = min(max_final_defer, COMBINED_MAX_FINAL_DEFER)
        self._is_parent_busy = is_parent_busy or (lambda: False)
        self._defer_final = defer_final
        self._max_final_defer = max(0.0, float(max_final_defer))
        self.tracker = DelegationActivityTracker(
            clock=clock,
            heartbeat_seconds=self._heartbeat_seconds,
            stall_seconds=stall_seconds,
        )
        self._state_lock = threading.Lock()
        self._cards: Dict[str, _CardState] = {}
        self._pending_alerts: List[Tuple[str, str]] = []
        self._flush_lock: Optional[asyncio.Lock] = None
        self._wake: Optional[asyncio.Event] = None
        self._pump_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._closed = False
        self._jobs_group: Optional[str] = None
        self._jobs_count = 0
        self._place_lock: Optional[asyncio.Lock] = None
        # Fallback group for relays that carry no delegation_id (e.g. live
        # transcript creation failed). Per publisher, so two turns in one chat
        # never share a status key like "delegation:delegation".
        self._fallback_group = f"deleg_{uuid.uuid4().hex[:8]}"
        # Join the follow registry only when observe() receives activity via
        # _ensure_live(). Empty per-turn publishers own no card; in combined
        # mode their repost is a no-op, so eagerly registering them would fill
        # the registry and evict a still-running unified card across turns.

    def external_job_identity(self) -> Tuple[str, int]:
        """Group id + index for an external agent job registered this turn.

        Unique per publisher so a later turn's jobs never share (and edit)
        this turn's card on adapters that key status messages by name.
        """
        with self._state_lock:
            if self._jobs_group is None:
                self._jobs_group = f"jobs_{uuid.uuid4().hex[:8]}"
            index = self._jobs_count
            self._jobs_count += 1
            return self._jobs_group, index

    def open_external_job_card(self, card_id: str, title: str, lifecycle: bool = False) -> str:
        """Unified card style: show an outside agent job on the subagent card.

        Returns ``card_id`` (the job's delegation id from now on) when the job
        has a part on the unified card, else ``""`` (classic style: the job
        keeps its 🔀 roster card).  The card joins the chat's running unified
        card, or is posted as its own message.  Any thread.
        """
        try:
            from agent.ack_checklist import open_agent_job_part

            card = open_agent_job_part(card_id, title, lifecycle)
        except Exception:
            return ""
        if card is None:
            return ""
        return card_id if self.place_card(card, card_id) else ""

    def place_card(self, card: Any, card_id: str, reuse_message_id: str = "") -> bool:
        """Place a freshly opened one-part unified card in this chat: it joins
        the chat's running card or is posted as its own message.  Any thread.
        ``reuse_message_id``: the message a restarted job's card was in; it is
        edited into the new card when it still exists.
        False when the loop is closed (nothing placed, the part is dropped)."""
        card.target = (id(self._adapter), self._chat_id, "pending")  # claimed: nothing else binds it
        try:
            asyncio.run_coroutine_threadsafe(
                self._place_job_card(card, card_id, reuse_message_id), self._loop,
            )
        except RuntimeError:
            from agent.ack_checklist import abandon

            abandon(card_id)  # loop closed: nothing to show
            return False
        return True

    def open_dispatch_card(
        self, delegation_id: str, goals: Any, titles: Any = (), steps: Any = (), child_steps: Any = (),
        work: str = "",
    ) -> bool:
        """Unified style: a delegate_task dispatch without a ``handoff`` summary
        still gets its card (never the 🔀 roster).  Any thread."""
        try:
            from agent.ack_checklist import open_dispatch_card

            card = open_dispatch_card(delegation_id, goals, titles, steps, child_steps, work)
        except Exception:
            return False
        if card is None:
            return False
        reuse = ""
        try:
            from tools.delegation_inflight import current_resume_card

            old_card = current_resume_card() or {}
            if str(old_card.get("chat_id") or "") == self._chat_id:
                reuse = str(old_card.get("message_id") or "")
        except Exception:
            reuse = ""
        return self.place_card(card, str(delegation_id), reuse)

    async def _reuse_card_message(self, card: Any, message_id: str) -> bool:
        """Turn the message a restarted job's card was in into this card.
        False when the message is gone (the caller posts a new one)."""
        editor = _attach_editor(
            card, self._adapter, self._chat_id, str(message_id), self._loop, self._metadata,
        )
        ok = False
        try:
            ok = await editor._edit(editor._render())
        except Exception:
            logger.debug("reusing the old card message failed", exc_info=True)
        if ok:
            return True
        editor.gone = True
        editor._unregister_live()
        _ACK_EDITORS.pop(id(card), None)
        card.on_change = None
        card.target = (id(self._adapter), self._chat_id, "pending")
        return False

    async def _place_job_card(self, card: Any, card_id: str, reuse_message_id: str = "") -> None:
        from agent.ack_checklist import abandon, join

        if self._place_lock is None:
            self._place_lock = asyncio.Lock()
        try:
            async with self._place_lock:  # one job at a time: the next one finds this card
                if reuse_message_id and await self._reuse_card_message(card, reuse_message_id):
                    return
                host = _running_editor_for(self._adapter, self._chat_id, card, self._metadata)
                if host is not None and join(host.card, card):
                    _note_card_location(host.card, self._adapter, self._chat_id, host._message_id, self._metadata)
                    host.notify()
                    return
                await _send_card_under_opener(card, self._adapter, self._chat_id, self._metadata)
        except Exception:
            logger.debug("agent job card placement failed", exc_info=True)
            card.target = None
        if card.target is None:
            abandon(card_id)

    # -- producer side (any thread) --------------------------------------

    def observe(
        self,
        event_type: Any,
        tool_name: Any = None,
        preview: Any = None,
        args: Any = None,
        **kwargs: Any,
    ) -> None:
        """Fold one relayed child event. Never raises, never blocks on I/O."""
        if self._closed:
            return
        if not kwargs.get("delegation_id"):
            kwargs["delegation_id"] = self._fallback_group
        try:
            from agent.ack_checklist import owns_delegation, unified

            if not owns_delegation(kwargs.get("delegation_id")):
                # Unified style: no helper ever gets the 🔀 roster card.  A
                # helper nobody opened a card for (a nested one, a dispatch
                # path that skipped it) gets a plain titled card now.
                self._open_card_from_event(event_type, preview, kwargs)
            if owns_delegation(kwargs.get("delegation_id")) or unified():
                # The unified helper card (the ack message) shows this
                # helper's activity and result: no separate 🔀 card.  If an
                # earlier event already started one (ownership was registered
                # late), take it back down.
                self._retire_owned(str(kwargs.get("delegation_id")))
                return
        except Exception:
            pass
        self._ensure_live()
        try:
            group_id, alerts = self.tracker.observe(
                event_type, tool_name, preview, args, **kwargs
            )
        except Exception:
            logger.debug("delegation activity observe failed", exc_info=True)
            return
        if group_id is None and not alerts:
            return
        gid = group_id or str(kwargs.get("delegation_id") or "delegation")
        with self._state_lock:
            card = self._cards.setdefault(gid, _CardState())
            if group_id is not None:
                card.dirty = True
                if not card.flood_backoff:
                    # Fresh content earns a fresh attempt after a plain
                    # transport error; server flood windows still hold.
                    card.backoff_until = float("-inf")
            self._pending_alerts.extend((gid, text) for text in alerts)
        self._schedule()

    def _open_card_from_event(self, event_type: Any, preview: Any, kwargs: Dict[str, Any]) -> None:
        """Open the unified card for the delegation of a relayed child event."""
        try:
            from agent.ack_checklist import unified

            if not unified() or str(event_type or "") not in _CARD_OPENING_EVENTS:
                return
            did = str(kwargs.get("delegation_id") or "")
            if not did:
                return
            count = max(1, int(kwargs.get("task_count") or 1))
            title = str(kwargs.get("title") or "").strip()
            goal = str(kwargs.get("goal") or "").strip() or (
                str(preview or "").strip() if event_type == "subagent.start" else ""
            ) or title or "background task"
            self.open_dispatch_card(did, [goal] * count, [title] if count == 1 else ())
        except Exception:
            logger.debug("could not open a card from a child event", exc_info=True)

    def _retire_owned(self, gid: str) -> None:
        """The unified card owns ``gid`` but a 🔀 card for it may already be up
        (or in flight): remove it and make sure nothing more is posted, no
        summary either.  Idempotent; any thread."""
        with self._state_lock:
            card = self._cards.get(gid)
            known = card is not None or gid in self.tracker.group_ids()
            if not known:
                return
            if card is None:
                card = self._cards.setdefault(gid, _CardState())
            if card.retired:
                return
            card.retired = card.suppressed = card.summary_posted = True
            card.dirty = False
            mid = card.message_id
            self._pending_alerts = [a for a in self._pending_alerts if a[0] != gid]
        self.tracker.drop_group(gid)
        self._remove_later(gid, mid)

    def _remove_later(self, gid: str, message_id: Optional[str]) -> None:
        async def _go() -> None:
            token = _CARD_SEND.set(True)
            try:
                await self._remove_live_card(gid, message_id)
                if self._board_mode:
                    _board_for(self).schedule(0.0)
            finally:
                _CARD_SEND.reset(token)

        try:
            asyncio.run_coroutine_threadsafe(_go(), self._loop)
        except RuntimeError:
            pass  # loop closed (gateway shutting down)

    def _schedule(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._ensure_tasks)
        except RuntimeError:
            pass  # loop closed (gateway shutting down): nothing to deliver to

    # -- consumer side (gateway loop) ------------------------------------

    def _ensure_tasks(self) -> None:
        if self._closed:
            return
        if self._flush_lock is None:
            self._flush_lock = asyncio.Lock()
            self._wake = asyncio.Event()
        self._wake.set()
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = self._loop.create_task(self._pump())
        if (
            self._auto_heartbeat
            and self._heartbeat_seconds > 0
            and (self._heartbeat_task is None or self._heartbeat_task.done())
            and self.tracker.active_group_ids()
        ):
            self._heartbeat_task = self._loop.create_task(self._heartbeat_loop())

    async def _pump(self) -> None:
        while not self._closed:
            self._wake.clear()
            await self.flush()
            delay = self._next_gate_delay()
            if delay is None:
                return
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.05, min(delay, 5.0)))
            except asyncio.TimeoutError:
                pass

    def _gate(self, gid: str, card: _CardState) -> float:
        """Earliest time this card may be sent. A finished group skips the
        edit-rate gate (it is sent once) so its result lands before the
        parent reacts to the same completion; server backoff still holds."""
        if self.tracker.group_finished(gid):
            return card.backoff_until
        return max(card.last_attempt + self._min_interval, card.backoff_until)

    def _next_gate_delay(self) -> Optional[float]:
        now = self._clock()
        with self._state_lock:
            gates = [
                self._gate(gid, c) - now
                for gid, c in self._cards.items()
                if c.dirty and not c.suppressed
            ]
        return max(0.0, min(gates)) if gates else None

    async def _heartbeat_loop(self) -> None:
        interval = max(1.0, min(self._heartbeat_seconds, 15.0))
        while not self._closed and self.tracker.active_group_ids():
            await asyncio.sleep(interval)
            await self.heartbeat_tick()

    async def heartbeat_tick(self) -> None:
        """One heartbeat pass: re-classify quiet children, refresh stale cards."""
        try:
            changed, alerts = self.tracker.tick(self._probe)
        except Exception:
            logger.debug("delegation activity tick failed", exc_info=True)
            return
        now = self._clock()
        with self._state_lock:
            for gid in self.tracker.group_ids():
                card = self._cards.setdefault(gid, _CardState())
                if gid in changed:
                    card.dirty = True
                    continue
                if self.tracker.group_finished(gid) or not self._heartbeat_seconds:
                    continue
                quiet_for = now - max(card.last_publish, card.last_attempt)
                event_age = self.tracker.seconds_since_last_event(gid) or 0.0
                if quiet_for >= self._heartbeat_seconds and event_age < _HEARTBEAT_MAX_QUIET:
                    card.dirty = True
            self._pending_alerts.extend(alerts)
        await self.flush()

    async def flush(self) -> None:
        """Deliver every dirty card whose gate is open, then pending alerts."""
        if self._flush_lock is None:
            self._flush_lock = asyncio.Lock()
            self._wake = asyncio.Event()
        token = _CARD_SEND.set(True)
        try:
            await self._flush_locked()
        finally:
            _CARD_SEND.reset(token)

    async def _flush_locked(self) -> None:
        async with self._flush_lock:
            now = self._clock()
            with self._state_lock:
                due = [
                    gid
                    for gid, card in self._cards.items()
                    if card.dirty
                    and not card.suppressed
                    and now >= self._gate(gid, card)
                ]
            for gid in due:
                try:
                    await self._deliver_card(gid)
                except Exception:
                    # A render/bookkeeping bug must not kill the pump task
                    # or the other cards; drop this pass for this card.
                    logger.debug("delegation card flush failed", exc_info=True)
                    with self._state_lock:
                        self._cards[gid].dirty = False
            try:
                await self._deliver_alerts()
            except Exception:
                logger.debug("delegation alert flush failed", exc_info=True)

    async def drain(self) -> None:
        """Let scheduled callbacks run, then complete one flush pass (tests/shutdown)."""
        for _ in range(3):
            await asyncio.sleep(0)
        await self.flush()

    async def aclose(self) -> None:
        self._closed = True
        self._unregister_live()
        for task in (self._pump_task, self._heartbeat_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    def _ensure_live(self) -> None:
        """(Re)join the chat's follow registry. A publisher is dropped while
        idle (e.g. a message went out before its first worker started), so
        activity must put it back or its card never follows."""
        if self._closed:
            return
        key = _inbox_key(self._adapter, self._chat_id)
        with _LIVE_LOCK:
            pubs = _LIVE.setdefault(key, [])
            if self not in pubs:
                pubs.append(self)
                if len(pubs) > _LIVE_MAX_PER_CHAT:
                    del pubs[: len(pubs) - _LIVE_MAX_PER_CHAT]

    def _unregister_live(self) -> None:
        key = _inbox_key(self._adapter, self._chat_id)
        with _LIVE_LOCK:
            pubs = _LIVE.get(key)
            if pubs and self in pubs:
                pubs.remove(self)
                if not pubs:
                    _LIVE.pop(key, None)

    async def repost_live_cards(self) -> int:
        """Delete each posted, still-running card and post it again at the
        bottom of the chat. Finished cards are left alone (their summary
        path already posts at the bottom)."""
        if self._closed or self._board_mode:
            return 0
        if self._flush_lock is None:
            self._flush_lock = asyncio.Lock()
            self._wake = asyncio.Event()
        moved = 0
        token = _CARD_SEND.set(True)
        try:
            return await self._repost_locked()
        finally:
            _CARD_SEND.reset(token)

    async def _repost_locked(self) -> int:
        moved = 0
        async with self._flush_lock:
            with self._state_lock:
                targets = [
                    (gid, card) for gid, card in self._cards.items()
                    if card.posted and not card.suppressed and not card.summary_posted
                    and not self.tracker.group_finished(gid)
                ]
            if not targets and not self.tracker.active_group_ids():
                self._unregister_live()
                return 0
            last_out = _LAST_OUT.get(_inbox_key(self._adapter, self._chat_id))
            for gid, card in targets:
                old_id = card.message_id
                if old_id and last_out and not _is_newer(last_out, str(old_id)):
                    continue  # already the newest message: nothing to follow
                status_ids = getattr(self._adapter, "_status_message_ids", None)
                if isinstance(status_ids, dict):
                    key_id = status_ids.pop((self._chat_id, f"delegation:{gid}"), None)
                    old_id = old_id or (str(key_id) if key_id else None)
                text = self._render(gid)
                if not text:
                    continue
                try:
                    result = await _maybe_await(
                        self._adapter.send(self._chat_id, text, metadata=self._metadata)
                    )
                except Exception:
                    logger.debug("delegation card repost failed", exc_info=True)
                    continue
                new_id = getattr(result, "message_id", None)
                if not (getattr(result, "success", False) and new_id):
                    continue
                with self._state_lock:
                    card.message_id = str(new_id)
                    card.last_text = text
                    card.last_publish = self._clock()
                if isinstance(status_ids, dict):
                    status_ids[(self._chat_id, f"delegation:{gid}")] = str(new_id)
                if old_id and str(old_id) != str(new_id):
                    try:
                        await _maybe_await(self._adapter.delete_message(self._chat_id, old_id))
                    except Exception:
                        logger.debug("old delegation card delete failed", exc_info=True)
                moved += 1
        return moved

    # -- combined turn card ------------------------------------------------

    @property
    def combined(self) -> bool:
        return self._combined

    def absorb_finished(self) -> Tuple[List[Any], List[str]]:
        """Hand every finished, not-yet-summarised group to the turn card.

        Returns ``(workers, live_card_message_ids)``. The groups are marked
        summarised so no separate summary message is ever posted for them;
        the caller deletes the returned live cards.
        """
        adopted_children: List[Any] = []
        ids: List[str] = []
        adopted, self._adopted = self._adopted, []
        for pub, agid in adopted:
            kids, kid_ids = pub._absorb_groups([agid])
            adopted_children.extend(kids)
            ids.extend(kid_ids)
        own, own_ids = self._absorb_groups(None)
        if self._board_mode:
            try:
                self._loop.call_soon_threadsafe(lambda: _board_for(self).schedule(0.0))
            except RuntimeError:
                pass
        return adopted_children + own, ids + own_ids

    def _absorb_groups(self, only: Optional[List[str]]) -> Tuple[List[Any], List[str]]:
        gids: List[str] = []
        ids: List[str] = []
        status_ids = getattr(self._adapter, "_status_message_ids", None)
        with self._state_lock:
            for gid in (only if only is not None else self.tracker.group_ids()):
                if not self.tracker.group_finished(gid):
                    continue
                card = self._cards.setdefault(gid, _CardState())
                if card.summary_posted:
                    continue
                card.summary_posted = True
                card.dirty = False
                gids.append(gid)
                mid = card.message_id
                if isinstance(status_ids, dict):
                    key_id = status_ids.pop((self._chat_id, f"delegation:{gid}"), None)
                    mid = mid or (str(key_id) if key_id else None)
                if mid:
                    ids.append(str(mid))
        return self.tracker.children_for(gids), ids

    def _park_in_inbox(self, gid: str, card: _CardState) -> None:
        """Finished after this turn ended: wait for the next turn's card."""
        with self._state_lock:
            card.in_inbox = True
        with _INBOX_LOCK:
            _INBOX.setdefault(_inbox_key(self._adapter, self._chat_id), []).append((self, gid))
        try:
            self._loop.call_later(INBOX_GRACE_SECONDS, self._expire_inbox, gid)
        except RuntimeError:
            self._expire_inbox(gid)

    def _expire_inbox(self, gid: str) -> None:
        """Nobody claimed it in time (or the claiming turn failed): post it
        on its own, as before."""
        key = _inbox_key(self._adapter, self._chat_id)
        with _INBOX_LOCK:
            entries = _INBOX.get(key, [])
            if (self, gid) in entries:
                entries.remove((self, gid))
                if not entries:
                    _INBOX.pop(key, None)
        with self._state_lock:
            card = self._cards.get(gid)
            if card is None or card.summary_posted:
                return
            card.inbox_expired = True
            card.dirty = True
        self._schedule()

    def adopt_inbox(self) -> int:
        """At turn start: claim results that finished after earlier turns in
        this chat, so this turn's card shows them. Returns how many groups."""
        if not self._combined:
            return 0
        with _INBOX_LOCK:
            claimed = _INBOX.pop(_inbox_key(self._adapter, self._chat_id), [])
        self._adopted.extend(claimed)
        return len(claimed)

    def adopted_preview(self) -> Tuple[int, List[str]]:
        """``(worker count, rows)`` of adopted results, for the carrier card."""
        from agent.delegation_activity import turn_card_body

        children: List[Any] = []
        for pub, gid in self._adopted:
            children.extend(pub.tracker.children_for([gid]))
        return len(children), turn_card_body(children, self._clock())

    def end_turn(self) -> None:
        """The parent turn is over: anything still held posts on its own."""
        if self._turn_over:
            return
        self._turn_over = True
        # Adopted results this turn never showed (failed turn, no card):
        # post them on their own now rather than lose them.
        leftovers, self._adopted = self._adopted, []
        for pub, gid in leftovers:
            pub._expire_inbox(gid)
        with self._state_lock:
            waiting = [g for g, c in self._cards.items()
                       if c.summary_waiting and not c.summary_posted]
            for g in waiting:
                # Finished DURING this turn but no card took it (a turn with
                # no progress bubbles): post now, don't wait for a next turn.
                self._cards[g].inbox_expired = True
        for gid in waiting:
            self._release_summary(gid)

    # -- delivery ----------------------------------------------------------

    def _current(self) -> bool:
        try:
            return bool(self._is_current())
        except Exception:
            return False

    async def _deliver_card(self, gid: str) -> None:
        card = self._cards[gid]
        if self.tracker.group_finished(gid):
            await self._deliver_summary(gid, card)
            return
        text = self._render(gid)
        with self._state_lock:
            if not text or text == card.last_text:
                card.dirty = False
                return
            if not card.posted and not self._current():
                # The turn moved on before this group was ever shown: do not
                # open a fresh card (or its alerts) in a conversation that has
                # left it behind.
                card.suppressed = True
                card.dirty = False
                return
            card.dirty = False
            card.last_attempt = self._clock()
        final = self.tracker.group_finished(gid)
        if self._board_mode:
            with self._state_lock:
                card.posted = True
                card.last_text = text
                card.last_publish = self._clock()
            await _board_for(self).refresh()
            return
        try:
            result = await self._send_card(gid, card, text, final)
        except Exception as exc:
            self._record_failure(card, None, f"{type(exc).__name__}: {exc}")
            return
        if card.retired:
            # The unified card took this group over while the post was in
            # flight: take the card that just landed back down.
            await self._remove_live_card(gid, card.message_id)
            return
        if result is not None and getattr(result, "success", False):
            with self._state_lock:
                card.posted = True
                card.last_text = text
                card.last_publish = self._clock()
                card.failures = 0
                card.flood_backoff = False
                card.backoff_until = float("-inf")
        else:
            self._record_failure(card, result, getattr(result, "error", "send failed"))

    def _record_failure(self, card: _CardState, result: Any, error: Any) -> None:
        now = self._clock()
        with self._state_lock:
            card.dirty = True  # latest state still owed; coalesced on retry
            card.failures += 1
            retry_after = getattr(result, "retry_after", None) if result is not None else None
            try:
                retry_after = float(retry_after) if retry_after is not None else None
            except (TypeError, ValueError):
                retry_after = None
            if retry_after and retry_after > 0:
                card.flood_backoff = True
                card.backoff_until = now + retry_after
            else:
                card.flood_backoff = False
                card.backoff_until = now + min(_MAX_ERROR_BACKOFF, 2.0 ** card.failures)
        logger.debug("delegation card delivery failed: %s", str(error)[:200])

    async def _send_card(self, gid: str, card: _CardState, text: str, final: bool) -> Any:
        adapter = self._adapter
        updater = getattr(adapter, "send_or_update_status", None)
        if callable(updater):
            result = await _maybe_await(
                updater(self._chat_id, f"delegation:{gid}", text, metadata=self._metadata)
            )
            if getattr(result, "success", False) and getattr(result, "message_id", None):
                card.message_id = str(result.message_id)
            return result
        editor = getattr(adapter, "edit_message", None)
        if card.message_id and callable(editor):
            result = await _maybe_await(
                editor(self._chat_id, card.message_id, text, finalize=True, metadata=self._metadata)
            )
            if getattr(result, "success", False):
                return result
            if not _message_gone(result):
                # Transient (rate limit, network, server): report the failure
                # so _record_failure backs off and honours retry_after, and the
                # latest state is retried as an edit of the same card.
                return result
            # The card was deleted or can no longer be edited: post a
            # replacement once and keep editing that one.
            card.message_id = None
        elif card.message_id and not final:
            # No working edit path: don't turn every update into a new
            # message. Only the final state gets one more post.
            from gateway.platforms.base import SendResult

            return SendResult(success=True, message_id=card.message_id)
        result = await _maybe_await(adapter.send(self._chat_id, text, metadata=self._metadata))
        if getattr(result, "success", False) and getattr(result, "message_id", None):
            card.message_id = str(result.message_id)
        return result

    # -- final summary -----------------------------------------------------

    def _parent_busy(self) -> bool:
        try:
            return bool(self._is_parent_busy())
        except Exception:
            return False

    def _release_summary(self, gid: str) -> None:
        with self._state_lock:
            card = self._cards.get(gid)
            if card is None or card.summary_posted:
                return
            card.summary_released = True
            card.dirty = True
        self._schedule()

    def _recheck_summary(self, gid: str) -> None:
        """Poll while held: release once the parent is idle (covers a lost or
        never-fired post-delivery hook) or the hard cap passes."""
        with self._state_lock:
            card = self._cards.get(gid)
            if card is None or card.summary_posted or card.summary_released:
                return
            expired = self._clock() >= card.summary_deadline
        if expired or not self._parent_busy():
            self._release_summary(gid)
            return
        try:
            self._loop.call_later(_SUMMARY_POLL_SECONDS, self._recheck_summary, gid)
        except RuntimeError:
            pass

    async def _deliver_summary(self, gid: str, card: _CardState) -> None:
        """Post the finished group's summary once, at the bottom of the chat.

        Ordering contract: while the parent turn is still working, the
        summary is held until the parent's reply has been delivered (or a
        bounded fallback fires), so it never lands between the parent's tool
        bubbles and its answer. Then the live card is removed, leaving one
        clean summary message after the parent's reply.
        """
        with self._state_lock:
            card.dirty = False
            if card.summary_posted:
                return
            waiting = card.summary_waiting
            released = card.summary_released
        if not released and self._parent_busy():
            if not waiting:
                with self._state_lock:
                    card.summary_waiting = True
                    card.summary_deadline = self._clock() + self._max_final_defer
                if self._defer_final is not None:
                    try:
                        # Preferred release: right after the parent's reply.
                        self._defer_final(lambda: self._release_summary(gid))
                    except Exception:
                        logger.debug("delegation summary defer failed", exc_info=True)
                # Always poll too: releases once the parent goes idle even if
                # the hook was overwritten or never fires, without ever
                # posting while the parent is still working (until the cap).
                try:
                    self._loop.call_later(_SUMMARY_POLL_SECONDS, self._recheck_summary, gid)
                except RuntimeError:
                    pass
                # Meanwhile show the result in place on the live card, so a
                # long parent turn doesn't hide it.
                if self._board_mode:
                    _board_for(self).schedule(0.0)
                    return
                held = self._render(gid)
                if held and held != card.last_text and card.posted:
                    try:
                        res = await self._send_card(gid, card, held, True)
                        if getattr(res, "success", False):
                            with self._state_lock:
                                card.last_text = held
                    except Exception:
                        logger.debug("held summary edit failed", exc_info=True)
            return
        if self._combined and self._turn_over and not card.inbox_expired:
            if not card.in_inbox:
                self._park_in_inbox(gid, card)
            return
        text = self._render(gid)
        if not text:
            return
        with self._state_lock:
            card.last_attempt = self._clock()
        try:
            result = await _maybe_await(
                self._adapter.send(self._chat_id, text, metadata=self._metadata)
            )
        except Exception as exc:
            # Ambiguous (e.g. timeout after the platform accepted it):
            # retrying could post the summary twice. The parent still gets
            # the full result through the delegation completion message.
            logger.debug("delegation summary send raised; not retrying: %s", exc)
            with self._state_lock:
                card.summary_posted = True
            return
        if not getattr(result, "success", False):
            self._record_failure(card, result, getattr(result, "error", "send failed"))
            return
        with self._state_lock:
            card.summary_posted = True
            card.posted = True
            card.last_text = text
            card.last_publish = self._clock()
            card.failures = 0
            old_id = card.message_id
            card.message_id = str(getattr(result, "message_id", "") or "") or None
        await self._remove_live_card(gid, old_id)
        if self._board_mode:
            _board_for(self).schedule(0.0)

    async def _remove_live_card(self, gid: str, message_id: Optional[str]) -> None:
        adapter = self._adapter
        status_ids = getattr(adapter, "_status_message_ids", None)
        if isinstance(status_ids, dict):
            key_id = status_ids.pop((self._chat_id, f"delegation:{gid}"), None)
            message_id = message_id or (str(key_id) if key_id else None)
        if not message_id:
            return
        deleter = getattr(type(adapter), "delete_message", None)
        try:
            from gateway.platforms.base import BasePlatformAdapter

            can_delete = deleter is not None and deleter is not BasePlatformAdapter.delete_message
        except Exception:
            can_delete = deleter is not None
        try:
            if can_delete:
                await _maybe_await(adapter.delete_message(self._chat_id, message_id))
                return
            editor = getattr(adapter, "edit_message", None)
            if callable(editor):
                await _maybe_await(editor(
                    self._chat_id, message_id, "🔀 Subagent finished · summary below",
                    finalize=True, metadata=self._metadata,
                ))
        except Exception:
            logger.debug("removing live delegation card failed", exc_info=True)

    def _render(self, gid: str) -> str:
        limit = self._message_limit()
        text = _fit_to_limit(self.tracker.render(gid), limit - (8 if self._expandable else 0))
        if not (self._expandable and text):
            return text
        hint = TAP_HINT_DONE if self.tracker.group_finished(gid) else TAP_HINT_LIVE
        return _to_expandable(text, hint)

    def _message_limit(self) -> int:
        adapter = self._adapter
        try:
            fn = getattr(adapter, "max_message_length_for_chat", None)
            if callable(fn):
                return int(fn(self._chat_id) or 0) or 4096
            return int(getattr(adapter, "MAX_MESSAGE_LENGTH", 0) or 0) or 4096
        except Exception:
            return 4096

    async def _deliver_alerts(self) -> None:
        with self._state_lock:
            pending, self._pending_alerts = self._pending_alerts, []
        for gid, text in pending:
            card = self._cards.setdefault(gid, _CardState())
            if card.suppressed or (not card.posted and not self._current()):
                continue
            if card.alerts_sent >= _MAX_ALERTS_PER_GROUP:
                continue  # the card still shows the state; stop pinging
            card.alerts_sent += 1
            try:
                await _maybe_await(
                    self._adapter.send(self._chat_id, text, metadata=self._metadata)
                )
            except Exception:
                logger.debug("delegation alert delivery failed", exc_info=True)


_GONE_MARKERS = (
    "not found",
    "unknown message",
    "message to edit not found",
    "message can't be edited",
    "message_id_invalid",
    "deleted",
    "404",
    # The adapter this card was bound to was disconnected/replaced (e.g. a
    # Telegram reconnect) and cannot edit anymore: re-post on the live one.
    "not connected",
)


TAP_HINT_LIVE = "tap to watch 🍀"
TAP_HINT_DONE = "tap to read 🍀"


def _to_expandable(text: str, hint: str = "", bold_title: bool = False) -> str:
    """Header + "> " detail lines -> one Telegram expandable blockquote:
    ``**> header`` / ``> detail`` / ... with ``||`` closing the last line.

    The header is the quote's first line, so a collapsed quote shows the
    header, the italic hint and a blank line.  ``bold_title`` (the unified
    subagent card) keeps the title's bold: ``**> 🍀 **title** · 1 of 3 done``."""
    # "||" would be read as a spoiler / early quote terminator, and "*" in
    # the header can eat the "**>" opener (Opus review).
    lines = text.replace("||", "¦¦").split("\n")
    body = [ln[2:] if ln.startswith("> ") else ln for ln in lines[1:]]
    # A collapsed Telegram quote shows about three lines and then cuts off.
    # Header, a small italic "tap" hint, and a blank line fill those three,
    # so the cut lands on empty space, never on half a detail.
    details = [ln for ln in body if ln.strip()]
    spacer = "\u2800"
    # The hint is followed by exactly one spacer: a detail block that opens
    # with its own spacer would leave two blank lines there.
    while details and details[0].strip() == spacer:
        details.pop(0)
    header = lines[0] if bold_title else lines[0].replace("*", "")
    out = [f"**> {header}"]
    if details:
        tip = f"*{hint}*" if hint else spacer
        out += [f"> {tip}"]
        out += [f"> {spacer}"]
        out += [f"> {ln}" for ln in details]
    return "\n".join(out) + "||"


def _message_gone(result: Any) -> bool:
    """True only for a confirmed "that message no longer exists / is not editable"."""
    if result is None:
        return False
    error = str(getattr(result, "error", "") or "").lower()
    return any(marker in error for marker in _GONE_MARKERS)


def _error_kind(result: Any) -> str:
    """A short, log-safe label for a failed send/edit: the platform's error
    string cut to one short line (never the message text we tried to send)."""
    error = " ".join(str(getattr(result, "error", "") or "").split())
    if not error:
        return "unknown"
    return error if len(error) <= 80 else error[:79] + "…"


def _fit_to_limit(text: str, limit: int) -> str:
    """Trim a card to the adapter's message limit on a line boundary."""
    if not text or len(text) <= limit:
        return text
    budget = max(16, limit - 2)
    cut = text[:budget].rsplit("\n", 1)[0]
    return (cut or text[:budget]) + "\n…"


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _combined_liveness_probe(worker_id: str) -> Dict[str, Any]:
    """Liveness for in-process children and registered external agent jobs."""
    if str(worker_id).startswith("proc_"):
        from tools.agent_job_observer import agent_job_liveness

        return agent_job_liveness(worker_id)
    from tools.delegate_tool import get_subagent_liveness

    return get_subagent_liveness(worker_id)


def build_turn_publisher(
    runner: Any,
    source: Any,
    user_config: Any,
    platform_key: str,
    run_still_current: Callable[[], bool],
) -> Optional[DelegationActivityPublisher]:
    """Create the publisher for one gateway turn, or None when disabled."""
    enabled, heartbeat = resolve_delegation_activity(user_config, platform_key)
    if not enabled:
        return None
    adapter = runner._adapter_for_source(source)
    if adapter is None or getattr(source, "chat_id", None) in (None, ""):
        return None
    try:
        metadata = runner._thread_metadata_for_source(source)
    except Exception:
        metadata = None
    probe = _combined_liveness_probe
    is_telegram = getattr(getattr(source, "platform", None), "value", None) == "telegram"
    cfg = user_config if isinstance(user_config, dict) else {}
    try:
        from gateway.display_config import resolve_display_setting

        turn_card = str(resolve_display_setting(cfg, platform_key, "turn_card", "combined")).lower()
    except Exception:
        turn_card = "combined"
    return DelegationActivityPublisher(
        adapter=adapter,
        chat_id=source.chat_id,
        metadata=metadata,
        loop=asyncio.get_running_loop(),
        is_current=run_still_current,
        heartbeat_seconds=heartbeat,
        liveness_probe=probe,
        # Posted as soon as the subagent finishes, so its summary sits ABOVE
        # the parent's next reply (which then needn't repeat it).
        expandable=is_telegram,
        # display.turn_card: "combined" (default) folds results into the
        # turn's own card; "separate" restores standalone summary cards.
        combined=is_telegram and turn_card != "separate",
    )


# ── The ack message as a live checklist ────────────────────────────────────
# The final reply of a turn that handed its work to a helper is the checklist
# text of ``agent.ack_checklist``.  When an adapter sends that text, the
# message it became is bound to its card here, and every later change of the
# checklist (a helper todo ticked, the helper finished) edits that same
# message.  Never a new message; never blocks or kills the helper: a missing
# edit transport or a deleted message just leaves the last text in place.

ACK_MIN_INTERVAL = DEFAULT_MIN_INTERVAL
ACK_TICK_SECONDS = 15.0
# A running unified card follows the bottom of the chat like the 🔀 card did,
# but re-posts at most this often.
ACK_REPOST_MIN_SECONDS = 4.0
_ACK_EDITORS: Dict[int, "AckCardEditor"] = {}
_ACK_SEQ = itertools.count(1)


def _adapter_is_telegram(adapter: Any) -> bool:
    """Telegram only has expandable quotes (the 🔀 roster card checks the same)."""
    plat = getattr(adapter, "platform", None)
    return str(getattr(plat, "value", plat) or "").lower() == "telegram"


def _unified_style() -> bool:
    try:
        from agent.ack_checklist import unified

        return bool(unified())
    except Exception:
        return False


# -- the finished card sits right under the reply it belongs to ---------------
#
# A part that ended is one line.  It is posted ONCE, immediately AFTER the
# root agent's reply that handles its completion (the turn the gateway tags
# with ``completion_delegation_ids``).  A turn reads, top to bottom: the
# ``🛠 N tool calls`` bubble, the reply, the finished card(s) in dispatch
# order, then a running card (if the reply handed off a new job).  Nothing
# else moves a finished card.

class CompletionTurn:
    """The delegation ids a turn reports, and whether its first outbound
    message went out yet (the one the finished cards must sit under)."""

    __slots__ = ("ids", "fired")

    def __init__(self, ids: Sequence[str]) -> None:
        self.ids = tuple(ids)
        self.fired = False


_COMPLETION_TURN: "contextvars.ContextVar[Optional[CompletionTurn]]" = contextvars.ContextVar(
    "_deleg_completion_turn", default=None
)


def start_completion_turn(ids: Optional[Sequence[str]]) -> Optional[CompletionTurn]:
    """Mark the current task as reporting the delegation ``ids`` (``None`` /
    empty: not a completion turn).  Every path that runs a turn carrying
    ``completion_delegation_ids`` must come through here -- a turn that
    starts on its own (``begin_completion_turn``) and the in-band queued
    follow-up (``hand_over_completion_turn``) alike."""
    turn: Optional[CompletionTurn] = None
    try:
        clean = [i for i in (ids or ()) if isinstance(i, str) and i.strip()]
        if clean:
            turn = CompletionTurn(clean)
    except Exception:
        turn = None
    _COMPLETION_TURN.set(turn)
    return turn


def begin_completion_turn(event: Any) -> Optional[CompletionTurn]:
    """Mark the current task as handling ``event`` (None for any turn that is
    not an async-delegation completion).  Called where a turn starts."""
    ids = None
    try:
        if getattr(event, "internal", False):
            ids = (getattr(event, "metadata", None) or {}).get("completion_delegation_ids")
            if not isinstance(ids, (list, tuple)):
                ids = None
    except Exception:
        ids = None
    return start_completion_turn(ids)


async def hand_over_completion_turn(
    adapter: Any, chat_id: Any, ids: Optional[Sequence[str]], metadata: Any = None,
) -> Optional[CompletionTurn]:
    """A queued follow-up is about to run in-band, inside the turn that was
    running when it arrived (a human turn, or another completion turn).

    The running turn's reply is already out, so it never fires the follow-up's
    cards; a completion turn of its own that has not settled yet (it ended
    silent) is settled first.  Then the current task becomes the follow-up's
    completion turn when ``ids`` are given (``None`` for a human follow-up, so
    it never re-posts anybody's cards), and its reply -- sent by this same task
    or by a task it spawns (the stream consumer) -- gets the finished cards
    right under it.  Left set on purpose: the final send happens after the
    follow-up returns."""
    prev = _COMPLETION_TURN.get()
    if prev is not None and not prev.fired and chat_id not in (None, ""):
        prev.fired = True
        try:
            await settle_completion(adapter, chat_id, prev.ids, silent=True, metadata=metadata)
        except Exception:
            logger.debug("settling the previous completion turn failed", exc_info=True)
    return start_completion_turn(ids)


# True inside the tasks that post a turn's progress / status messages (the
# tool-progress bubble that becomes "🛠 9 tool calls · ⏱ 1m44s", thoughts,
# heartbeats).  Those are NOT the reply: the finished card is placed after
# the REPLY, never after one of these.
_STATUS_SEND: "contextvars.ContextVar[bool]" = contextvars.ContextVar("_deleg_status_send", default=False)


@contextlib.contextmanager
def status_send():
    """Mark the sends made inside as progress / status messages, not the reply."""
    token = _STATUS_SEND.set(True)
    try:
        yield
    finally:
        _STATUS_SEND.reset(token)


async def run_as_status_task(coro: Any) -> Any:
    """Await ``coro`` (the progress sender task) with its sends marked as status."""
    with status_send():
        return await coro


def _is_status_send(metadata: Any) -> bool:
    if _STATUS_SEND.get():
        return True
    return isinstance(metadata, dict) and bool(metadata.get("_interim_send"))


async def _post_liner(adapter: Any, chat_id: Any, text: str, metadata: Any) -> Optional[str]:
    """Send one finished line; returns its message id (None if unknown)."""
    kwargs: Dict[str, Any] = {}
    if metadata is not None:
        kwargs["metadata"] = metadata
    try:
        result = await _maybe_await(adapter.send(chat_id, _fit_to_limit(text, 4000), **kwargs))
    except TypeError:
        result = await _maybe_await(adapter.send(chat_id, _fit_to_limit(text, 4000)))
    mid = getattr(result, "message_id", None)
    return str(mid) if mid and str(mid) != "__no_edit__" else None


async def settle_completion(
    adapter: Any, chat_id: Any, ids: Sequence[str], *, silent: bool, metadata: Any = None,
) -> int:
    """Put the finished one-liners of these delegation ids in place.

    ``silent=False``: the reply just went out, so every ended part gets its
    one-line message right under it, in dispatch order (the old card message
    is deleted and re-posted as the newest message in the chat).
    ``silent=True``: nothing will be sent; a stopped part just disappears, a
    done / failed one still posts its line.  Once per part.  Never raises;
    returns how many messages were posted or moved."""
    from agent import ack_checklist as ac

    moved = 0
    last_id: Optional[str] = None
    token = _CARD_SEND.set(True)
    try:
        for part in ac.pending_liner_parts(ids):
            if part.liner_done:
                continue
            card = part.card
            stopped = part.checklist.all_stopped
            editor = _ACK_EDITORS.get(id(card)) if card is not None else None
            if editor is not None and _follow_key(adapter, chat_id, metadata) != _follow_key(
                editor._adapter, editor._chat_id, editor._metadata,
            ):
                continue  # another adapter, chat or topic's card
            is_card = bool(card is not None and card.finished and card.final_part() is part)
            if is_card:
                part.liner_done = True
                if stopped and silent:
                    if editor is not None:
                        await editor.delete_card()
                    continue
                if editor is not None:
                    moved += await editor.repost_final()
                    last_id = editor._message_id or last_id
                else:
                    last_id = await _post_liner(adapter, chat_id, card.part_liner(part), metadata) or last_id
                    moved += 1
                continue
            part.liner_done = True
            if stopped and silent:
                continue
            text = card.part_liner(part) if card is not None else part.checklist.render()
            last_id = await _post_liner(adapter, chat_id, text, metadata) or last_id
            moved += 1
    except Exception:
        logger.debug("settle_completion failed", exc_info=True)
    finally:
        _CARD_SEND.reset(token)
    if moved and last_id:
        # A card that is still running stays BELOW the finished ones: the
        # newest finished message counts as "something newer" for it.
        try:
            key = _follow_key(adapter, chat_id, metadata)
            if _is_newer(last_id, _LAST_PLAIN.get(key)):
                _LAST_PLAIN[key] = last_id
            if _is_newer(last_id, _LAST_OUT.get(key)):
                _LAST_OUT[key] = last_id
            await follow_latest_message(adapter, chat_id, metadata)
        except Exception:
            logger.debug("running card follow after settle failed", exc_info=True)
    return moved


async def after_reply(adapter: Any, chat_id: Any, content: Any, metadata: Any = None) -> None:
    """Called by the adapter's ``send`` after a message went out: the first
    reply of a completion turn (not a progress / status message, not a card)
    gets the finished cards re-posted right under it."""
    turn = _COMPLETION_TURN.get()
    if turn is None or turn.fired or _CARD_SEND.get() or chat_id in (None, ""):
        return
    if _is_ack_card_text(content) or _is_status_send(metadata):
        return
    turn.fired = True
    await settle_completion(adapter, chat_id, turn.ids, silent=False, metadata=metadata)


async def after_silent_reply(adapter: Any, chat_id: Any, metadata: Any = None) -> None:
    """The completion turn ended and sent nothing (``[SILENT]``): a stopped part
    just disappears, a done / failed one still posts its line."""
    turn = _COMPLETION_TURN.get()
    if turn is None or turn.fired or chat_id in (None, ""):
        return
    turn.fired = True
    await settle_completion(adapter, chat_id, turn.ids, silent=True, metadata=metadata)


class AckCardEditor:
    """Edits one sent ack message to follow its checklist (coalesced, throttled)."""

    def __init__(
        self,
        card: Any,
        adapter: Any,
        chat_id: Any,
        message_id: str,
        loop: asyncio.AbstractEventLoop,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        min_interval: float = ACK_MIN_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
        initial_text: Optional[str] = None,
    ) -> None:
        self.card = card
        self._adapter = adapter
        self._chat_id = str(chat_id)
        self._message_id = str(message_id)
        self._loop = loop
        self._metadata = metadata
        self._min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self._lock = threading.Lock()
        # Telegram: title visible, the rest in a collapsed expandable quote
        # (like the 🔀 roster card); other platforms keep the plain quote.
        self._expandable = _adapter_is_telegram(adapter) and _unified_style()
        self._last_text = initial_text if initial_text is not None else card.render()
        # Let the message settle first: a stream consumer may still finalize it.
        self._last_attempt = clock()
        self._backoff_until = float("-inf")
        self._failures = 0
        self._task: Optional[asyncio.Task] = None
        self._ticking = False
        self.gone = False
        self._edit_lock: Optional[asyncio.Lock] = None
        self._last_repost = float("-inf")
        self._repost_timer: Any = None
        self._stale: List[str] = []
        self._registered = False
        self._seq = next(_ACK_SEQ)
        # Shares the follow protocol with the 🔀 cards (follow_latest_message).
        self._combined = False
        card.on_change = self.notify
        self._logged_final = False
        self._register_live()

    def _finished_safe(self) -> bool:
        try:
            return bool(self.card.finished)
        except Exception:
            return False

    def _parts_safe(self) -> int:
        try:
            return len(self.card.parts)
        except Exception:
            return 0

    def _render(self) -> str:
        """What the card message should say now."""
        text = self.card.render()
        # The title must be the first line: a card that still carries its
        # opener in the same message (never on a chat platform) stays plain.
        if not (self._expandable and text) or (self.card.say_text() and not self.card.split_opener):
            return text
        if self.card.finished:
            # A finished card is one plain line (no quote, no hint): the edit
            # that finishes it swaps the expandable quote for plain text.
            return _fit_to_limit(text, 4000 - 16)
        text = _fit_to_limit(text, 4000 - 16)
        hint = TAP_HINT_DONE if self.card.finished else TAP_HINT_LIVE
        # Collapsed, the card shows exactly three lines: header / hint / blank.
        # One subagent: stats line, hint, blank, then title, Goal, steps,
        # tools.  Several: "N subagents" header, hint, blank, then the job
        # title(s) and the sections.  Nothing of the body peeks through.
        return _to_expandable(text, hint, bold_title=True)

    # any thread
    def notify(self) -> None:
        if self.gone:
            return
        try:
            self._loop.call_soon_threadsafe(self._ensure_task)
        except RuntimeError:
            pass  # loop closed: gateway shutting down

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = self._loop.create_task(self._run())

    def _tick_later(self) -> None:
        """The unified card shows a running clock: refresh it now and then
        while the helper works, even when nothing else changed."""
        if self.gone or self.card.finished or self._ticking:
            return
        try:
            from agent.ack_checklist import unified

            if not unified():
                return
        except Exception:
            return

        def _go() -> None:
            self._ticking = False
            self.notify()

        self._ticking = True
        try:
            self._loop.call_later(ACK_TICK_SECONDS, _go)
        except RuntimeError:
            self._ticking = False

    def _delay(self) -> float:
        return max(0.0, self._last_attempt + self._min_interval - self._clock(),
                   self._backoff_until - self._clock())

    # -- follow the bottom of the chat (running unified cards only) ----------

    @property
    def _closed(self) -> bool:
        """Not eligible to follow: gone, or finished (a done card stays put)."""
        if self.gone:
            return True
        try:
            return bool(self.card.finished)
        except Exception:
            return True

    def _register_live(self) -> None:
        if self._registered or self._closed:
            return
        try:
            from agent.ack_checklist import unified

            if not unified() or not _adapter_can_delete(self._adapter):
                return
        except Exception:
            return
        key = _follow_key(self._adapter, self._chat_id, self._metadata)
        with _LIVE_LOCK:
            pubs = _LIVE.setdefault(key, [])
            if self not in pubs:
                pubs.append(self)
                if len(pubs) > _LIVE_MAX_PER_CHAT:
                    del pubs[: len(pubs) - _LIVE_MAX_PER_CHAT]
        self._registered = True

    def _unregister_live(self) -> None:
        if not self._registered:
            return
        self._registered = False
        key = _follow_key(self._adapter, self._chat_id, self._metadata)
        with _LIVE_LOCK:
            pubs = _LIVE.get(key)
            if pubs and self in pubs:
                pubs.remove(self)
                if not pubs:
                    _LIVE.pop(key, None)

    async def repost_live_cards(self) -> int:
        """Re-post the card at the bottom when something newer was sent after
        it, delete the old message, and keep editing the new one. Throttled;
        a finished card never moves. Returns 1 if it moved."""
        if self._closed:
            self._unregister_live()
            return 0
        if self._edit_lock is None:
            self._edit_lock = asyncio.Lock()
        wait = self._last_repost + ACK_REPOST_MIN_SECONDS - self._clock()
        if wait > 0:
            self._repost_later(wait)
            return 0
        token = _CARD_SEND.set(True)
        try:
            async with self._edit_lock:
                return await self._repost_locked()
        except Exception:
            logger.debug("ack card repost failed", exc_info=True)
            return 0
        finally:
            _CARD_SEND.reset(token)

    def _repost_later(self, delay: float) -> None:
        if self._repost_timer is not None:
            return

        def _go() -> None:
            self._repost_timer = None
            if not self._closed:
                self._loop.create_task(self.repost_live_cards())

        try:
            self._repost_timer = self._loop.call_later(max(0.0, delay), _go)
        except RuntimeError:
            self._repost_timer = None

    def _is_newest_running(self) -> bool:
        """Only the newest running card in a chat follows; older ones stay put
        (two cards chasing each other's messages would loop forever)."""
        key = _follow_key(self._adapter, self._chat_id, self._metadata)
        with _LIVE_LOCK:
            peers = [p for p in _LIVE.get(key, []) if isinstance(p, AckCardEditor) and not p._closed]
        return all(p._seq <= self._seq for p in peers)

    async def repost_final(self) -> int:
        """One last move for a card that ended: delete it and re-post its
        one-line form at the bottom, right under the reply it belongs to.
        Does nothing when the card already is the newest message in the chat.
        Returns 1 if it moved."""
        if self.gone:
            return 0
        if self._edit_lock is None:
            self._edit_lock = asyncio.Lock()
        token = _CARD_SEND.set(True)
        try:
            async with self._edit_lock:
                return await self._repost_locked(final=True)
        except Exception as exc:
            logger.info(
                "helper card final repost raised: chat=%s message=%s (%s)",
                self._chat_id, self._message_id, type(exc).__name__,
            )
            logger.debug("ack card final repost failed", exc_info=True)
            return 0
        finally:
            _CARD_SEND.reset(token)

    async def delete_card(self) -> bool:
        """Remove the card message (a job that was stopped on purpose leaves
        nothing behind).  True when it is gone."""
        if self._edit_lock is None:
            self._edit_lock = asyncio.Lock()
        token = _CARD_SEND.set(True)
        try:
            async with self._edit_lock:
                self.gone = True
                self._unregister_live()
                if not _adapter_can_delete(self._adapter):
                    return False
                await _maybe_await(self._adapter.delete_message(self._chat_id, self._message_id))
                return True
        except Exception:
            logger.debug("ack card delete failed", exc_info=True)
            return False
        finally:
            _CARD_SEND.reset(token)

    async def _repost_locked(self, final: bool = False) -> int:
        if not final:
            if self._closed:
                self._unregister_live()
                return 0
            if not self._is_newest_running():
                return 0
        # Only a message that is not a helper card counts as "newer".
        last_out = _LAST_PLAIN.get(_follow_key(self._adapter, self._chat_id, self._metadata))
        if not (last_out and _is_newer(last_out, self._message_id)):
            return 0  # already the newest message in the chat
        if not final:
            self._last_repost = self._clock()
        text = self._render()
        if not text:
            return 0
        try:
            kwargs: Dict[str, Any] = {}
            if self._metadata is not None:
                kwargs["metadata"] = self._metadata
            result = await _maybe_await(
                self._adapter.send(self._chat_id, _fit_to_limit(text, 4000), **kwargs)
            )
        except Exception as exc:
            if final:
                logger.info(
                    "helper card final repost send raised: chat=%s message=%s (%s)",
                    self._chat_id, self._message_id, type(exc).__name__,
                )
            logger.debug("ack card repost send failed", exc_info=True)
            return 0
        new_id = getattr(result, "message_id", None)
        if not (getattr(result, "success", False) and new_id) or str(new_id) == "__no_edit__":
            if final:
                logger.info(
                    "helper card final repost failed: chat=%s message=%s error=%s",
                    self._chat_id, self._message_id, _error_kind(result),
                )
            return 0
        old_id = self._message_id
        self._message_id = str(new_id)
        self._last_text = text
        self._last_attempt = self._clock()
        self._failures = 0
        self._backoff_until = float("-inf")
        self.card.target = (id(self._adapter), self._chat_id, self._message_id)
        _note_card_location(self.card, self._adapter, self._chat_id, self._message_id, self._metadata)
        for mid in [old_id] + list(self._stale):
            if not mid or mid == self._message_id:
                continue
            try:
                deleted = await _maybe_await(self._adapter.delete_message(self._chat_id, mid))
                if deleted is False:
                    if mid not in self._stale:
                        self._stale.append(mid)
                    continue
                if mid in self._stale:
                    self._stale.remove(mid)
            except Exception as exc:
                logger.info(
                    "helper card old message delete raised: chat=%s message=%s (%s)",
                    self._chat_id, mid, type(exc).__name__,
                )
                if mid not in self._stale:
                    self._stale.append(mid)
        del self._stale[8:]
        if final:
            logger.info(
                "helper card finalized (moved under the reply): chat=%s message=%s old=%s parts=%d",
                self._chat_id, self._message_id, old_id, self._parts_safe(),
            )
        if self._render() != self._last_text:
            self.notify()  # changed while we were sending
        return 1

    async def _run(self) -> None:
        # Bursts of ticks collapse: sleep out the gate, then render the latest.
        self._register_live()
        while not self.gone:
            delay = self._delay()
            if delay > 0:
                await asyncio.sleep(delay)
                continue
            text = self._render()
            if text == self._last_text:
                self._tick_later()
                return
            if self._edit_lock is None:
                self._edit_lock = asyncio.Lock()
            async with self._edit_lock:
                # A repost may have run while we waited: edit what is current.
                ok = await self._edit(self._render())
            if not ok:
                if self.gone or self._failures >= 8:
                    return
                continue
            if self._render() == self._last_text:
                self._tick_later()
                return

    async def _edit(self, text: str) -> bool:
        editor = getattr(self._adapter, "edit_message", None)
        self._last_attempt = self._clock()
        if not callable(editor):
            self.gone = True  # no edit transport: the static text stays
            return False
        token = _CARD_SEND.set(True)
        try:
            # Always finalize=True: Telegram only formats (MarkdownV2) on the
            # finalize path; finalize=False is the plain-text streaming path and
            # would show literal ``**`` / ``>`` on every live edit. The text is
            # already fitted, so the overflow split never triggers.
            kwargs: Dict[str, Any] = {"finalize": True}
            if self._metadata is not None:
                kwargs["metadata"] = self._metadata
            try:
                result = await _maybe_await(
                    editor(self._chat_id, self._message_id, _fit_to_limit(text, 4000), **kwargs)
                )
            except TypeError:
                kwargs.pop("metadata", None)
                result = await _maybe_await(
                    editor(self._chat_id, self._message_id, _fit_to_limit(text, 4000), **kwargs)
                )
        except Exception as exc:
            result = None
            # Proof for "the card stayed up": the error type only, never text.
            logger.info(
                "helper card edit raised: chat=%s message=%s finished=%s (%s)",
                self._chat_id, self._message_id, self._finished_safe(), type(exc).__name__,
            )
        finally:
            _CARD_SEND.reset(token)
        if result is not None and getattr(result, "success", False):
            self._last_text = text
            self._failures = 0
            self._backoff_until = float("-inf")
            # A native ack may already have been sent before it joins. Remove
            # that duplicate only after the shared card contains its section.
            for mid in list(self._stale):
                try:
                    deleted = await _maybe_await(self._adapter.delete_message(self._chat_id, mid))
                    if deleted is not False:
                        self._stale.remove(mid)
                except Exception:
                    logger.debug("joined ack duplicate delete failed", exc_info=True)
            if self._finished_safe() and not self._logged_final:
                self._logged_final = True
                logger.info(
                    "helper card finalized: chat=%s message=%s parts=%d",
                    self._chat_id, self._message_id, self._parts_safe(),
                )
            return True
        if _message_gone(result):
            self.gone = True
            logger.info(
                "helper card edit: message gone, card closed: chat=%s message=%s finished=%s",
                self._chat_id, self._message_id, self._finished_safe(),
            )
            return False
        if result is not None:
            logger.info(
                "helper card edit failed: chat=%s message=%s finished=%s failures=%d error=%s",
                self._chat_id, self._message_id, self._finished_safe(), self._failures + 1,
                _error_kind(result),
            )
        self._failures += 1
        retry_after = getattr(result, "retry_after", None) if result is not None else None
        try:
            retry_after = float(retry_after) if retry_after is not None else None
        except (TypeError, ValueError):
            retry_after = None
        wait = retry_after if retry_after and retry_after > 0 else min(_MAX_ERROR_BACKOFF, 2.0 ** self._failures)
        self._backoff_until = self._clock() + wait
        return False


def bind_ack_card(adapter: Any, chat_id: Any, content: Any, result: Any, metadata: Any = None) -> Any:
    """Called after an adapter sent ``content``: if it is an ack checklist that
    is not yet placed, remember where it landed and keep it current.  Never raises.

    Two shapes of ack text:
      * the whole card (classic style, or no opener line): the message that was
        just sent *is* the card; bind an editor to it;
      * the opener line alone (unified card with a ``say``): that message stays a
        plain chat message forever.  Returns a coroutine that sends the card as
        its own message right under it and binds the editor to that one; the
        sending wrapper awaits it so the order is opener, then card.
    """
    try:
        if adapter is None or chat_id in (None, "") or not getattr(result, "success", False):
            return None
        message_id = getattr(result, "message_id", None)
        if not message_id or str(message_id) == "__no_edit__":
            return None
        from agent.ack_checklist import match_unbound, match_unbound_opener

        card = match_unbound(content)
        if card is None:
            opener_card = match_unbound_opener(content)
            if opener_card is None:
                return None
            # A card is already running in this chat: the new dispatch joins it
            # (one live card per chat); only its opener line is a message.
            host = _running_editor_for(adapter, chat_id, opener_card, metadata)
            if host is not None:
                from agent.ack_checklist import join

                if join(host.card, opener_card):
                    _note_card_location(host.card, adapter, chat_id, host._message_id, metadata)
                    host.notify()
                    return None
            # Claim it now so nothing else binds it while the card is sent.
            opener_card.target = (id(adapter), str(chat_id), "pending")
            return _send_card_under_opener(opener_card, adapter, chat_id, metadata)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        if _unified_style() and not card.say_text():
            return _bind_sent_unified(card, adapter, chat_id, str(message_id), loop, metadata)
        _attach_editor(card, adapter, chat_id, str(message_id), loop, metadata)
    except Exception:
        logger.debug("ack card bind failed", exc_info=True)
    return None


async def _bind_sent_unified(card: Any, adapter: Any, chat_id: Any, message_id: str,
                             loop: asyncio.AbstractEventLoop, metadata: Any) -> None:
    """A native handoff with no opener sends its card as the final reply.

    It is still a joinable unified card, not a stationary combined opener.
    The send already happened; retire it only after its shared section lands.
    """
    from agent.ack_checklist import join

    card.split_opener = True
    host = _running_editor_for(adapter, chat_id, card, metadata)
    if host is not None and _adapter_can_delete(adapter):
        if host._edit_lock is None:
            host._edit_lock = asyncio.Lock()
        async with host._edit_lock:
            if join(host.card, card):
                _note_card_location(host.card, adapter, chat_id, host._message_id, metadata)
                host._stale.append(message_id)
                host.notify()  # Keep the normal edit throttle and server backoff.
                return
    _attach_editor(card, adapter, chat_id, message_id, loop, metadata)


def _running_editor_for(adapter: Any, chat_id: Any, new_card: Any = None, metadata: Any = None) -> Optional["AckCardEditor"]:
    """The editor of the running unified card in this chat (newest first), if any."""
    for editor in reversed(list(_ACK_EDITORS.values())):
        if editor.card is new_card or editor._adapter is not adapter or editor._chat_id != str(chat_id):
            continue
        if _follow_key(adapter, chat_id, metadata) != _follow_key(editor._adapter, editor._chat_id, editor._metadata):
            continue
        if editor._closed or not getattr(editor.card, "split_opener", False):
            continue
        return editor
    return None


def _note_card_location(card: Any, adapter: Any, chat_id: Any, message_id: Any, metadata: Any = None) -> None:
    """Keep, for every dispatch on this card, where its message is: a gateway
    restart that starts the helper again puts the card back in the same message
    (``tools.delegation_inflight``).  Never raises."""
    try:
        from tools import delegation_inflight

        platform = str(getattr(getattr(adapter, "platform", None), "value", "") or "")
        thread = ""
        if isinstance(metadata, dict):
            thread = str(metadata.get("thread_id") or "")
        for part in list(getattr(card, "parts", ()) or ()):
            delegation_inflight.note_card(
                part.delegation_id, platform=platform, chat_id=str(chat_id),
                thread_id=thread, message_id=str(message_id),
            )
    except Exception:
        logger.debug("could not note the card location", exc_info=True)


def _attach_editor(
    card: Any, adapter: Any, chat_id: Any, message_id: str,
    loop: asyncio.AbstractEventLoop, metadata: Any = None, initial_text: Optional[str] = None,
) -> "AckCardEditor":
    editor = AckCardEditor(card, adapter, chat_id, message_id, loop, metadata, initial_text=initial_text)
    card.target = (id(adapter), str(chat_id), str(message_id))
    _note_card_location(card, adapter, chat_id, message_id, metadata)
    _ACK_EDITORS[id(card)] = editor
    while len(_ACK_EDITORS) > 32:
        _ACK_EDITORS.pop(next(iter(_ACK_EDITORS)))
    if editor._render() != editor._last_text or card.finished:
        editor.notify()  # it already moved on between render and send
    return editor


async def _send_card_under_opener(card: Any, adapter: Any, chat_id: Any, metadata: Any) -> None:
    """Send the card as its own message (the opener was just delivered above it)."""
    loop = asyncio.get_running_loop()
    # Build the editor first so the text we send is exactly what it renders.
    probe = AckCardEditor.__new__(AckCardEditor)
    probe.card = card
    probe._expandable = _adapter_is_telegram(adapter) and _unified_style()
    text = AckCardEditor._render(probe)
    if not text:
        card.target = None
        return
    token = _CARD_SEND.set(True)
    try:
        kwargs: Dict[str, Any] = {}
        if metadata is not None:
            kwargs["metadata"] = metadata
        result = await _maybe_await(adapter.send(chat_id, _fit_to_limit(text, 4000), **kwargs))
    except Exception:
        logger.debug("ack card send under the opener failed", exc_info=True)
        card.target = None
        return
    finally:
        _CARD_SEND.reset(token)
    new_id = getattr(result, "message_id", None)
    if not (getattr(result, "success", False) and new_id) or str(new_id) == "__no_edit__":
        card.target = None  # the opener stands alone; nothing to keep current
        return
    _attach_editor(card, adapter, chat_id, str(new_id), loop, metadata, initial_text=text)
