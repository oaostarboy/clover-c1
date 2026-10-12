"""Hand the rest of a turn to a helper when the foreground time limit's hard stop hits.

The hard stop (``agent.foreground_time_limit.run_hard_stop``) used to ask the
model for a "where this stands" summary and offer to continue.  When the work
was not finished that stopped the agent mid-task and made the user type
"continue" -- the exact thing the limit exists to avoid.

Now, when the work is NOT finished, the runtime hands the remainder to one
background helper, the same way the router backstop does
(``agent.router_backstop._dispatch``): the full-context notes file, the "known
so far" bullets, the hand-off title and the agent's own one-line opener all
ride along.  The turn ends with that opener (the card follows as usual); the
model is never asked to stop and "offer to continue".

* **Human turn**: the helper gets the user's request plus everything the turn
  did so far.
* **Completion turn** (a helper's results being integrated, ``display_kind``
  ``internal_notification``): the helper gets the completion results and the
  user's original request, and finishes the integration.
* **Never twice.**  A turn that already handed off (the backstop, the root's
  own ``delegate_task``) is acknowledged, not dispatched again; the completion
  of a hand-off this module (or the integration hand-back) made is never handed
  off a second time -- that turn falls back to the plain summary.
* **Fallback** (``None`` returned, the caller asks for the summary): no
  ``delegate_task`` tool, no detached delivery, a pending Stop, the user asked
  for it to be done directly, ``delegation.time_limit_handoff`` off, or the
  dispatch was refused / failed.

Nothing here raises into the turn.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent import foreground_time_limit as _fl

logger = logging.getLogger(__name__)

SOURCE = "time_limit"
_OWN_ATTR = "_tlh_dispatches"
_REQUEST_CHARS = 4000
_COMPLETION_CHARS = 12_000

_GOAL = (
    "Finish the user's request, which the parent started and did not finish. "
    "Do not redo what the notes and findings already cover."
)
_COMPLETION_GOAL = (
    "Finish integrating the helper results the parent was working through for "
    "the user's request: check what came back, fix or redo only what did not "
    "come back right, and report the outcome."
)
_ACCEPTANCE = "the user's request is fully done, and anything left undone is named"
_COMPLETION_LEAD = (
    "Results that came back from helpers dispatched for the user's earlier "
    "request (delivered to the parent this turn):"
)


def enabled() -> bool:
    """``delegation.time_limit_handoff`` (default true)."""
    import os

    if os.environ.get("CLOVER_IGNORE_USER_CONFIG") == "1":
        return True
    try:
        from agent.delegation_router import _bool
        from clover_cli.config import load_config_readonly

        delegation = (load_config_readonly() or {}).get("delegation")
        if not isinstance(delegation, dict):
            return True
        return _bool(delegation.get("time_limit_handoff"), True)
    except Exception:
        logger.debug("time_limit_handoff setting unreadable; using the default", exc_info=True)
        return True


# ── reading the turn ───────────────────────────────────────────────────────

def _text_of(content: Any) -> str:
    from agent.handoff_notes import _text_of as text_of

    return text_of(content)


def _turn_start(messages: Sequence[Dict[str, Any]]) -> int:
    """Index of the user message that opened this turn (the last user row that
    is not the runtime's own hidden scaffolding), or ``-1``."""
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, dict) and msg.get("role") == "user" and msg.get("display_kind") != "hidden":
            return i
    return -1


def _original_request(messages: Sequence[Dict[str, Any]], before: int) -> str:
    """The last human-authored request before index ``before``."""
    from agent.context_compressor import is_user_originated_turn

    for i in range(before - 1, -1, -1):
        msg = messages[i]
        if (
            isinstance(msg, dict) and msg.get("role") == "user" and not msg.get("display_kind")
            and is_user_originated_turn(msg)
        ):
            text = _text_of(msg.get("content")).strip()
            if text:
                return text
    return ""


def _tool_calls(messages: Sequence[Dict[str, Any]], start: int) -> List[Tuple[str, Any, str]]:
    """``(name, args, result)`` for every finished tool call after ``start``."""
    calls: Dict[str, Tuple[str, Any]] = {}
    out: List[Tuple[str, Any, str]] = []
    for msg in messages[start + 1:]:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                raw = fn.get("arguments")
                try:
                    args = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
                except ValueError:
                    args = {"arguments": str(raw)[:600]}
                calls[str(tc.get("id"))] = (str(fn.get("name") or ""), args if isinstance(args, dict) else {})
        elif msg.get("role") == "tool":
            name, args = calls.get(str(msg.get("tool_call_id")), (str(msg.get("tool_name") or ""), {}))
            if name:
                out.append((name, args, _text_of(msg.get("content"))))
    return out


