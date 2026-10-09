"""Mandatory delegation checkpoint for conversational root agents.

Before a capable root runs its first *work* tool of a task it must record a
choice with the ``todo`` tool: ``direct`` (do it here, with an operational
reason) or ``delegate`` (with a reason, followed by an actual child dispatch).
This module owns the policy, the per-agent runtime authorization and the
accounting; the executor calls :func:`admit` on its common dispatch funnel.

What this is not: a task classifier. The model still makes the choice and
nothing here judges whether the stated reason is good. It only guarantees that
a choice was made, that a ``delegate`` choice was followed by a real child
start before further work, and that the root's foreground allowance belongs to
one human request: it is never renewed by declaring again, only by a new human
request, and an accepted background dispatch closes the root's heavy work for
that request (the worker owns the remaining phase).

State is private to the root ``AIAgent`` (``agent._delegation_checkpoint``) and
is deliberately separate from ``TodoStore._delegation``, which is cleared when a
plan completes and is restored from history. Runtime authorization is never
restored from history and never inferred from tool output.

Known limits (also in docs/delegation-decisions.md):

* One outer ``execute_code`` call counts as one work tool even when the script
  makes many nested tool calls.
* External gateway adapters that mark an inbound event ``internal`` also keep a
  live decision across that delivery (``internal_notification``); the work
  budget bounds this opt-out.
* Terminal/ACP denials refund their reservation through a private receipt; no
  tool output is ever parsed to decide a refund.
"""

from __future__ import annotations

import contextvars
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

UNDECIDED = "undecided"
DIRECT_AUTHORIZED = "direct_authorized"
SPAWN_REQUIRED = "spawn_required"
DELEGATED_STARTED = "delegated_started"

DECISION_REQUIRED = "delegation_decision_required"
DISPATCH_REQUIRED = "delegation_dispatch_required"
FOREGROUND_EXHAUSTED = "delegation_foreground_exhausted"
HANDOFF_ACTIVE = "delegation_handoff_active"
SPAWN_CLOSED = "delegation_spawn_closed"
INTEGRATION_EXHAUSTED = "delegation_integration_exhausted"

# How much authority a request still has. ``state`` records that a choice was
# made; ``phase`` records how much foreground work that choice may still do.
PHASE_FOREGROUND = "foreground"
PHASE_EXHAUSTED = "exhausted"
PHASE_HANDED_OFF = "handed_off"
# Reported while a completion-integration window is open / spent. A window is an
# overlay for one internal turn; the stored ledger phase underneath is untouched.
PHASE_INTEGRATING = "integrating"
PHASE_CLOSED = "closed"

_CLOSING_CODES = frozenset({
    FOREGROUND_EXHAUSTED, HANDOFF_ACTIVE, SPAWN_CLOSED, INTEGRATION_EXHAUSTED,
})
_MAX_OWNED_HANDOFFS = 64
_MAX_GOAL_CHARS = 160
# The handoff line is a short-horizon estimate, not a promise about total task
# duration.
_MAX_HANDOFF_ETA_MINUTES = 24 * 60


# Card text is shown to the user, so it is normalized rather than validated:
# a model-written handoff with ordinary punctuation (";", "/", ":", quotes) is
# shown as written. Only things that never belong on the card are removed:
# control characters, markdown emphasis/code markers, absolute filesystem
# paths (kept as their last segment) and internal job/child identifiers.
_HANDOFF_WORK_CHARS = 120
_HANDOFF_OUTCOME_CHARS = 180
_GOAL_TITLE_CHARS = 120
_CARD_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]")
_CARD_MARKUP_RE = re.compile(r"[*`]+|_{2,}|~{2,}|^#+\s*|^\s*(?:[-+>]|\d+[.)])\s+")
_CARD_ABS_PATH_RE = re.compile(
    r"(?<![\w:/\\])(?:~|[A-Za-z]:)?[\\/](?:[\w.@-]+[\\/])+([\w.@-]+)[\\/]?"
)
_CARD_ID_RE = re.compile(
    r"\b(?:async|deleg|subagent|child|job)(?:_[\w-]+|-[\w-]*\d[\w-]*)", re.IGNORECASE
)
_CARD_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,;:.!?)])")
_CARD_DANGLING_RE = re.compile(r"(?:^[\s,;:.!?)]+|[\s,;:(]+$)")


