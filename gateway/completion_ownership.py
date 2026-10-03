"""Typed route ownership for background completions (invariant I1).

A background result (delegation completion, background-process completion,
council verdict) is stamped with a session id when it is spawned. That id may
be a delegated child's, may predate a ``/new``, or may have been compressed
since. :func:`resolve_owner` answers one question without ever touching the
route: *which conversation owns this result, and does this route's current
session descend from it?*

Session edges are typed by the markers the writers already stamp, never by a
bare ``parent_session_id`` (one column carries four edge kinds):

``delegation``   ``model_config._delegate_from == parent_session_id``
``compression``  parent ended ``compression`` and the child is not a fork
                 (a delegate's continuation inherits ``_delegate_from`` from
                 its parent, so the marker only counts when it names the
                 child's own parent)
``reset``        ``model_config._reset_from == parent_session_id`` (``/new``
                 AND idle/daily auto-reset stamp it)
``branch``       ``model_config._branched_from == parent_session_id``

Step 1 normalises the pin to its root over delegation and compression edges
only. Step 2 decides whether the route's current session reaches that root,
walking backwards and crossing a reset edge only when the predecessor ended
for an allowlisted non-boundary reason. Absence of ancestry never proves
ownership.
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# End reasons that mean the USER deliberately closed this thread of work
# (/new -> session_reset / new_session, an explicit exit, or a /switch).
USER_BOUNDARY_END_REASONS = (
    "session_reset",
    "user_exit",
    "session_switch",
    "new_session",
)
# The only end reasons a reset edge may carry for the route to still own the
# predecessor's background work (automatic idle/daily succession, a cleanup
# close, a compression rotation). Anything else — a user boundary, an unknown
# reason, NULL — fails closed (Fable r3 C1).
NON_BOUNDARY_END_REASONS = frozenset({"idle", "daily", "agent_close", "compression"})
# Reasons for an accidental close that are recoverable ONLY when the pin is the
# route's own current session (the route lookup reopens it). Not valid on a
# reset path: a predecessor closed this way proves nothing about succession.
RECOVERABLE_CURRENT_END_REASONS = frozenset({"ws_orphan_reap"})
MAX_HOPS = 100

# ``Route.index_session_id`` value meaning "the topic binding could not be read":
# never equal to a real session id, so the verdict is ``retry``.
BINDING_UNAVAILABLE = "<binding-unavailable>"

DELIVER = "deliver"
UNOWNED = "unowned"
RETRY = "retry"


class Route(NamedTuple):
    """The route a result is being delivered to; the caller supplies it.

    The first six fields are the frozen interface. ``index_session_id`` is the
    session index's session for the route when it can differ from
    ``current_session_id`` (a Telegram topic lane passes its tip-walked
    binding as ``current_session_id``); disagreement is a ``retry``.
    """

    profile: Optional[str]
    session_key: str
    current_session_id: str
    platform: str
    chat_id: str
    thread_id: Optional[str]
    index_session_id: Optional[str] = None
    topic_lane: bool = False


@dataclass(frozen=True)
class Resolution:
    """Canonical owner plus the delivery verdict for one route.

    ``owner_root_id`` is the root of the pin's typed lineage and is returned
    whatever the verdict (P3 stores it on the inbox record). ``owner_tip_id``
    is where the result lands when ``verdict == "deliver"`` — the route's
    current session — and the root lineage's compression tip otherwise.
    """

    owner_root_id: Optional[str]
    owner_tip_id: Optional[str]
    verdict: str
    reason: str


def _model_config(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row.get("model_config")
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str):
        return {}
    try:
        cfg = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def edge_kind(child: Dict[str, Any], parent: Dict[str, Any]) -> str:
    """Type the ``child -> parent`` edge: branch, delegation, tool, reset,
    compression or untyped. Markers only count when they name *this* parent.
    """
    parent_id = child.get("parent_session_id")
    cfg = _model_config(child)
    if cfg.get("_branched_from") == parent_id:
        return "branch"
    if cfg.get("_delegate_from") == parent_id:
        return "delegation"
    if child.get("source") == "tool":
        return "tool"
    if cfg.get("_reset_from") == parent_id:
        return "reset"
    if parent.get("end_reason") == "compression":
        return "compression"
    return "untyped"


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class _Retry(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Walker:
    def __init__(self, db: Any):
        self._db = db
        self._rows: Dict[str, Optional[Dict[str, Any]]] = {}

    async def row(self, session_id: str) -> Dict[str, Any]:
        if session_id not in self._rows:
            try:
                self._rows[session_id] = await _await(self._db.get_session(session_id))
            except Exception:
                logger.debug("ownership: session lookup failed for %s", session_id, exc_info=True)
                raise _Retry(f"lookup_failed:{session_id}")
        row = self._rows[session_id]
        if row is None:
            raise _Retry(f"missing_session:{session_id}")
        return row

    async def children(self, session_id: str) -> List[Dict[str, Any]]:
        try:
            return list(await _await(self._db.get_child_sessions(session_id)) or [])
        except Exception:
            logger.debug("ownership: child lookup failed for %s", session_id, exc_info=True)
            raise _Retry(f"lookup_failed:{session_id}")

    async def root_of(self, pin_id: str) -> str:
        """Step 1: climb delegation and compression edges only."""
        current = pin_id
        seen: Set[str] = {current}
        for _ in range(MAX_HOPS):
            row = await self.row(current)
            parent_id = row.get("parent_session_id")
            if not parent_id:
                return current
            parent = await self.row(parent_id)
            if edge_kind(row, parent) not in ("delegation", "compression"):
                return current
            if parent_id in seen:
                raise _Retry("cycle")
            seen.add(parent_id)
            current = parent_id
        raise _Retry("hop_limit")

    async def lineage(self, root_id: str) -> Tuple[List[str], str]:
        """Root's compression chain (bound-aware) and its tip id."""
        chain = [root_id]
        seen = {root_id}
        current = root_id
        for _ in range(MAX_HOPS):
            row = await self.row(current)
            if row.get("end_reason") != "compression":
                return chain, current
            continuations = [
                c for c in await self.children(current)
                if edge_kind(c, row) == "compression"
            ]
            if not continuations:
                # Rotation caught mid-flight: the continuation is not visible yet.
                raise _Retry("compression_continuation_missing")

            def _rank(c: Dict[str, Any]) -> Tuple[int, float]:
                state = 0 if c.get("end_reason") == "compression" else (
                    1 if not c.get("ended_at") else 2
                )
                return state, -float(c.get("started_at") or 0.0)

            nxt = sorted(continuations, key=_rank)[0]["id"]
            if nxt in seen:
                raise _Retry("cycle")
            seen.add(nxt)
            chain.append(nxt)
            current = nxt
        raise _Retry("hop_limit")


