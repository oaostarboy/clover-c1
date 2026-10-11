"""What the root may say about helper work, and who finishes integrating it.

Two jobs, both read from the durable ledger and the dispatch receipts the
instance noted -- never from model text:

* **Truthful status.**  ``report`` classifies every helper the turn dispatched,
  or (on a helper-completion turn) the helpers of the request that completion
  belongs to.  A batch row that is ``completed`` while one of its children
  failed, was truncated at its iteration cap, or never reported is *not* a
  success: pending helpers make the status ``partial``, a failed or truncated
  one makes it ``incomplete``, and only a clean result that a consumer has read
  is ``verified_completion``.  The finalizer stamps it on the result and, when
  the work is not fully verified, appends one plain line to the reply so the
  user never reads a combined verdict over work that is still running or did
  not finish (same pattern as the file-mutation footer).  The completion turn
  also gets the same facts as a ``[Clover runtime]`` note on the API copy of
  its user message, so the root can scope what it says.

* **Hand-back of substantial integration.**  A helper-completion turn of a
  dispatch this instance made arms the router backstop with a one-task plan
  ("put the results together and check them").  A short check stays direct;
  once the root has made ``delegation.router.backstop_calls`` work calls the
  runtime itself dispatches that task to a helper and ends the turn.  The
  completion turn of such a hand-back (or of any dispatch made on a completion
  turn) is never armed again, so there is no acknowledge/dispatch loop.

Transport ``completed`` is untouched: cron and the gateway read ``False`` as a
failed run.  ``work_status`` is the truth about the work.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent import foreground_time_limit as _fl
from agent.router_backstop import (
    EXIT_REASON,
    WORK_DISPATCH_ACK,
    WORK_INCOMPLETE,
    WORK_PARTIAL,
    WORK_VERIFIED,
)
from agent.turn_notices import runtime_note

logger = logging.getLogger(__name__)

OK = "ok"
PENDING = "pending"
BAD = "bad"

_RUNNING_STATES = frozenset({"running", "finalizing", "stalling", ""})
_CLEAN_STATUSES = frozenset({"completed", "success"})
_FAILED_EXITS = frozenset({"max_iterations", "interrupted", "error", "timeout", "failed"})
_GOAL_CHARS = 80
_LISTED_GOALS = 4

# Per-instance state (agent attributes, like the receipts themselves).
_FLOOR = "_wl_floor"
_COMPLETION_IDS = "_wl_completion_ids"
_HANDBACK_IDS = "_wl_handback_dispatches"

HANDBACK_GOAL = (
    "Put the finished helper results together, check them, and write the "
    "final report for the user."
)
HANDBACK_ACCEPTANCE = (
    "Every statement in the report is backed by a helper result or a check "
    "you ran, and anything unfinished is named."
)
_HANDBACK_REASON = "the helpers' results need more than a short check"
_HANDBACK_REQUEST_LEAD = (
    "Results that came back from helpers dispatched for the user's earlier "
    "request (the conversation continues in the chat):\n"
)


@dataclass(frozen=True)
class Slot:
    """One helper task and what the ledger says about it."""

    goal: str
    outcome: str


@dataclass(frozen=True)
class WorkReport:
    slots: Tuple[Slot, ...]
    status: str

    def goals(self, outcome: str) -> List[str]:
        return [s.goal for s in self.slots if s.outcome == outcome]


# ── classifying durable rows ───────────────────────────────────────────────

def classify_result(entry: Any) -> str:
    """``ok`` or ``bad`` for one terminal result dict (single dispatch or one
    child of a batch).  Truncation and a non-success exit are not success even
    when the row says ``completed``: a child stopped at its iteration cap still
    returns a summary, and so does one whose turn was cut off by a refusal."""
    if not isinstance(entry, dict):
        return BAD
    if entry.get("incomplete") or entry.get("truncated"):
        return BAD
    if str(entry.get("exit_reason") or "") in _FAILED_EXITS:
        return BAD
    status = str(entry.get("status") or "")
    if status and status not in _CLEAN_STATUSES:
        return BAD
    return OK


def _row(delegation_id: str) -> Optional[Dict[str, Any]]:
    try:
        from tools.async_delegation import get_durable_delegation

        return get_durable_delegation(delegation_id)
    except Exception:
        logger.debug("durable row unavailable: %s", delegation_id, exc_info=True)
        return None


def _terminal_outcome(row: Dict[str, Any]) -> str:
    if str(row.get("state") or "") != "completed":
        return BAD
    result = row.get("result")
    return classify_result(result) if isinstance(result, dict) else OK


def _slot_goal(goals: Sequence[str], index: int) -> str:
    from agent.delegation_router import clean_text

    if 0 <= index < len(goals) and goals[index]:
        return clean_text(goals[index], _GOAL_CHARS)
    return "a helper task"


def slots_for(delegation_id: str, goals: Sequence[str] = ()) -> List[Slot]:
    """The slots of one dispatch (a batch has one per task), read from the
    durable batch row, its committed child rows and its combined results."""
    from tools.async_delegation import child_delegation_id

    row = _row(delegation_id)
    count = max(len(goals), 1)

    def child(index: int) -> Optional[Dict[str, Any]]:
        return _row(child_delegation_id(delegation_id, index)) if len(goals) > 0 else None

    def child_outcome(index: int) -> str:
        crow = child(index)
        if crow is None or str(crow.get("state") or "") in _RUNNING_STATES:
            return PENDING
        return _terminal_outcome(crow)

    if row is None or str(row.get("state") or "") in _RUNNING_STATES:
        # Joined fan-out parks each finished child's receipt before the join:
        # a failed one is already a known failure, a missing one is pending.
        return [Slot(_slot_goal(goals, i), child_outcome(i)) for i in range(count)]
    state = str(row.get("state") or "")
    result = row.get("result")
    results = result.get("results") if isinstance(result, dict) else None
    if isinstance(results, list):
        by_index: Dict[int, Any] = {}
        for entry in results:
            index = entry.get("task_index") if isinstance(entry, dict) else None
            if isinstance(index, int):
                by_index.setdefault(index, entry)
        total = max(len(goals), len(by_index), 1)
        slots: List[Slot] = []
        for i in range(total):
            entry = by_index.get(i)
            crow = child(i)
            settled = crow is not None and str(crow.get("state") or "") not in _RUNNING_STATES
            if entry is not None and not entry.get("incomplete"):
                outcome = classify_result(entry)
            elif settled:
                outcome = _terminal_outcome(crow)
            elif state == "unknown":
                # The straggler join: this helper has not reported yet and its
                # result still arrives separately as a late result.
                outcome = PENDING
            else:
                outcome = BAD  # a slot the join has no result for
            slots.append(Slot(_slot_goal(goals, i), outcome))
        return slots
    outcome = _terminal_outcome(row)
    return [Slot(_slot_goal(goals, i), outcome) for i in range(count)]


def _aggregate(slots: Sequence[Slot], ids: Sequence[str]) -> str:
    if any(s.outcome == BAD for s in slots):
        return WORK_INCOMPLETE
    if slots and all(s.outcome == PENDING for s in slots):
        return WORK_DISPATCH_ACK
    if any(s.outcome == PENDING for s in slots):
        return WORK_PARTIAL
    try:
        from tools.async_delegation import owned_receipts_ready

        # A finished result nobody has read is not verification.
        if set(owned_receipts_ready(list(ids))) >= set(ids):
            return WORK_VERIFIED
    except Exception:
        logger.debug("delivery check unavailable", exc_info=True)
    return WORK_PARTIAL


def build_report(receipts: Sequence["_fl.DispatchReceipt"]) -> Optional[WorkReport]:
    if not receipts:
        return None
    try:
        slots: List[Slot] = []
        for r in receipts:
            slots.extend(slots_for(r.delegation_id, r.goals))
        status = _aggregate(slots, [r.delegation_id for r in receipts])
        return WorkReport(tuple(slots), status)
    except Exception:
        logger.debug("work report unavailable", exc_info=True)
        return WorkReport((), WORK_DISPATCH_ACK)


# ── which helpers a turn is answerable for ─────────────────────────────────

def _completion_ids(agent: Any) -> Tuple[str, ...]:
    ids = getattr(agent, _COMPLETION_IDS, ())
    return ids if isinstance(ids, tuple) else ()


def completion_group(agent: Any, completion_ids: Sequence[str]) -> List["_fl.DispatchReceipt"]:
    """The receipts a completion answers for: the dispatches the trusted ids
    name plus the helpers dispatched in the same request turn, so a sibling that
    is still running is not forgotten.  Empty when this instance made none of
    them (another generation, cron, another profile)."""
    from tools.async_delegation import dispatch_id_for

    own = _fl.own_dispatch_receipts(agent)
    by_id = {r.delegation_id: r for r in own}
    named: List[Any] = []
    for raw in completion_ids:
        receipt = by_id.get(dispatch_id_for(str(raw or "").strip()))
        if receipt is not None and receipt not in named:
            named.append(receipt)
    if not named:
        return []
    turns = {r.turn_id for r in named if r.turn_id}
    siblings = [
        r for r in own
        if r not in named and r.turn_id and r.turn_id in turns
    ]
    return sorted(named + siblings, key=lambda r: r.seq)


def report(agent: Any) -> Optional[WorkReport]:
    """What this turn may say about its helpers, or ``None`` when it has none.

    Helpers the turn itself dispatched come first (what the turn accepted);
    otherwise a helper-completion turn answers for the group its trusted ids
    belong to.  A human turn that dispatched nothing has no report, so
    unrelated chat is never annotated.
    """
    floor = getattr(agent, _FLOOR, None)
    if not isinstance(floor, int) or isinstance(floor, bool):
        return None
    turn = _fl.own_dispatch_receipts(agent, after=floor)
    if turn:
        return build_report(turn)
    ids = _completion_ids(agent)
    if ids:
        return build_report(completion_group(agent, ids))
    return None


# ── what is said about it ──────────────────────────────────────────────────

def _listed(goals: Sequence[str]) -> str:
    shown = list(goals[:_LISTED_GOALS])
    text = "; ".join(shown)
    if len(goals) > len(shown):
        text += f" (and {len(goals) - len(shown)} more)"
    return text


def status_footer(work: Optional[WorkReport], exit_reason: Any = "") -> str:
    """One plain line for the user, or ``""`` when the work is clean, only
    acknowledged, or this turn's reply is the runtime's own hand-off."""
    if work is None or str(exit_reason or "") == EXIT_REASON:
        return ""
    if work.status not in (WORK_PARTIAL, WORK_INCOMPLETE):
        return ""
    total = len(work.slots)
    pending = work.goals(PENDING)
    bad = work.goals(BAD)
    parts: List[str] = []
    if bad:
        parts.append(
            f"{len(bad)} of {total} helper task{'s' if total != 1 else ''} did not "
            f"finish properly: {_listed(bad)}. Treat that part as not done."
        )
    if pending:
        parts.append(
            f"{len(pending)} of {total} helper task{'s' if total != 1 else ''} "
            f"still running: {_listed(pending)}. What is said above covers only "
            "what has reported so far."
        )
    return " ".join(parts)


def status_note(work: Optional[WorkReport]) -> str:
    """The model-facing form of the same facts (a ``[Clover runtime]`` note)."""
    if work is None or work.status not in (WORK_PARTIAL, WORK_INCOMPLETE):
        return ""
    total = len(work.slots)
    done = len(work.goals(OK))
    lines = [f"Helper status for this request: {done} of {total} finished cleanly."]
    pending = work.goals(PENDING)
    bad = work.goals(BAD)
    if pending:
        lines.append(f"Still running: {_listed(pending)}.")
    if bad:
        lines.append(f"Did not finish properly: {_listed(bad)}.")
    lines.append(
        "Report only what has come back, say plainly what is still running or "
        "not done, and do not give a final overall conclusion over it."
    )
    return runtime_note(" ".join(lines))


# ── turn entry / exit ──────────────────────────────────────────────────────

def _handback_ids(agent: Any) -> set:
    ids = getattr(agent, _HANDBACK_IDS, None)
    if not isinstance(ids, set):
        ids = set()
        try:
            setattr(agent, _HANDBACK_IDS, ids)
        except Exception:
            pass
    return ids


def _arm_handback(agent: Any, message: str, group: Sequence["_fl.DispatchReceipt"]) -> bool:
    """Arm the router backstop with the one-task integration plan."""
    from agent import delegation_router as dr
    from agent import router_backstop

    if any(r.delegation_id in _handback_ids(agent) for r in group):
        return False  # the result of a hand-back is final here: no second one
    settings = dr.load_settings()
    digest = dr.goal_text(message)
    if dr.skip_reason(agent, digest or "x", None, settings=settings):
        return False
    verdict = dr.Verdict(
        mode="single",
        tasks=(dr.PlanTask(goal=HANDBACK_GOAL, acceptance=HANDBACK_ACCEPTANCE),),
        reason=_HANDBACK_REASON,
        source="lifecycle",
    )
    # The count is the "short check" allowance; the clock is already bounded by
    # the verification episode, so the time trigger is left to it.
    router_backstop.begin(
        agent, verdict, _HANDBACK_REQUEST_LEAD + digest,
        dataclasses.replace(settings, backstop_seconds=None),
    )
    return True


def begin_turn(
    agent: Any,
    message: Any,
    display_kind: Optional[str],
    completion_ids: Optional[Sequence[str]],
    *,
    may_note: bool = True,
) -> str:
    """Once per turn, after the router step.  Returns the model-facing status
    note for the API copy of the user message (``""`` for none).  Never raises."""
    try:
        setattr(agent, _FLOOR, _fl.dispatch_seq(agent))
        ids: Tuple[str, ...] = ()
        if display_kind and isinstance(completion_ids, (list, tuple)):
            ids = tuple(str(i).strip() for i in completion_ids if isinstance(i, str) and i.strip())
        setattr(agent, _COMPLETION_IDS, ids)
        if not ids:
            return ""
        group = completion_group(agent, ids)
        if not group:
            return ""
        text = message if isinstance(message, str) else ""
        if text.strip():
            _arm_handback(agent, text, group)
        return status_note(build_report(group)) if may_note else ""
    except Exception:
        logger.debug("helper-work lifecycle unavailable", exc_info=True)
        return ""


def end_turn(agent: Any) -> None:
    """A completion turn that dispatched helpers marks them: their own
    completion is the end of the chain and is never handed back again."""
    try:
        if not _completion_ids(agent):
            return
        floor = getattr(agent, _FLOOR, None)
        if not isinstance(floor, int):
            return
        made = _fl.own_dispatch_receipts(agent, after=floor)
        if made:
            _handback_ids(agent).update(r.delegation_id for r in made)
    except Exception:
        logger.debug("could not mark hand-back dispatches", exc_info=True)
