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
import contextvars
import inspect
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

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


def note_outbound(adapter: Any, chat_id: Any, message_id: Any = None) -> None:
    """Record that the bot just posted a message in this chat. When running
    subagent cards live there, move them below it after a short quiet window
    (coalesces streaming bursts into one move). Never raises."""
    try:
        if _CARD_SEND.get() or adapter is None or chat_id in (None, ""):
            return
        key = _inbox_key(adapter, chat_id)
        if message_id:
            prev = _LAST_OUT.get(key)
            if prev is None or _is_newer(str(message_id), prev):
                _LAST_OUT[key] = str(message_id)
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
                lambda: (_FOLLOW_TIMERS.pop(key, None), loop.create_task(follow_latest_message(adapter, chat_id))),
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


async def follow_latest_message(adapter: Any, chat_id: Any) -> int:
    """Move every live (still running) delegation card in this chat below the
    message just delivered. Returns how many cards were moved.

    Only on adapters that can delete: without delete, re-posting would leave
    duplicate cards behind, which is worse than a card that stays put.
    """
    if adapter is None or chat_id in (None, "") or not _adapter_can_delete(adapter):
        return 0
    key = _inbox_key(adapter, chat_id)
    with _LIVE_LOCK:
        pubs = [p for p in _LIVE.get(key, []) if not p._closed]
        if pubs:
            _LIVE[key] = pubs
        else:
            _LIVE.pop(key, None)
    moved = 0
    for pub in pubs:
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
        # Fallback group for relays that carry no delegation_id (e.g. live
        # transcript creation failed). Per publisher, so two turns in one chat
        # never share a status key like "delegation:delegation".
        self._fallback_group = f"deleg_{uuid.uuid4().hex[:8]}"
        with _LIVE_LOCK:
            pubs = _LIVE.setdefault(_inbox_key(adapter, self._chat_id), [])
            pubs.append(self)
            # Publishers are per turn and never explicitly closed; idle ones
            # drop out on the next follow pass. Cap as a backstop.
            if len(pubs) > _LIVE_MAX_PER_CHAT:
                del pubs[: len(pubs) - _LIVE_MAX_PER_CHAT]

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
        if self._closed:
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
        try:
            result = await self._send_card(gid, card, text, final)
        except Exception as exc:
            self._record_failure(card, None, f"{type(exc).__name__}: {exc}")
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
)


TAP_HINT_LIVE = "tap to watch 🍀"
TAP_HINT_DONE = "tap to read 🍀"


def _to_expandable(text: str, hint: str = "") -> str:
    """Header + "> " detail lines -> one Telegram expandable blockquote:
    ``**> header`` / ``> detail`` / ... with ``||`` closing the last line."""
    # "||" would be read as a spoiler / early quote terminator, and "*" in
    # the header can eat the "**>" opener (Opus review).
    lines = text.replace("||", "¦¦").split("\n")
    header = lines[0].replace("*", "")
    body = [ln[2:] if ln.startswith("> ") else ln for ln in lines[1:]]
    # A collapsed Telegram quote shows about three lines and then cuts off.
    # Header, a small italic "tap" hint, and a blank line fill those three,
    # so the cut lands on empty space, never on half a detail.
    details = [ln for ln in body if ln.strip()]
    spacer = "\u2800"
    out = [f"**> {header}"]
    if details:
        tip = f"*{hint}*" if hint else spacer
        out += [f"> {tip}", f"> {spacer}"] + [f"> {ln}" for ln in details]
    return "\n".join(out) + "||"


def _message_gone(result: Any) -> bool:
    """True only for a confirmed "that message no longer exists / is not editable"."""
    if result is None:
        return False
    error = str(getattr(result, "error", "") or "").lower()
    return any(marker in error for marker in _GONE_MARKERS)


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