def _as_route(route: Any) -> Route:
    if isinstance(route, Route):
        return route
    return Route(*tuple(route))


async def resolve_owner(
    pin_id: str,
    route: Any,
    *,
    db: Any = None,
) -> Resolution:
    """Resolve *pin_id* against *route*; never mutates a route or a session.

    ``db`` is anything with ``get_session`` / ``get_child_sessions`` (sync or
    awaitable) — the gateway's ``AsyncSessionDB``. When omitted, a ``SessionDB``
    for the current ``CLOVER_HOME`` is opened. Missing rows, lookup errors,
    cycles and runaway chains are ``retry``.
    """
    route = _as_route(route)
    if db is None:
        from clover_state import SessionDB

        db = SessionDB()
    walker = _Walker(db)
    root_id: Optional[str] = None
    tip_id: Optional[str] = None
    try:
        if not pin_id:
            raise _Retry("missing_pin")
        root_id = await walker.root_of(pin_id)
        chain, tip_id = await walker.lineage(root_id)
        return await _decide(walker, route, root_id, set(chain), tip_id)
    except _Retry as exc:
        return Resolution(root_id, tip_id or root_id, RETRY, exc.reason)


async def _decide(
    walker: _Walker,
    route: Route,
    root_id: str,
    chain: Set[str],
    tip_id: str,
) -> Resolution:
    def _res(verdict: str, reason: str, tip: Optional[str] = None) -> Resolution:
        return Resolution(root_id, tip or tip_id, verdict, reason)

    current_id = route.current_session_id
    if not current_id:
        # No index entry: the pipeline's route lookup may still recover the
        # live session (``_query_recoverable_session``), so this is uncertainty,
        # not proof the pin is foreign.
        return _res(RETRY, "no_route_session")
    if route.index_session_id and route.index_session_id != current_id:
        # Topic lane: the binding decides where a human lands, and the index
        # has not caught up (e.g. after an idle auto-reset). Wait, don't guess.
        return _res(RETRY, "binding_index_disagree")

    current = await walker.row(current_id)
    # Topic lane whose bound session was closed by the expiry watcher
    # (``set_expiry_finalized`` promotes it to ``session_reset``): the index
    # entry still points at it (a manual /new moves the index to the new
    # session), the durable ``expiry_finalized`` flag is set, and the human path
    # switches the lane back to it and reopens it. A manual /new has neither.
    expiry_closed = bool(
        route.topic_lane
        and route.index_session_id == current_id
        and current.get("end_reason") == "session_reset"
        and current.get("expiry_finalized")
    )
    if current.get("end_reason") in USER_BOUNDARY_END_REASONS and not expiry_closed:
        return _res(UNOWNED, "route_session_closed")

    # Walk the route's current session back toward the root.
    cursor_id = current_id
    cursor = current
    crossed_reset = False
    seen: Set[str] = {cursor_id}
    for _ in range(MAX_HOPS):
        if cursor_id in chain:
            break
        parent_id = cursor.get("parent_session_id")
        if not parent_id:
            return _res(UNOWNED, await _off_path_reason(walker, tip_id))
        parent = await walker.row(parent_id)
        kind = edge_kind(cursor, parent)
        if kind == "compression":
            pass
        elif kind == "reset":
            reason = parent.get("end_reason")
            if reason not in NON_BOUNDARY_END_REASONS:
                label = "user_boundary" if reason in USER_BOUNDARY_END_REASONS else "unknown_end_reason"
                return _res(UNOWNED, f"{label}:{reason or 'NULL'}")
            crossed_reset = True
        else:
            return _res(UNOWNED, f"{kind}_edge")
        if parent_id in seen:
            raise _Retry("cycle")
        seen.add(parent_id)
        cursor_id, cursor = parent_id, parent
    else:
        raise _Retry("hop_limit")

    if crossed_reset:
        # Root reached through allowlisted automatic resets: deliver to the
        # route's own current session.
        return _res(DELIVER, "reset_successor", current_id)

    # The route's current session is in the root's compression lineage.
    tip = await walker.row(tip_id)
    tip_reason = tip.get("end_reason")
    if not tip.get("ended_at"):
        return _res(DELIVER, "live", tip_id)
    if current_id == tip_id:
        if route.topic_lane and tip_reason in ("idle", "daily"):
            # Topic lane: a human message there is switched back to the bound
            # session (``switch_session`` in ``_route_event_session``, which
            # also reopens it), so the idle/daily-ended bound session is where
            # this result belongs. Nowhere else may an ended session receive
            # delivery.
            return _res(DELIVER, "topic_lane_bound", tip_id)
        if expiry_closed and tip_id == current_id:
            return _res(DELIVER, "topic_lane_expired", tip_id)
        if tip_reason in RECOVERABLE_CURRENT_END_REASONS:
            # Accidentally closed (e.g. a mistaken websocket-orphan reap) and
            # still the route's own session: the route lookup reopens it.
            return _res(DELIVER, "recoverable_end", tip_id)
    if tip_reason in USER_BOUNDARY_END_REASONS:
        return _res(UNOWNED, f"user_boundary:{tip_reason}")
    if tip_reason in NON_BOUNDARY_END_REASONS:
        # Ended for an automatic reason but no reset successor exists yet (the
        # next route lookup creates it). Delivering into the ended row would
        # run a turn in a closed session; wait for the verified successor.
        return _res(RETRY, "owner_ended_no_successor")
    # Anything else is unowned on purpose: ``suspended`` and
    # ``resume_pending_expired`` are store-produced auto-reset reasons that are
    # NOT allowlisted — fail closed (the drop is recorded and noticed) rather
    # than guess whether the user meant to close that conversation.
    return _res(UNOWNED, f"unknown_end_reason:{tip_reason or 'NULL'}")