def _runtime_refusal(result: str) -> bool:
    """A call the runtime refused (it ran nothing): not something the turn found."""
    if not result.startswith("{"):
        return False
    try:
        kind = str((json.loads(result) or {}).get("error_type") or "")
    except ValueError:
        return False
    return kind in (_fl.BLOCK_CODE, _fl.ROUTER_HANDOFF_CODE, _fl.ROUTER_HANDOFF_FAILED_CODE)


# ── guards ─────────────────────────────────────────────────────────────────

def _own(agent: Any) -> Dict[str, bool]:
    own = getattr(agent, _OWN_ATTR, None)
    if not isinstance(own, dict):
        own = {}
        try:
            setattr(agent, _OWN_ATTR, own)
        except Exception:
            pass
    return own


def _completion_of_a_handoff(agent: Any) -> bool:
    """Is this turn the completion of a hand-off the runtime already made
    (this module's, or the integration hand-back's)?  Those never go again."""
    try:
        from agent import work_lifecycle as wl
        from tools.async_delegation import dispatch_id_for

        own = _own(agent)
        lineage = wl._lineage(agent)
        for raw in getattr(agent, "_router_completion_ids", ()) or ():
            did = dispatch_id_for(str(raw or "").strip())
            if did and (did in own or did in lineage or _marked(did)):
                return True
    except Exception:
        logger.debug("could not check the completion's lineage", exc_info=True)
    return False


def _marker(delegation_id: str):
    import re
    from agent.handoff_notes import notes_dir

    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", delegation_id or ""):
        return None
    return notes_dir() / f"{delegation_id}.tlh"


def _mark(delegation_id: str) -> None:
    """Remember durably (across a rebuilt agent or a restart) that this
    dispatch is a time-limit hand-off, so its completion never goes again."""
    try:
        path = _marker(delegation_id)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_text("time-limit hand-off\n", encoding="utf-8")
    except Exception:
        logger.debug("could not mark the time-limit hand-off", exc_info=True)


def _marked(delegation_id: str) -> bool:
    try:
        path = _marker(delegation_id)
        return path is not None and path.is_file()
    except Exception:
        return False


def _turn_helpers(agent: Any) -> List["_fl.DispatchReceipt"]:
    """The helpers accepted for THIS turn: the router's request-scoped view when
    it recorded the turn, else the receipts above the lifecycle's floor."""
    if rb_floor(agent) is not None:
        from agent import router_backstop as rb

        return rb.turn_receipts(agent)
    floor = getattr(agent, "_wl_floor", None)
    if isinstance(floor, int) and not isinstance(floor, bool):
        return _fl.own_dispatch_receipts(agent, after=floor)
    return []


def rb_floor(agent: Any) -> Optional[int]:
    from agent import router_backstop as rb

    return rb.turn_receipt_floor(agent)


def _can_hand_off(agent: Any) -> bool:
    if not enabled():
        return False
    if "delegate_task" not in (getattr(agent, "valid_tool_names", None) or ()):
        return False
    if not _fl.detached_delivery_available():
        return False
    if getattr(agent, "_interrupt_requested", False) is True:
        return False  # respect Stop
    return True


# ── the hand-off ───────────────────────────────────────────────────────────

