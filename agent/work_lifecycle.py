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
  turn) is never armed again, so there is no acknowledge/dispatch loop.  A
  completion turn the router already armed (a staged plan's next stage was just
  started) keeps the router's arming: the hand-back never replaces it.  Such a
  dispatch remembers the work it integrates, so its completion still answers
  for an original helper that failed or is still running: integration success
  alone never clears either.

Transport ``completed`` is untouched: cron and the gateway read ``False`` as a
failed run.  ``work_status`` is the truth about the work.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent import foreground_time_limit as _fl
from agent import router_backstop
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
STOPPED = "stopped"  # stopped on purpose (Stop / the parent took over): not a failure

_RUNNING_STATES = frozenset({"running", "finalizing", "stalling", ""})
_CLEAN_STATUSES = frozenset({"completed", "success"})
_FAILED_EXITS = frozenset({"max_iterations", "error", "timeout", "failed"})
_STOPPED_WORDS = frozenset({"interrupted", "cancelled", "canceled", "stopped"})

# Whole-reply markers meaning "send nothing" (same set as
# gateway.response_filters.LIVE_GATEWAY_SILENT_MARKERS; agent/ never imports
# gateway/).  No footer is ever glued onto one: it would break the silence.
_SILENT_REPLIES = frozenset({"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"})


def is_silent_reply(text: Any) -> bool:
    return " ".join(str(text or "").strip().upper().split()) in _SILENT_REPLIES
_GOAL_CHARS = 80
_LISTED_GOALS = 4

# Per-instance state (agent attributes, like the receipts themselves).
_FLOOR = "_wl_floor"
_COMPLETION_IDS = "_wl_completion_ids"
_HANDBACK_IDS = "_wl_handback_dispatches"  # dispatch id -> receipts it integrates

HANDBACK_GOAL = (
    "Finish checking the helper results (rerun or fix whatever did not come "
    "back right) and report the outcome."
)
# A completion turn may run this many direct tool calls to check what came
# back before the rest goes to a helper.  The router's default (2) is meant
# for a fresh request, and is too tight for "look at what the helpers
# produced".
HANDBACK_CHECK_CALLS = 6
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
    if (
        str(entry.get("exit_reason") or "") in _STOPPED_WORDS
        or str(entry.get("status") or "") in _STOPPED_WORDS
    ):
        return STOPPED
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
    state = str(row.get("state") or "")
    if state in _STOPPED_WORDS:
        return STOPPED
    if state != "completed":
        return BAD
    result = row.get("result")
    return classify_result(result) if isinstance(result, dict) else OK


def _slot_goal(goals: Sequence[str], index: int) -> str:
    from agent.delegation_router import clean_text

    if 0 <= index < len(goals) and goals[index]:
        return clean_text(goals[index], _GOAL_CHARS)
    return "a subagent task"


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
    if any(s.outcome in (BAD, STOPPED) for s in slots):
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


def build_report(
    receipts: Sequence["_fl.DispatchReceipt"], carried: Sequence[Slot] = (),
) -> Optional[WorkReport]:
    """``carried`` are unresolved slots of the work these receipts integrate."""
    if not receipts:
        return None
    try:
        slots: List[Slot] = []
        for r in receipts:
            slots.extend(slots_for(r.delegation_id, r.goals))
        slots.extend(carried)
        status = _aggregate(slots, [r.delegation_id for r in receipts])
        return WorkReport(tuple(slots), status)
    except Exception:
        logger.debug("work report unavailable", exc_info=True)
        return WorkReport((), WORK_DISPATCH_ACK)


# ── typed unfinished-work projection (opt-in workstream ledger) ────────────

# A helper's clean result is only a delivery fact.  The parent must explicitly
# verify the artifact before work can be marked complete; consumption alone is
# not acceptance evidence.
_OUTCOME_STATUS = {OK: "unverified", BAD: "failed", PENDING: "unverified", STOPPED: "stopped"}