async def _off_path_reason(walker: _Walker, tip_id: str) -> str:
    tip = await walker.row(tip_id)
    return "owned_elsewhere" if not tip.get("ended_at") else "not_on_path"


def canonical_profile(profile: Optional[str]) -> str:
    """Profile label stamped on inbox records: ``None``/``""``/``"default"`` -> ``"default"``.

    The label is the serving profile of the SOURCE the event arrived on, never
    the process's active profile, so writers and readers agree per route.
    """
    return (str(profile).strip() if profile else "") or "default"


async def route_path_session_ids(route: Any, *, db: Any = None) -> List[str]:
    """Session ids on the route's current session's ancestry path.

    Walks backwards from ``current_session_id`` over compression and reset
    edges only (never branch / delegation / untyped), so it names every
    conversation whose results this route might be entitled to see. It grants
    nothing by itself: callers still run :func:`resolve_owner` per owner.
    """
    route = _as_route(route)
    if db is None:
        from clover_state import SessionDB

        db = SessionDB()
    walker = _Walker(db)
    ids: List[str] = []
    cursor_id = route.current_session_id
    try:
        for _ in range(MAX_HOPS):
            if not cursor_id or cursor_id in ids:
                break
            ids.append(cursor_id)
            cursor = await walker.row(cursor_id)
            parent_id = cursor.get("parent_session_id")
            if not parent_id:
                break
            parent = await walker.row(parent_id)
            if edge_kind(cursor, parent) not in ("compression", "reset"):
                break
            cursor_id = parent_id
    except _Retry:
        pass
    return ids