def _build_state(agent: Any, messages: List[Dict[str, Any]]):
    """A router-backstop state describing the turn's remaining work, or
    ``None`` when there is nothing to hand over (or the user said no)."""
    from agent import delegation_router as dr
    from agent import handoff_notes as notes
    from agent import router_backstop as rb

    start = _turn_start(messages)
    if start < 0:
        return None
    opener = messages[start]
    text = _text_of(opener.get("content")).strip()
    kind = opener.get("display_kind")
    completion = ""
    if kind and kind in _fl._internal_kinds():
        completion = text
        request = _original_request(messages, start) or text
    else:
        request = text
    request = dr.goal_text(request)
    if not request.strip():
        return None
    if rb._asked_for_self(dr.Verdict(mode="direct", tasks=()), request):
        return None  # "do it yourself" is never overridden

    settings = dr.load_settings()
    goal = _COMPLETION_GOAL if completion else _GOAL
    one_line = rb._one_line(request, rb._DIRECT_GOAL_REQUEST_CHARS)
    if one_line:
        goal += f" The request: {one_line}"
    verdict = dr.Verdict(
        mode="single", reason="", source=SOURCE,
        tasks=(dr.PlanTask(goal=goal, acceptance=_ACCEPTANCE),),
    )
    state = rb.RouterTurn(
        verdict=verdict, user_message=request[:_REQUEST_CHARS], settings=settings,
        started_at=_fl._now(), receipt_floor=_fl.dispatch_seq(agent),
        identity=rb._capture_identity(agent), planned=False, direct=True,
    )
    if getattr(settings, "handoff_notes", True):
        state.work_log = notes.WorkLog()
        try:
            state.history = notes.recent_turns(messages[:start])
        except Exception:
            logger.debug("could not read the conversation for the notes", exc_info=True)
    if completion:
        clipped = completion if len(completion) <= _COMPLETION_CHARS else completion[:_COMPLETION_CHARS] + " …"
        rb.record_result(state, "helper_results", {"delivered": "this turn"}, f"{_COMPLETION_LEAD}\n{clipped}")
    for name, args, result in _tool_calls(messages, start):
        if _runtime_refusal(result):
            continue
        rb.record_result(state, name, args, result)
    return state


def hand_off(
    agent: Any, messages: List[Dict[str, Any]], *, task_id: str = ""
) -> Optional[str]:
    """Hand the turn's remaining work to one helper.

    Returns the reply that ends the turn (the agent's own opener, or the card
    text) once a helper was ACCEPTED, or ``None`` when the caller must fall
    back to the plain summary.  Never raises.
    """
    try:
        return _hand_off(agent, messages, task_id)
    except Exception:
        logger.warning("time-limit hand-off failed; falling back to the summary", exc_info=True)
        return None


def _hand_off(agent: Any, messages: List[Dict[str, Any]], task_id: str) -> Optional[str]:
    from agent import router_backstop as rb

    if not _can_hand_off(agent):
        return None
    # A helper already owns this turn's remaining work (the backstop's, or the
    # root's own delegate_task): acknowledge it, never dispatch a second one.
    existing = _turn_helpers(agent)
    if existing:
        logger.info("time-limit hand-off: turn already handed off (%d helper(s)); acknowledging", len(existing))
        return rb._ack_text(existing, agent)
    if _completion_of_a_handoff(agent):
        return None
    state = _build_state(agent, messages)
    if state is None:
        return None
    # The hand-off notes' "opener" call reads the live transcript.
    if not getattr(agent, "_session_messages", None):
        try:
            agent._session_messages = messages
        except Exception:
            pass
    info = rb._dispatch(agent, state, task_id)
    if not info.get("ok"):
        logger.warning("time-limit hand-off: dispatch failed: %s", info.get("reason"))
        return None
    did = str(info.get("delegation_id") or "")
    state.handoff = info
    if did:
        _own(agent)[did] = True
        _mark(did)
    # An armed backstop must not hand the same work off again, and must refuse
    # the root's own spawn beside this one.
    armed = rb._state(agent)
    if armed is not None and armed.handoff is None:
        armed.handoff = info
    logger.info(
        "time-limit hand-off: handed off delegation=%s (work_calls=%d)",
        did, len(state.work_log.entries) if state.work_log is not None else 0,
    )
    receipts = _fl.own_dispatch_receipts(agent, after=state.receipt_floor)
    # The agent's own line, written now that the helper is running.
    rb._voice(agent, state, receipts, messages)
    return rb._ack_text(receipts, agent)


def finish_turn(
    agent: Any, messages: List[Dict[str, Any]], *, task_id: str = ""
) -> Optional[Tuple[str, str]]:
    """``(reply, exit_reason)`` for a hard stop that handed the work off, with
    the reply appended to ``messages`` and streamed; ``None`` for the fallback."""
    from agent import router_backstop as rb
    from agent.message_metadata import append_message

    text = hand_off(agent, messages, task_id=task_id)
    if not text:
        return None
    append_message(messages, {"role": "assistant", "content": text})
    try:
        agent._safe_print(f"\n{text}\n")
        callback = getattr(agent, "stream_delta_callback", None)
        if callback:
            callback(text)
            callback(None)
    except Exception:
        logger.debug("could not stream the hand-off reply", exc_info=True)
    return text, rb.EXIT_REASON