def unfinished_outcomes(receipts: Sequence["_fl.DispatchReceipt"], *, session_id: str = "") -> List[Any]:
    """Project dispatch receipts onto ``agent.workstreams.TypedOutcome`` rows.

    A clean helper result remains ``unverified`` even once it has been claimed:
    consumption proves delivery, not independent acceptance.  Failed, still-running
    and stopped helpers stay open work.  Never raises: an unreadable ledger
    projects nothing.
    """
    try:
        from agent.workstreams import TypedOutcome
        out: List[Any] = []
        for r in receipts:
            slots = slots_for(r.delegation_id, r.goals)
            for i, slot in enumerate(slots):
                status = _OUTCOME_STATUS.get(slot.outcome, "unverified")
                out.append(TypedOutcome(
                    deliverable=slot.goal,
                    status=status,
                    source_kind="delegation",
                    source_id=r.delegation_id if len(slots) == 1 else f"{r.delegation_id}#{i}",
                    summary=(f"{slot.goal}: result received; acceptance unverified"
                             if slot.outcome == OK else f"{slot.goal}: {status}"),
                    session_id=session_id,
                ))
        return out
    except Exception:
        logger.debug("unfinished-work projection unavailable", exc_info=True)
        return []


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


def _turn_receipts(agent: Any, floor: int) -> List["_fl.DispatchReceipt"]:
    """The helpers accepted for this turn.  When the router recorded the turn
    its request-scoped view is the answer (a late receipt of an older request
    is not this turn's); otherwise the instance's receipts above ``floor``."""
    if router_backstop.turn_receipt_floor(agent) is not None:
        return router_backstop.turn_receipts(agent)
    return _fl.own_dispatch_receipts(agent, after=floor)


def _lineage(agent: Any) -> Dict[str, Tuple["_fl.DispatchReceipt", ...]]:
    sources = getattr(agent, _HANDBACK_IDS, None)
    if not isinstance(sources, dict):
        sources = {}
        try:
            setattr(agent, _HANDBACK_IDS, sources)
        except Exception:
            pass
    return sources