# ---------------------------------------------------------------------------
# Owner normalisation and route recovery (used by the writers of dropped
# records, the notice sender and /results)
# ---------------------------------------------------------------------------
def resolve_root_sync(db: Any, pin_id: str) -> Optional[str]:
    """Root of *pin_id* over delegation/compression edges (sync DB), or ``None``
    when it cannot be resolved (missing row, lookup error, cycle, >100 hops).

    The same typed climb as :func:`resolve_owner` step 1, for callers that hold
    a synchronous ``SessionDB`` and no event loop (the delegation ledger).
    """
    if not pin_id:
        return None
    try:
        current = pin_id
        seen = {current}
        for _ in range(MAX_HOPS):
            row = db.get_session(current)
            if row is None:
                return None
            parent_id = row.get("parent_session_id")
            if not parent_id:
                return current
            parent = db.get_session(parent_id)
            if parent is None:
                return None
            if edge_kind(row, parent) not in ("delegation", "compression"):
                return current
            if parent_id in seen:
                return None
            seen.add(parent_id)
            current = parent_id
    except Exception:
        logger.debug("ownership: sync root lookup failed for %s", pin_id, exc_info=True)
    return None


async def resolve_root(pin_id: str, *, db: Any) -> Optional[str]:
    """Async twin of :func:`resolve_root_sync` (``None`` = unresolved)."""
    if not pin_id:
        return None
    try:
        return await _Walker(db).root_of(pin_id)
    except _Retry:
        return None


def route_from_session_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Route (profile / platform / chat / thread / session_key) persisted on a
    gateway session row, or ``None`` when the session has no chat route.

    Read straight from the ``sessions`` columns written by the gateway
    (``source``, ``chat_id``, ``thread_id``, ``session_key``) and the profile
    from its persisted origin — never parsed out of a session key.
    """
    if not row:
        return None
    platform, chat_id = row.get("source"), row.get("chat_id")
    if not platform or not chat_id:
        return None
    profile = row.get("profile_name")
    raw_origin = row.get("origin_json")
    if raw_origin:
        try:
            origin = json.loads(raw_origin) if isinstance(raw_origin, str) else raw_origin
            if isinstance(origin, dict) and origin.get("profile"):
                profile = origin["profile"]
        except (TypeError, ValueError):
            pass
    return {
        "profile": canonical_profile(profile),
        "platform": str(platform),
        "chat_id": str(chat_id),
        "thread_id": str(row["thread_id"]) if row.get("thread_id") else None,
        "session_key": row.get("session_key") or None,
    }