def _normalize_card_text(value: Any, limit: int) -> Optional[str]:
    """Lightly normalize model/task text for the handoff card, or ``None``.

    Never rejects text for ordinary punctuation, case or length: overlong text
    is cut at a word boundary and marked with an ellipsis.
    """
    if not isinstance(value, str):
        return None
    lines = [ln.strip() for ln in value.replace("\r", "\n").split("\n")]
    text = next((ln for ln in lines if ln), "")
    text = _CARD_CONTROL_RE.sub(" ", text)
    text = _CARD_MARKUP_RE.sub("", text)
    text = _CARD_ABS_PATH_RE.sub(lambda m: m.group(1), text)
    text = _CARD_ID_RE.sub("", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\s+", " ", text)
    text = _CARD_SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    text = _CARD_DANGLING_RE.sub("", text).strip()
    if not any(ch.isalnum() for ch in text):
        return None
    if len(text) > limit:
        cut = text[: limit - 1]
        space = cut.rfind(" ")
        if space >= limit // 2:
            cut = cut[:space]
        text = cut.rstrip(" ,;:.!?-–(") + "…"
    return text


def _lower_lead(text: str) -> str:
    """Lowercase a capitalized first word for mid-sentence use; keep acronyms."""
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def _goal_title(value: Any) -> Optional[str]:
    """The task goal as a clean one-line card title (first sentence/line)."""
    text = _normalize_card_text(value, 10_000)
    if not text:
        return None
    # Prefer the first sentence when the goal is a longer brief.
    match = re.match(r"(.+?[.!?])(?:\s|$)", text)
    if match and len(match.group(1)) >= 12:
        text = match.group(1)
    return _normalize_card_text(text, _GOAL_TITLE_CHARS)


def _clean_handoff(value: Any) -> Optional[dict]:
    """Normalize the model's handoff summary and keep a qualified numeric range.

    A field that is missing or empty after normalization is ``None``; the card
    then fills it from the task goal. Nothing here rejects the whole summary
    because of punctuation or wording.
    """
    if not isinstance(value, dict):
        return None
    fields: Dict[str, Any] = {
        "work": _normalize_card_text(value.get("work"), _HANDOFF_WORK_CHARS),
        "outcome": _normalize_card_text(value.get("outcome"), _HANDOFF_OUTCOME_CHARS),
    }
    if fields["work"] is None and fields["outcome"] is None:
        return None
    low, high = value.get("estimated_minutes_min"), value.get("estimated_minutes_max")
    estimate = None
    if (
        not isinstance(low, bool) and not isinstance(high, bool)
        and isinstance(low, (int, float)) and isinstance(high, (int, float))
    ):
        try:
            low_num, high_num = float(low), float(high)
            if (
                math.isfinite(low_num) and math.isfinite(high_num)
                and 1 <= low_num <= high_num <= _MAX_HANDOFF_ETA_MINUTES
            ):
                estimate = (low_num, high_num)
        except (OverflowError, TypeError, ValueError):
            pass
    fields["estimate"] = estimate
    return fields


def _with_period(value: str) -> str:
    return value if value.endswith((".", "!", "?", "…")) else value + "."


def _format_minutes(value: float) -> str:
    return str(math.floor(value + 0.5))

DEFAULT_MAX_WORK_TOOLS = 5
DEFAULT_MAX_FOREGROUND_SECONDS = 120.0
DEFAULT_MAX_INTEGRATION_WINDOWS = 2
_MAX_WINDOW_HISTORY = 64

# The only trusted origin kind whose delivery keeps a live ledger and may open
# an integration window. Exact match: other kinds (including the TUI's
# async_delegation_complete) reset to a fresh request.
INTEGRATION_KINDS = frozenset({"internal_notification"})
PRESERVE_KINDS = INTEGRATION_KINDS

# Control plane: choosing, discovering and dispatching. Everything else —
# including unknown, new and MCP tools — is work.
CONTROL_PLANE_TOOLS = frozenset({
    "todo",
    "skill_view",
    "skills_list",
    "tool_search",
    "tool_describe",
    "clarify",
    "delegate_task",
})

_FALSE_STRINGS = frozenset({"false", "0", "no", "off"})


@dataclass(frozen=True)
class CheckpointSettings:
    enabled: bool = True
    max_work_tools: int = DEFAULT_MAX_WORK_TOOLS
    max_foreground_seconds: float = DEFAULT_MAX_FOREGROUND_SECONDS
    max_integration_windows: int = DEFAULT_MAX_INTEGRATION_WINDOWS


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return default
        value = int(value)
    if isinstance(value, int) and value > 0:
        return value
    return default


def _positive_seconds(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = float(value)
    if math.isfinite(value) and value > 0:
        return value
    return default


def normalize_settings(raw: Any) -> CheckpointSettings:
    """Validate a ``delegation.checkpoint`` mapping; never yield a zero budget."""
    if not isinstance(raw, dict):
        return CheckpointSettings()
    enabled_raw = raw.get("enabled", True)
    if isinstance(enabled_raw, str):
        enabled = enabled_raw.strip().lower() not in _FALSE_STRINGS
    elif isinstance(enabled_raw, bool):
        enabled = enabled_raw
    else:
        enabled = True
    return CheckpointSettings(
        enabled=enabled,
        max_work_tools=_positive_int(
            raw.get("max_work_tools"), DEFAULT_MAX_WORK_TOOLS
        ),
        max_foreground_seconds=_positive_seconds(
            raw.get("max_foreground_seconds"), DEFAULT_MAX_FOREGROUND_SECONDS
        ),
        max_integration_windows=_positive_int(
            raw.get("max_integration_windows"), DEFAULT_MAX_INTEGRATION_WINDOWS
        ),
    )


def load_settings() -> CheckpointSettings:
    """Read ``delegation.checkpoint`` from the active profile's config."""
    if os.environ.get("CLOVER_IGNORE_USER_CONFIG") == "1":
        return CheckpointSettings()
    try:
        from clover_cli.config import load_config_readonly

        delegation = (load_config_readonly() or {}).get("delegation") or {}
        raw = delegation.get("checkpoint") if isinstance(delegation, dict) else None
        return normalize_settings(raw)
    except Exception:
        logger.debug("delegation.checkpoint config unreadable; using defaults", exc_info=True)
        return CheckpointSettings()


@dataclass(frozen=True)
class Admission:
    """Private, generation-bound proof that one work call was admitted."""

    checkpoint: "DelegationCheckpoint"
    token: str
    generation: int
    tool_name: str
    tool_call_id: str
    in_window: bool = False


@dataclass(frozen=True)
class Verdict:
    """Outcome of :func:`admit`: allowed (maybe with an admission) or blocked."""

    admission: Optional[Admission] = None
    block_result: Optional[str] = None
    block_code: Optional[str] = None
    block_message: Optional[str] = None

    @property
    def blocked(self) -> bool:
        return self.block_result is not None


ALLOWED = Verdict()


class AppliedSlot:
    """Private per-invocation transport between a todo worker and the root.

    The worker may only WRITE data here: ``decision`` (the normalized
    declaration ``todo`` really applied) and ``completed`` (set after a normal
    managed return). Neither is authority. The foreground root alone reads a
    snapshot of them after it has accepted the completion.
    """

    __slots__ = ("decision", "completed", "registered")

    def __init__(self) -> None:
        self.decision: Optional[Dict[str, str]] = None
        self.completed: bool = False
        self.registered: bool = False


@dataclass(frozen=True)
class DeclarationOwner:
    """Immutable per-invocation receipt, captured BEFORE a todo call runs.

    Pairs the checkpoint identity + generation with the agent identity and the
    tool-call id, plus this invocation's own private ``AppliedSlot``. A new
    receipt (and slot) is allocated for every call; none is ever shared.
    """

    checkpoint: "DelegationCheckpoint"
    generation: int
    agent_id: int = 0
    tool_call_id: str = ""
    slot: AppliedSlot = field(default_factory=AppliedSlot, compare=False, repr=False)


@dataclass
class OwnedHandoff:
    """The background job a request handed its remaining phase to.

    Built by the checkpoint (never by the caller) at the moment the dispatch is
    accepted, so every binding field comes from runtime state. ``receipt_ids``
    are the durable async-delegation row ids that will carry the result: the
    delegation id for one goal, ``<id>:child:<i>`` per goal for a fan-out.
    """

    delegation_id: str
    request_id: str
    generation: int
    accepted_at: float
    goals: tuple
    subagent_ids: tuple
    declared_reason: str
    receipt_ids: tuple
    handoff: Optional[dict] = None
    continuation_plan: tuple = ()
    continuation_expires_at: Optional[float] = None
    runtime_policy: Optional[dict] = None
    consumed_receipts: set = field(default_factory=set, compare=False, repr=False)


@dataclass
class IntegrationWindow:
    """A bounded verification allowance for one finished background result.

    Owned by the request that handed off, never by the live ledger: work done
    inside it spends this window's own budget only.
    """

    request_id: str
    receipt_ids: tuple
    used: int = 0
    first_work_at: Optional[float] = None
    spent: bool = False
    continuation_plan: tuple = ()
    continuation_expires_at: Optional[float] = None
    previous_delegation_id: str = ""
    continuation_generation: Optional[int] = None
    continuation_receipt_id: str = ""
    continuation_policy: Optional[dict] = None
    continuation_claimed: bool = False


@dataclass(frozen=True)
class Directive:
    """A deterministic, provider-free end-of-turn message (see completion_directive)."""

    reason: str
    text: str


@dataclass(frozen=True)
class DispatchTicket:
    """Generation-bound right to credit a real child start or accept a handoff."""

    checkpoint: "DelegationCheckpoint"
    generation: int

    def credit(self, kind: str) -> bool:
        return self.checkpoint._credit_dispatch(self.generation, kind)

    def accept_handoff(
        self, *, delegation_id: str, goals: Any, subagent_ids: Any = (), handoff: Any = None,
        continuation_plan: Any = None, accepted_task_data: Any = None,
        runtime_policy: Any = None,
    ) -> bool:
        """A background dispatch was really accepted: it owns the rest.

        Only a ticket from the live generation of a request that still has
        foreground authority can hand off. A stale, superseded or already
        handed-off ticket returns False and changes nothing; it never cancels
        the job.
        """
        return self.checkpoint._accept_handoff(
            self.generation,
            delegation_id=delegation_id,
            goals=goals,
            subagent_ids=subagent_ids,
            handoff=handoff,
            continuation_plan=continuation_plan,
            accepted_task_data=accepted_task_data,
            runtime_policy=runtime_policy,
        )


class DelegationCheckpoint:
    """Per-root runtime authorization (thread-safe, monotonic-clock budgets)."""

    def __init__(
        self,
        settings: Optional[CheckpointSettings] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self.settings = settings or CheckpointSettings()
        self.generation = 0
        self.request_id = uuid.uuid4().hex
        self.phase = PHASE_FOREGROUND
        self.state = UNDECIDED
        self.used = 0
        self.first_work_at: Optional[float] = None
        self.decision: Optional[Dict[str, str]] = None
        self.exit_armed = False
        self.turn_blocks = 0
        self._blocked_in_message = False
        self.window: Optional[IntegrationWindow] = None
        self._outstanding: Dict[str, Admission] = {}
        # Integration windows opened per originating request (bounded).
        self._windows_by_request: Dict[str, int] = {}
        # Handoffs outlive the request that made them so a late result can be
        # attributed to its own request, never to a newer one.
        self._owned: Dict[str, OwnedHandoff] = {}

    # ── lifecycle ──────────────────────────────────────────────────────
    def _reset_locked(self) -> None:
        """A new human request: the only thing that restores the allowance."""
        self.generation += 1
        self.request_id = uuid.uuid4().hex
        self.phase = PHASE_FOREGROUND
        self.state = UNDECIDED
        self.used = 0
        self.first_work_at = None
        self.decision = None
        self.exit_armed = False
        self.turn_blocks = 0
        self._blocked_in_message = False
        self.window = None
        self._outstanding.clear()

    def begin_turn(
        self,
        *,
        preserve: bool,
        settings: CheckpointSettings,
        kind: Optional[str] = None,
        receipt_probe: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        """Start a request, or keep the live ledger for a trusted delivery.

        A preserved internal delivery keeps the stored ledger exactly as it is.
        It may additionally open one bounded integration window, but only when
        a durable, claimed, terminal receipt of a job this checkpoint handed
        off authenticates (``receipt_probe``; the real durable table by
        default). Everything else leaves no new authority.
        """
        with self._lock:
            # Per-turn flags never outlive their turn, preserved or not.
            self.window = None
            self.exit_armed = False
            self.turn_blocks = 0
            self._blocked_in_message = False
            if not preserve:
                self.settings = settings
                self._reset_locked()
                return
            candidates = {
                rid: handoff
                for handoff in self._owned.values()
                for rid in handoff.receipt_ids
                if rid not in handoff.consumed_receipts
            }
        if kind not in INTEGRATION_KINDS or not candidates:
            return
        probe = receipt_probe or _durable_receipts_ready
        try:
            ready = set(probe(tuple(candidates)))
        except Exception:
            logger.debug("receipt probe failed; no window opened", exc_info=True)
            return
        ready &= set(candidates)
        if ready:
            self._open_window(ready, candidates)

    def _open_window(self, ready: set, candidates: Dict[str, "OwnedHandoff"]) -> None:
        with self._lock:
            # Authenticated receipts are spent when seen; replay grants nothing.
            ready = {r for r in ready if r not in candidates[r].consumed_receipts}
            if not ready:
                return
            by_request: Dict[str, list] = {}
            for rid in sorted(ready):
                by_request.setdefault(candidates[rid].request_id, []).append(rid)
                candidates[rid].consumed_receipts.add(rid)
            # The earliest handed-off request with cap left owns the window;
            # receipts beyond a request's cap are report-only.
            for request_id in sorted(
                by_request,
                key=lambda r: min(
                    candidates[x].accepted_at for x in by_request[r]
                ),
            ):
                if (
                    self._windows_by_request.get(request_id, 0)
                    >= self.settings.max_integration_windows
                ):
                    continue
                self._windows_by_request[request_id] = (
                    self._windows_by_request.get(request_id, 0) + 1
                )
                while len(self._windows_by_request) > _MAX_WINDOW_HISTORY:
                    del self._windows_by_request[next(iter(self._windows_by_request))]
                owner = next(
                    (candidates[r] for r in by_request[request_id]
                     if candidates[r].continuation_plan),
                    candidates[by_request[request_id][0]],
                )
                plan = owner.continuation_plan
                expires = owner.continuation_expires_at
                if expires is None or time.time() >= expires:
                    plan = ()
                self.window = IntegrationWindow(
                    request_id=request_id,
                    receipt_ids=tuple(by_request[request_id]),
                    continuation_plan=tuple(plan),
                    continuation_expires_at=expires if plan else None,
                    previous_delegation_id=owner.delegation_id,
                    continuation_generation=owner.generation if plan else None,
                    continuation_receipt_id=(by_request[request_id][0] if plan else ""),
                    continuation_policy=owner.runtime_policy if plan else None,
                )
                self.generation += 1
                logger.debug("delegation checkpoint: integration window opened")
                return

    def declare(
        self, mode: str, reason: str, *, expected_generation: Optional[int] = None
    ) -> bool:
        """Record a valid ``todo.delegation`` event.

        A declaration says what the agent intends; it grants no allowance. It
        never touches the request, the phase or the work ledger, so restating
        the choice, renaming the todo or naming a new phase cannot extend the
        foreground budget. It still bumps ``generation`` so an earlier
        in-flight declaration, admission or ticket can never apply to it.

        ``expected_generation`` is the generation captured before the todo call
        ran. If this checkpoint has moved on since (a new request or another
        declaration), the declaration is stale and not applied. Returns whether
        it was applied.
        """
        with self._lock:
            if expected_generation is not None and expected_generation != self.generation:
                return False
            self.generation += 1
            self.decision = {"mode": mode, "reason": reason}
            self.state = SPAWN_REQUIRED if mode == "delegate" else DIRECT_AUTHORIZED
            return True

    def current_generation(self) -> int:
        with self._lock:
            return self.generation

    def _effective_phase_locked(self) -> str:
        if self.window is not None:
            return PHASE_CLOSED if self.window.spent else PHASE_INTEGRATING
        return self.phase

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "generation": self.generation,
                "request_id": self.request_id,
                "phase": self._effective_phase_locked(),
                "state": self.state,
                "used": self.used,
                "decision": dict(self.decision) if self.decision else None,
                "outstanding": len(self._outstanding),
            }

    # ── admission ──────────────────────────────────────────────────────
    def admit(self, tool_name: str, tool_call_id: str) -> Verdict:
        with self._lock:
            if self.window is not None:
                return self._admit_window_locked(tool_name, tool_call_id)
            if self.phase == PHASE_HANDED_OFF:
                return self._block(HANDOFF_ACTIVE, tool_name)
            if self.phase == PHASE_EXHAUSTED:
                return self._block(FOREGROUND_EXHAUSTED, tool_name)
            if self.state == SPAWN_REQUIRED:
                return self._block(DISPATCH_REQUIRED, tool_name)
            if self.state in (DIRECT_AUTHORIZED, DELEGATED_STARTED):
                if self._budget_exhausted_locked():
                    self.phase = PHASE_EXHAUSTED
                    return self._block(FOREGROUND_EXHAUSTED, tool_name)
                return self._reserve_locked(tool_name, tool_call_id)
            return self._block(DECISION_REQUIRED, tool_name)

    def admit_spawn(self, is_background: bool, args: Any = None) -> Verdict:
        """Gate one ``delegate_task`` dispatch (not list/steer/stop).

        A background dispatch is the one way out of an exhausted request, and a
        rejected one leaves the phase alone. A synchronous dispatch is held to
        the same budget as any work call, but only latches here: the unit is
        charged at the real child start (``credit('inline')``), so a rejected
        construction spends nothing and a started child is charged once.
        """
        with self._lock:
            if self.window is not None:
                window = self.window
                from agent.delegation_followthrough import matches_stage

                if (
                    not is_background or window.spent or window.continuation_claimed
                    or not window.continuation_plan
                    or window.continuation_expires_at is None
                    or time.time() >= window.continuation_expires_at
                    or not matches_stage(args, window.continuation_plan[0])
                ):
                    return self._block(SPAWN_CLOSED, "delegate_task")
                from tools.async_delegation import claim_continuation_stage
                if not claim_continuation_stage(
                    delegation_id=window.previous_delegation_id,
                    request_id=window.request_id,
                    generation=window.continuation_generation,
                    receipt_id=window.continuation_receipt_id,
                    stage=window.continuation_plan[0],
                ):
                    return self._block(SPAWN_CLOSED, "delegate_task")
                # The durable one-shot claim is taken before dispatch; failures
                # remain closed rather than risking a duplicate worker.
                window.continuation_claimed = True
                return ALLOWED
            if self.phase == PHASE_HANDED_OFF:
                return self._block(SPAWN_CLOSED, "delegate_task")
            if is_background:
                return ALLOWED
            if self.phase == PHASE_EXHAUSTED:
                return self._block(FOREGROUND_EXHAUSTED, "delegate_task")
            if self._budget_exhausted_locked():
                self.phase = PHASE_EXHAUSTED
                return self._block(FOREGROUND_EXHAUSTED, "delegate_task")
            return ALLOWED

    def _budget_exhausted_locked(self) -> bool:
        if self.used >= self.settings.max_work_tools:
            return True
        if self.first_work_at is not None:
            elapsed = self._clock() - self.first_work_at
            if elapsed >= self.settings.max_foreground_seconds:
                return True
        return False

    def _admit_window_locked(self, tool_name: str, tool_call_id: str) -> Verdict:
        """Work inside an integration window: same size as the base budget."""
        window = self.window
        if not window.spent:
            expired = (
                window.first_work_at is not None
                and self._clock() - window.first_work_at
                >= self.settings.max_foreground_seconds
            )
            if window.used >= self.settings.max_work_tools or expired:
                window.spent = True
        if window.spent:
            return self._block(INTEGRATION_EXHAUSTED, tool_name)
        if window.first_work_at is None:
            window.first_work_at = self._clock()
        window.used += 1
        admission = Admission(
            checkpoint=self,
            token=uuid.uuid4().hex,
            generation=self.generation,
            tool_name=tool_name,
            tool_call_id=tool_call_id or "",
            in_window=True,
        )
        self._outstanding[admission.token] = admission
        return Verdict(admission=admission)

    def _reserve_locked(self, tool_name: str, tool_call_id: str) -> Verdict:
        if self.first_work_at is None:
            self.first_work_at = self._clock()
        self.used += 1
        admission = Admission(
            checkpoint=self,
            token=uuid.uuid4().hex,
            generation=self.generation,
            tool_name=tool_name,
            tool_call_id=tool_call_id or "",
        )
        self._outstanding[admission.token] = admission
        return Verdict(admission=admission)

    def _block(self, code: str, tool_name: str) -> Verdict:
        if code in _CLOSING_CODES:
            # Counted per assistant message at the loop seam, not per call.
            self._blocked_in_message = True
        if code == DISPATCH_REQUIRED:
            message = (
                f"Delegation checkpoint ({code}): you chose to delegate, so "
                f"'{tool_name}' was not run. Dispatch at least one child with "
                "delegate_task first (list/steer/stop do not count), or record "
                "todo delegation mode 'direct' with the blocker as the reason."
            )
            recovery: Dict[str, Any] = {
                "tool": "delegate_task",
                "alternative": _direct_recovery(),
            }
        elif code == FOREGROUND_EXHAUSTED:
            message = (
                f"Delegation checkpoint ({code}): '{tool_name}' was not run. "
                "The foreground allowance for this request "
                f"({self.settings.max_work_tools} work calls or "
                f"{self.settings.max_foreground_seconds:g} seconds) is spent and "
                "declaring again does not extend it. If the user allowed "
                "delegated work, hand the remaining phase to delegate_task; "
                "otherwise stop and give the user an honest status."
            )
            recovery = {
                "tool": "delegate_task",
                "only_if": "the user has not prohibited subagents",
                "otherwise": "stop and report an honest status to the user",
            }
        elif code == INTEGRATION_EXHAUSTED or (
            code == SPAWN_CLOSED and self.window is not None
        ):
            message = (
                f"Delegation checkpoint ({code}): '{tool_name}' was not run. "
                "This turn only checks and reports a finished background "
                "result, and its verification allowance is limited and "
                "closed to new helpers. Stop and give the user an honest "
                "status of what you verified and what you could not."
            )
            recovery = {"action": "stop and report an honest status to the user"}
        elif code in (HANDOFF_ACTIVE, SPAWN_CLOSED) and not self._live_handoffs_locked(
            self.request_id
        ):
            # Every receipt of this request's job was already delivered: it is
            # not running any more, but the request's foreground stays closed.
            message = (
                f"Delegation checkpoint ({code}): '{tool_name}' was not run. "
                "The background job for this request has already finished and "
                "its result was already delivered into this conversation. No "
                "further foreground work is available for this request: stop "
                "and give the user a short status; their next message starts "
                "a new request."
            )
            recovery = {"action": "stop and report a short status to the user"}
        elif code in (HANDOFF_ACTIVE, SPAWN_CLOSED):
            message = (
                f"Delegation checkpoint ({code}): '{tool_name}' was not run. "
                "A background job for this request owns the remaining phase "
                "and its result has not yet returned into this conversation. "
                "Do not repeat its work here: stop and give the user a short "
                "status."
            )
            recovery = {"action": "stop and report a short status to the user"}
        else:
            message = (
                f"Delegation checkpoint ({code}): '{tool_name}' was not run. "
                "Before using work tools, record your choice with the todo tool: "
                "delegation={mode: 'direct', reason: '<brief operational "
                "rationale>'} to do the work yourself, or mode 'delegate' and "
                "then call delegate_task."
            )
            recovery = _direct_recovery()
        payload = {
            "error": message,
            "error_type": code,
            "blocked_tool": tool_name,
            "recovery": recovery,
        }
        return Verdict(
            block_result=json.dumps(payload, ensure_ascii=False),
            block_code=code,
            block_message=message,
        )

    # ── outcomes ───────────────────────────────────────────────────────
    def finish(self, admission: Admission) -> None:
        """The admitted call returned; its reservation is final (counted)."""
        with self._lock:
            self._outstanding.pop(admission.token, None)

    def refund(self, admission: Admission) -> bool:
        """Release a reservation for a call that provably did not execute."""
        with self._lock:
            if admission.generation != self.generation:
                return False
            if self._outstanding.pop(admission.token, None) is None:
                return False
            if admission.in_window:
                if self.window is not None:
                    self.window.used = max(0, self.window.used - 1)
                    if self.window.used == 0:
                        self.window.first_work_at = None
                return True
            self.used = max(0, self.used - 1)
            if self.used == 0:
                self.first_work_at = None
            return True

    # ── dispatch crediting ─────────────────────────────────────────────
    def ticket(self) -> DispatchTicket:
        with self._lock:
            return DispatchTicket(self, self.generation)

    def _credit_dispatch(self, generation: int, kind: str) -> bool:
        """A synchronous child really started (``kind == 'inline'``).

        Clears ``SPAWN_REQUIRED`` and spends one work unit, so a chain of
        synchronous children cannot run unbounded inside one request. A
        background dispatch is not credited here: it is a handoff
        (:meth:`_accept_handoff`).
        """
        with self._lock:
            if kind != "inline" or generation != self.generation:
                return False
            if self.window is not None:
                return False
            credited = False
            if self.state == SPAWN_REQUIRED:
                self.state = DELEGATED_STARTED
                credited = True
            if self.phase == PHASE_FOREGROUND:
                if self.first_work_at is None:
                    self.first_work_at = self._clock()
                self.used += 1
                credited = True
            logger.debug("delegation checkpoint: inline child start credited")
            return credited

    def _accept_handoff(
        self, generation: int, *, delegation_id: str, goals: Any, subagent_ids: Any,
        handoff: Any = None, continuation_plan: Any = None, accepted_task_data: Any = None,
        runtime_policy: Any = None,
    ) -> bool:
        from agent.delegation_followthrough import (
            MAX_PLAN_AGE_SECONDS, MAX_STAGES, canonical_stages, persist_accepted_plan,
        )

        proposed_plan = canonical_stages(continuation_plan)
        accepted_stages = proposed_plan or ()
        clean_policy = None
        if isinstance(runtime_policy, dict):
            try:
                clean_policy = json.loads(json.dumps(runtime_policy, sort_keys=True))
            except (TypeError, ValueError):
                clean_policy = None
        with self._lock:
            if not self.settings.enabled or generation != self.generation:
                return False
            if self.window is not None:
                window = self.window
                if (
                    window.spent or not window.continuation_claimed
                    or not window.continuation_plan
                    or window.continuation_expires_at is None
                    or time.time() >= window.continuation_expires_at
                    or len(window.continuation_plan) > MAX_STAGES
                    or len(tuple(goals or ())) != 1
                    or not isinstance(accepted_task_data, list)
                    or len(accepted_task_data) != 1
                    or accepted_task_data[0] != window.continuation_plan[0]
                    or not clean_policy or clean_policy != window.continuation_policy
                ):
                    return False
                # Consume before the next dispatch can acquire more authority.
                # A failed persistence/update stays closed rather than replaying.
                accepted_stages = window.continuation_plan[1:]
                request_id = window.request_id
                accepted_policy = window.continuation_policy
                accepted_at = window.continuation_expires_at - MAX_PLAN_AGE_SECONDS
                window.spent = True
                self.window = None
            else:
                if self.phase not in (PHASE_FOREGROUND, PHASE_EXHAUSTED):
                    return False
                request_id = self.request_id
                accepted_at = time.time()
                accepted_policy = clean_policy
                # A malformed/oversized proposal or ambiguous fan-out receipt
                # adds no continuation authority.
                accepted_stages = proposed_plan or ()
                if len(tuple(goals or ())) != 1 or not accepted_policy:
                    accepted_stages = ()

            goal_texts = tuple(str(g)[:_MAX_GOAL_CHARS] for g in (goals or ()))
            if len(goal_texts) <= 1:
                receipt_ids = (delegation_id,)
            else:
                receipt_ids = tuple(f"{delegation_id}:child:{i}" for i in range(len(goal_texts)))

            if accepted_stages and not persist_accepted_plan(
                delegation_id=delegation_id,
                request_id=request_id,
                generation=generation,
                receipt_ids=receipt_ids,
                stages=accepted_stages,
                accepted_at=accepted_at,
                runtime_policy=accepted_policy,
            ):
                accepted_stages = ()
            expires_at = accepted_at + MAX_PLAN_AGE_SECONDS if accepted_stages else None
            self._owned[delegation_id] = OwnedHandoff(
                delegation_id=delegation_id,
                request_id=request_id,
                generation=generation,
                accepted_at=self._clock(),
                goals=goal_texts,
                subagent_ids=tuple(str(s) for s in (subagent_ids or ())),
                declared_reason=(self.decision or {}).get("reason", ""),
                receipt_ids=receipt_ids,
                handoff=_clean_handoff(handoff),
                continuation_plan=tuple(accepted_stages),
                continuation_expires_at=expires_at,
                runtime_policy=accepted_policy if accepted_stages else None,
            )
            while len(self._owned) > _MAX_OWNED_HANDOFFS:
                self._evict_owned_locked()
            self.phase = PHASE_HANDED_OFF
            self.exit_armed = True
            logger.debug("delegation checkpoint: handoff accepted (%s)", delegation_id)
            return True

    def expected_followthrough_policy(self) -> Optional[dict]:
        """Return policy only while a claimed owned stage window is live."""
        with self._lock:
            window = self.window
            if (
                window is None or not window.continuation_plan
                or window.continuation_expires_at is None
                or time.time() >= window.continuation_expires_at
            ):
                return None
            return dict(window.continuation_policy or {})

    def revoke_followthrough(self, subagent_id: str) -> bool:
        """Explicit stop revokes any unconsumed plan owned by that child."""
        delegation_ids = []
        with self._lock:
            for owned in self._owned.values():
                if subagent_id and subagent_id in owned.subagent_ids:
                    owned.continuation_plan = ()
                    owned.continuation_expires_at = None
                    delegation_ids.append(owned.delegation_id)
                    if self.window and self.window.previous_delegation_id == owned.delegation_id:
                        self.window.continuation_plan = ()
                        self.window.continuation_expires_at = None
                        self.window.spent = True
        if not delegation_ids:
            return False
        try:
            from tools.async_delegation import revoke_continuation_plan
            for delegation_id in delegation_ids:
                revoke_continuation_plan(delegation_id)
        except Exception:
            logger.debug("continuation revocation persistence failed", exc_info=True)
        return True

    def _evict_owned_locked(self) -> None:
        """Drop the oldest fully consumed handoff, else the oldest of all."""
        for key, handoff in self._owned.items():
            if handoff.receipt_ids and set(handoff.receipt_ids) <= handoff.consumed_receipts:
                del self._owned[key]
                return
        del self._owned[next(iter(self._owned))]


    # ── normal completion ──────────────────────────────────────────────
    def take_completion_directive(self) -> Optional[Directive]:
        """Whether the turn should end now, and with what deterministic text.

        Called once per assistant message, after every tool result of that
        message is canonical. An accepted handoff ends the turn at once. A
        message with a close/exhaust block gets one more provider call so the
        model can write its own status; a second such message ends the turn.
        Never cancels anything and never calls a provider.
        """
        with self._lock:
            if not self.settings.enabled:
                return None
            if self.exit_armed:
                self.exit_armed = False
                self._blocked_in_message = False
                return Directive("delegation_handoff", self._handoff_text_locked())
            if not self._blocked_in_message:
                return None
            self._blocked_in_message = False
            self.turn_blocks += 1
            if self.turn_blocks < 2:
                return None
            if self.window is not None:
                return Directive(
                    INTEGRATION_EXHAUSTED,
                    "I stopped checking here: the verification allowance for the "
                    "finished background result is spent, and the calls in my "
                    "last message were not run.",
                )
            return Directive(FOREGROUND_EXHAUSTED, self._exhausted_text_locked())

    def _live_handoffs_locked(self, request_id: str) -> list:
        return [
            h for h in self._owned.values()
            if h.request_id == request_id
            and not set(h.receipt_ids) <= h.consumed_receipts
        ]

    def _handoff_text_locked(self) -> str:
        handoffs = self._live_handoffs_locked(self.request_id)
        if not handoffs:
            return (
                "**delegated:** accepted background work has no readable summary here.\n\n"
                "**goal:** its result will return to this conversation."
            )
        handoff = max(handoffs, key=lambda h: h.accepted_at)
        count = len(handoff.goals)
        details = handoff.handoff or {}
        titles = [t for t in (_goal_title(g) for g in handoff.goals) if t]
        work = details.get("work")
        outcome = details.get("outcome")
        if work is None:
            work = self._goal_work_text(count, titles)
        elif count > 1:
            work = f"Workers are {_lower_lead(work)}"
        if outcome is None:
            title = titles[0].rstrip(".!?") if count == 1 and titles else ""
            if title and not title.endswith("…") and len(title) <= 90:
                outcome = f"a completed result for {title}, returned here"
            else:
                outcome = (
                    "completed results for those tasks, returned here" if count > 1
                    else "a completed result for this task, returned here"
                )
        return (
            f"**delegated:** {_with_period(work)}\n\n"
            f"**goal:** {_with_period(outcome)}"
        )

    @staticmethod
    def _goal_work_text(count: int, titles: list) -> str:
        """Card work line from the task goals when the model gave no summary."""
        if count <= 1:
            return titles[0] if titles else "handling the background task you asked for"
        if titles and len(titles) == count and count <= 3:
            joined = " and ".join(_lower_lead(t.rstrip(".!?")) for t in titles)
            if len(joined) <= 150:
                return f"Workers are handling {joined}"
        lead = _lower_lead(titles[0].rstrip(".!?…")) if titles else ""
        text = f"Workers are handling {count} tasks" + (f", starting with {lead}" if lead else "")
        return _normalize_card_text(text, 160) or f"Workers are handling {count} tasks"

    def _exhausted_text_locked(self) -> str:
        base = (
            "I stopped here: the foreground allowance for this request is "
            "spent, and the calls in my last message were not run."
        )
        handoffs = self._live_handoffs_locked(self.request_id)
        if handoffs:
            ids = ", ".join(h.delegation_id for h in handoffs)
            return (
                f"{base} A background job started for this request ({ids}) has "
                "not returned yet and its result will return to this conversation."
            )
        if self.phase == PHASE_HANDED_OFF:
            # Handed off and every receipt already consumed (the record itself
            # may even have been evicted): never deny it, never call it running.
            ids = ", ".join(
                h.delegation_id for h in self._owned.values()
                if h.request_id == self.request_id
            )
            named = f" ({ids})" if ids else ""
            return (
                f"{base} The background job started for this request{named} "
                "has already finished and its result was already delivered to "
                "this conversation."
            )
        return f"{base} Also, no background job was started for this request."


def _direct_recovery() -> Dict[str, Any]:
    return {
        "tool": "todo",
        "arguments": {
            "delegation": {
                "mode": "direct",
                "reason": "<brief operational rationale>",
            }
        },
    }


# ── per-agent access ───────────────────────────────────────────────────
_CREATE_LOCK = threading.Lock()
_ACTIVE_ADMISSION: contextvars.ContextVar[Optional[Admission]] = contextvars.ContextVar(
    "delegation_checkpoint_active_admission", default=None
)


def is_eligible(agent: Any) -> bool:
    """A conversational root that actually has both ``todo`` and ``delegate_task``.

    Leaves/delegated contexts, cron, the real background-review fork and any
    caller-marked noninteractive root are exempt. Introspection failures fail
    open: the gate must never break agents it cannot identify.
    """
    try:
        if getattr(agent, "_is_background_review_fork", False) is True:
            return False
        if getattr(agent, "_delegation_checkpoint_exempt", None):
            return False
        if getattr(agent, "platform", None) == "cron":
            return False
        # A dispatcher-spawned kanban worker is an explicitly noninteractive
        # process (the dispatcher sets this per worker; see agent_init).
        if os.environ.get("CLOVER_KANBAN_TASK"):
            return False
        from tools.todo_tool import delegation_check_for_agent

        return bool(delegation_check_for_agent(agent))
    except Exception:
        return False


def checkpoint_applies(agent: Any) -> bool:
    """Whether the checkpoint is enabled and this agent is eligible for it."""
    return is_eligible(agent) and load_settings().enabled


def get_checkpoint(agent: Any) -> Optional[DelegationCheckpoint]:
    """The agent's checkpoint state, created lazily (undecided)."""
    state = getattr(agent, "_delegation_checkpoint", None)
    if isinstance(state, DelegationCheckpoint):
        return state
    if not is_eligible(agent):
        return None
    with _CREATE_LOCK:
        # Concurrent workers in one batch may race to first use.
        state = getattr(agent, "_delegation_checkpoint", None)
        if isinstance(state, DelegationCheckpoint):
            return state
        state = DelegationCheckpoint(load_settings())
        try:
            agent._delegation_checkpoint = state
        except Exception:
            return None
        return state


def begin_turn(agent: Any, persist_user_display_kind: Optional[str] = None) -> None:
    """Called once per fresh root ``run_conversation`` entry."""
    existing = getattr(agent, "_delegation_checkpoint", None)
    if not isinstance(existing, DelegationCheckpoint):
        if is_eligible(agent):
            get_checkpoint(agent)  # lazily created undecided
        return
    existing.begin_turn(
        preserve=persist_user_display_kind in PRESERVE_KINDS,
        settings=load_settings(),
        kind=persist_user_display_kind,
    )


def _durable_receipts_ready(receipt_ids: Any) -> Any:
    from tools.async_delegation import owned_receipts_ready

    return owned_receipts_ready(receipt_ids)


def completion_directive(agent: Any) -> Optional[Directive]:
    """The conversation loop's question after a tool round: end the turn now?

    Reads existing state only; agents without a checkpoint never end early.
    """
    checkpoint = getattr(agent, "_delegation_checkpoint", None)
    if not isinstance(checkpoint, DelegationCheckpoint):
        return None
    return checkpoint.take_completion_directive()


def resolve_work_call(function_name: str, function_args: Any) -> tuple:
    """Peel the Tool Search ``tool_call`` wrapper to the underlying (name, args)."""
    try:
        from tools import tool_search as _ts

        if function_name == _ts.TOOL_CALL_NAME and isinstance(function_args, dict):
            underlying, args, err = _ts.resolve_underlying_call(function_args)
            if not err and underlying:
                return underlying, args
    except Exception:
        pass
    return function_name, function_args


def resolve_work_name(function_name: str, function_args: Any) -> str:
    """Peel the Tool Search ``tool_call`` wrapper to the underlying tool name."""
    return resolve_work_call(function_name, function_args)[0]


def current_admission_for(agent: Any, tool_name: str, tool_call_id: str) -> Optional[Admission]:
    """The executor-issued admission for exactly this call, if any."""
    admission = _ACTIVE_ADMISSION.get()
    if (
        admission is not None
        and admission.checkpoint is getattr(agent, "_delegation_checkpoint", None)
        and admission.tool_name == tool_name
        and admission.tool_call_id == (tool_call_id or "")
    ):
        return admission
    return None


def _is_spawn_dispatch(args: Any) -> bool:
    """A ``delegate_task`` call that would start children (not list/steer/stop)."""
    if not isinstance(args, dict):
        return False
    action = args.get("action")
    if action is not None and not isinstance(action, str):
        return False
    return (action or "").strip().lower() in ("", "spawn")


def _admit_spawn(checkpoint: DelegationCheckpoint, agent: Any, args: Any) -> Verdict:
    if not _is_spawn_dispatch(args):
        return ALLOWED
    # The dispatcher's own rule decides background; call arguments never do.
    from tools.delegate_tool import _model_background_value

    return checkpoint.admit_spawn(_model_background_value(args, agent), args)


def admit(agent: Any, function_name: str, function_args: Any, tool_call_id: str) -> Verdict:
    """Gate one resolved tool call. Exempt/ineligible agents pass unchanged."""
    name, args = resolve_work_call(function_name, function_args)
    if name in CONTROL_PLANE_TOOLS and name != "delegate_task":
        return ALLOWED
    checkpoint = get_checkpoint(agent)
    if checkpoint is None or not checkpoint.settings.enabled:
        return ALLOWED
    if not is_eligible(agent):
        return ALLOWED
    if name == "delegate_task":
        return _admit_spawn(checkpoint, agent, args)
    return checkpoint.admit(name, tool_call_id)


class admitted:
    """Context manager exposing an admission to downstream nonexecution receipts."""

    def __init__(self, admission: Optional[Admission]) -> None:
        self._admission = admission
        self._token: Optional[contextvars.Token] = None

    def __enter__(self) -> Optional[Admission]:
        if self._admission is not None:
            self._token = _ACTIVE_ADMISSION.set(self._admission)
        return self._admission

    def __exit__(self, *exc_info) -> None:
        if self._token is not None:
            try:
                _ACTIVE_ADMISSION.reset(self._token)
            except ValueError:
                # Reset from a different context (thread hop); clear instead.
                _ACTIVE_ADMISSION.set(None)
        if self._admission is not None:
            self._admission.checkpoint.finish(self._admission)


def report_nonexecution(tool_name: str) -> bool:
    """Private receipt: the in-flight call named ``tool_name`` never executed.

    Raised only from the actual denial/pending boundaries (ACP edit approval,
    terminal approval). It acts on the executor-issued admission held in a
    ContextVar; tool output is never consulted. A nested call for a different
    tool (e.g. ``terminal`` inside ``execute_code``) cannot refund the outer one.
    """
    admission = _ACTIVE_ADMISSION.get()
    if admission is None or admission.tool_name != tool_name:
        return False
    return admission.checkpoint.refund(admission)


def ticket_for(agent: Any) -> Optional[DispatchTicket]:
    """Generation-bound right to credit a child start, for eligible roots."""
    checkpoint = getattr(agent, "_delegation_checkpoint", None)
    if not isinstance(checkpoint, DelegationCheckpoint):
        return None
    return checkpoint.ticket()


def claim_declaration(agent: Any, tool_call_id: str = "") -> Optional[DeclarationOwner]:
    """Capture who owns a declaration before the todo call executes."""
    checkpoint = get_checkpoint(agent)
    if checkpoint is None:
        return None
    return DeclarationOwner(
        checkpoint,
        checkpoint.current_generation(),
        agent_id=id(agent),
        tool_call_id=tool_call_id or "",
    )


def record_declaration(
    agent: Any,
    decision: Optional[Dict[str, str]],
    owner: Optional[DeclarationOwner] = None,
) -> bool:
    """Root-side atomic application of a declaration (validate + declare).

    Called ONLY by the foreground root after it accepted a normal completion
    (see ``register_accepted_declaration``); a todo worker never calls it.
    There is no fallback: without a receipt nothing is granted.

    Authority is granted only if BOTH the checkpoint object and its generation
    still match the receipt, atomically under the checkpoint lock.
    """
    if not decision or owner is None:
        return False
    if getattr(agent, "_delegation_checkpoint", None) is not owner.checkpoint:
        return False
    return owner.checkpoint.declare(
        decision["mode"], decision["reason"], expected_generation=owner.generation
    )


def register_accepted_declaration(
    agent: Any,
    owner: Optional[DeclarationOwner],
    tool_call_id: str,
    decision: Optional[Dict[str, str]] = None,
) -> bool:
    """Foreground root registers an applied declaration it has ACCEPTED.

    The caller has already established that this invocation returned a normal
    completed result (not timed out, cancelled, abandoned, blocked or crashed).
    ``decision`` is the root's frozen snapshot of the slot when it has one (the
    concurrent boundary); otherwise the slot is read now. The receipt must
    belong to exactly this agent and tool call, and registers at most once.
    """
    if owner is None:
        return False
    if owner.agent_id != id(agent) or owner.tool_call_id != (tool_call_id or ""):
        return False
    slot = owner.slot
    if slot.registered:
        return False
    slot.registered = True
    return record_declaration(
        agent, decision if decision is not None else slot.decision, owner
    )


_EXECUTOR_OWNED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "delegation_checkpoint_executor_owned", default=False
)


class executor_context:
    """Marks the whole executor-owned worker pipeline (middleware, plugin hooks,
    dispatch). Inside it a legacy ``invoke_tool`` call with no explicit owner
    must not reacquire declaration authority; the foreground root registers."""

    def __enter__(self) -> None:
        self._token = _EXECUTOR_OWNED.set(True)

    def __exit__(self, *exc_info) -> None:
        try:
            _EXECUTOR_OWNED.reset(self._token)
        except ValueError:
            _EXECUTOR_OWNED.set(False)


def in_executor_context() -> bool:
    return _EXECUTOR_OWNED.get()


# Sentinel for ``invoke_tool(declaration_owner=...)``: "the caller said
# nothing", as opposed to an explicit ``None`` ("no receipt, grant nothing").
RECEIPT_UNSET: Any = object()