def _carried(agent: Any, receipts: Sequence["_fl.DispatchReceipt"]) -> List[Slot]:
    """Slots of the original work that ``receipts`` integrate and that are still
    failed or running, read live from the ledger.  A finished clean original
    carries nothing; a hand-back's own clean result clears neither a failure nor
    a pending sibling."""
    lineage = _lineage(agent)
    seen = {r.delegation_id for r in receipts}
    queue = [r.delegation_id for r in receipts]
    carried: List[Slot] = []
    while queue:
        for source in lineage.get(queue.pop(), ()):
            if source.delegation_id in seen:
                continue
            seen.add(source.delegation_id)
            queue.append(source.delegation_id)
            carried.extend(
                s for s in slots_for(source.delegation_id, source.goals) if s.outcome != OK
            )
    return carried


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
    turn = _turn_receipts(agent, floor)
    if turn:
        return build_report(turn, _carried(agent, turn))
    ids = _completion_ids(agent)
    if ids:
        group = completion_group(agent, ids)
        return build_report(group, _carried(agent, group))
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
    stopped = work.goals(STOPPED)
    parts: List[str] = []
    if bad:
        parts.append(
            f"{len(bad)} of {total} subagent task{'s' if total != 1 else ''} did not "
            f"finish properly: {_listed(bad)}. Treat that part as not done."
        )
    if stopped:
        parts.append(
            f"{len(stopped)} of {total} subagent task{'s' if total != 1 else ''} "
            f"{'was' if len(stopped) == 1 else 'were'} stopped: {_listed(stopped)}."
        )
    if pending:
        parts.append(
            f"{len(pending)} of {total} subagent task{'s' if total != 1 else ''} "
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
    stopped = work.goals(STOPPED)
    if stopped:
        lines.append(f"Stopped before finishing (not a failure): {_listed(stopped)}.")
    lines.append(
        "Report only what has come back, say plainly what is still running or "
        "not done, and do not give a final overall conclusion over it."
    )
    return runtime_note(" ".join(lines))


# ── turn entry / exit ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class LocalAuditGoal:
    """Pre-execution specification, not model-written completion or authority.

    Bounded positive path: the entire human request is to verify a named local
    SHA-256 manifest. Extra deliverables (e.g. packaging) are not inferred away.
    The original manifest is frozen before tools run so rewriting a checksum
    file cannot manufacture acceptance. No commands are executed here.
    """
    message: str
    manifest: str
    digest: str
    entries: Tuple[Tuple[str, str], ...]
    event_id: str


def _audit_bytes(project: str, name: str, limit: int) -> bytes:
    """Bounded regular-file read, no symlinks (including parent components)."""
    import os
    import stat
    from pathlib import PurePosixPath

    parts = PurePosixPath(name).parts
    if not parts or tuple(name.split("/")) != parts or any(p in (".", "..", "/") for p in parts):
        raise ValueError("nonlocal audit path")
    # Fail closed on hosts lacking directory-relative, no-follow opens.
    fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(child, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise ValueError("audit file is not bounded regular data")
            value = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if len(value) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("audit file changed during read")
            return value
    finally:
        os.close(fd)


def prepare_local_audit(agent: Any, message: Any, scope: Any) -> Optional[LocalAuditGoal]:
    """Existing host turn entry captures the supported outcome before tools."""
    import hashlib
    import re
    from agent import workstream_origin as origin
    from tools.terminal_tool import _get_env_config

    if not isinstance(message, str):
        return None
    match = re.fullmatch(r"(?:Please )?[Vv]erify SHA-256 manifest ([A-Za-z0-9][A-Za-z0-9_./-]*)", message.strip())
    ingress = getattr(agent, "_trusted_human_ingress", None)
    if (not match or ingress is None or not scope.private or not origin.ingress_is_valid(ingress, profile=scope.profile)
            or ingress.session_id != str(getattr(agent, "session_id", ""))
            or _get_env_config().get("env_type") != "local"):
        return None
    try:
        name = match[1]
        content = _audit_bytes(scope.project, name, 8192)
        entries = []
        for line in content.decode("ascii").splitlines():
            entry = re.fullmatch(r"([0-9a-f]{64}) [ *]([A-Za-z0-9][A-Za-z0-9_./-]*)", line)
            if not entry:
                return None
            # Entries are relative to the project, matching sha256sum -c.
            entries.append((entry[2], entry[1]))
        if not 1 <= len(entries) <= 32 or len({p for p, _ in entries}) != len(entries):
            return None
        return LocalAuditGoal(message, name, hashlib.sha256(content).hexdigest(), tuple(entries), ingress.event_id)
    except (OSError, ValueError):
        return None


def verify_local_audit(agent: Any, goal: Optional[LocalAuditGoal], scope: Any,
                       row: dict, steps: str, result: dict) -> Optional[dict]:
    """Actual outcome owner callback. Shell success is necessary, not sufficient.

    Independently reads every requested artifact against the pre-tool manifest;
    requires an actual foreground check, original current human binding, a
    non-silent clean turn and no unresolved helper work. No save/delivery claim.
    """
    import hashlib
    from agent.workstream_human_hooks import _authenticate

    if goal is None or is_silent_reply(result.get("final_response")):
        return None
    if (not str(result.get("final_response") or "").strip()
            or any(result.get(k) for k in ("failed", "interrupted", "partial", "error", "cleanup_errors"))
            or result.get("work_status") not in (None, WORK_VERIFIED)
            or getattr(agent, "_interrupt_requested", False)):
        return None
    authenticated = _authenticate(agent, role="user", message=goal.message,
        session_id=agent.session_id, message_id=None, row_id=row["id"])
    if not authenticated or authenticated[2].ingress_event_id != goal.event_id:
        return None
    if f"sha256sum -c {goal.manifest}" not in [s.partition(". ")[2] for s in steps.splitlines()]:
        return None
    try:
        if hashlib.sha256(_audit_bytes(scope.project, goal.manifest, 8192)).hexdigest() != goal.digest:
            return None
        remaining = 4 * 1024 * 1024
        for name, expected in goal.entries:
            value = _audit_bytes(scope.project, name, remaining)
            remaining -= len(value)
            if hashlib.sha256(value).hexdigest() != expected:
                return None
        # Read the original again after artifact verification, then fence Stop.
        if hashlib.sha256(_audit_bytes(scope.project, goal.manifest, 8192)).hexdigest() != goal.digest:
            return None
        from agent.workstream_origin import ingress_is_valid
        if not ingress_is_valid(authenticated[3], profile=scope.profile):
            return None
        return {"v": 1, "owner": "local_sha256_audit", "goal": hashlib.sha256(goal.message.encode()).hexdigest(),
                "manifest": goal.digest, "count": len(goal.entries), "event": goal.event_id, "row": row["id"]}
    except (OSError, ValueError):
        return None


def _arm_handback(agent: Any, message: str, group: Sequence["_fl.DispatchReceipt"]) -> bool:
    """Arm the router backstop with the one-task integration plan."""
    from agent import delegation_router as dr

    if any(r.delegation_id in _lineage(agent) for r in group):
        return False  # the result of a hand-back is final here: no second one
    if router_backstop.turn_is_armed(agent):
        # The router already armed this turn (a staged plan's next stage was
        # just started: hand-off, no spawn, a receipt floor below it).  Arming
        # again would replace that and let the root run beside the stage.
        return False
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
    # The count is the "short check" allowance; the completion turn's own
    # foreground clock bounds it in time, so the time trigger is left to it.
    router_backstop.begin(
        agent, verdict, _HANDBACK_REQUEST_LEAD + digest,
        dataclasses.replace(
            settings, backstop_seconds=None,
            backstop_calls=max(settings.backstop_calls, HANDBACK_CHECK_CALLS),
        ),
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
        # The router has already recorded where this turn starts (its public
        # floor); a turn it did not record starts at the current sequence.
        floor = router_backstop.turn_receipt_floor(agent)
        setattr(agent, _FLOOR, floor if floor is not None else _fl.dispatch_seq(agent))
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
        return status_note(build_report(group, _carried(agent, group))) if may_note else ""
    except Exception:
        logger.debug("helper-work lifecycle unavailable", exc_info=True)
        return ""


# Optional explicit host envelope for the opt-in workstream ledger: an
# ``agent.workstreams.Scope`` set on the instance.  When absent the canonical
# ``workstreams.scope_for_agent`` derivation is used; a non-``Scope`` value is
# ignored.  With the feature disabled this module touches no storage.
_SCOPE_ATTR = "_workstream_scope"


def _record_unfinished(agent: Any) -> None:
    """Persist this turn's workstream facts for the opt-in ledger.

    * unfinished/failed helper work, restated from the durable delegation
      ledger (the same receipts ``report`` answers for);
    * an explicit user correction, read back from the SessionDB's current
      ``user`` row (never on a helper-completion turn, whose user row is
      synthetic).

    Best-effort and never raises; inert unless ``proactive.enabled``.
    """
    try:
        from agent import workstreams as _ws

        settings = _ws.load_settings()
        if not settings.enabled:
            return
        scope = getattr(agent, _SCOPE_ATTR, None)
        if scope is None:
            # No explicit host envelope: the same canonical derivation the
            # turn-context seam uses, so both read and write one scope.
            scope = _ws.scope_for_agent(agent)
        elif not isinstance(scope, _ws.Scope):
            return  # a foreign object is never a scope
        ids = _completion_ids(agent)
        session_id = str(getattr(agent, "session_id", "") or "")
        if not ids and session_id:
            try:
                _ws.capture_user_correction(
                    scope, session_id=session_id, settings=settings,
                    db=getattr(agent, "_session_db", None) or None,
                )
            except Exception:
                logger.debug("user correction not recorded", exc_info=True)
        floor = getattr(agent, _FLOOR, None)
        if not isinstance(floor, int) or isinstance(floor, bool):
            return
        turn = _turn_receipts(agent, floor)
        group = completion_group(agent, ids) if ids else []
        # A hand-back turn can dispatch new work while also receiving the
        # previous helper's result. Preserve both owner receipts; `turn or
        # group` silently dropped the just-received original completion.
        by_id = {r.delegation_id: r for r in [*group, *turn]}
        receipts = sorted(by_id.values(), key=lambda r: r.seq)
        if receipts:
            _ws.record_outcomes(scope, unfinished_outcomes(receipts, session_id=session_id), settings)
    except Exception:
        logger.debug("workstream recording unavailable", exc_info=True)


def end_turn(agent: Any) -> None:
    """A completion turn that dispatched helpers marks them: their own
    completion is the end of the chain and is never handed back again."""
    _record_unfinished(agent)
    try:
        if not _completion_ids(agent):
            return
        floor = getattr(agent, _FLOOR, None)
        if not isinstance(floor, int):
            return
        made = _turn_receipts(agent, floor)
        if made:
            integrated = tuple(completion_group(agent, _completion_ids(agent)))
            lineage = _lineage(agent)
            for r in made:
                lineage[r.delegation_id] = integrated
    except Exception:
        logger.debug("could not mark hand-back dispatches", exc_info=True)
