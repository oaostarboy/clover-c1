"""Mandatory delegation checkpoint for conversational root agents.

Before a capable root runs its first *work* tool of a task it must record a
choice with the ``todo`` tool: ``direct`` (do it here, with an operational
reason) or ``delegate`` (with a reason, followed by an actual child dispatch).
This module owns the policy, the per-agent runtime authorization and the
accounting; the executor calls :func:`admit` on its common dispatch funnel.

What this is not: a task classifier. The model still makes the choice and
nothing here judges whether the stated reason is good. It only guarantees that
a choice was made, and that a ``delegate`` choice was followed by a real child
start before further work.

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

DEFAULT_MAX_WORK_TOOLS = 5
DEFAULT_MAX_FOREGROUND_SECONDS = 120.0

# The only trusted origin kind whose delivery keeps a live decision. Exact
# match: other kinds (including the TUI's async_delegation_complete) reset.
PRESERVE_DISPLAY_KIND = "internal_notification"

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


@dataclass(frozen=True)
class DispatchTicket:
    """Generation-bound right to credit one real child start."""

    checkpoint: "DelegationCheckpoint"
    generation: int

    def credit(self, kind: str) -> bool:
        return self.checkpoint._credit_dispatch(self.generation, kind)


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
        self.state = UNDECIDED
        self.used = 0
        self.first_work_at: Optional[float] = None
        self.renewal_pending = False
        self.decision: Optional[Dict[str, str]] = None
        self._outstanding: Dict[str, Admission] = {}

    # ── lifecycle ──────────────────────────────────────────────────────
    def _reset_locked(self) -> None:
        self.generation += 1
        self.state = UNDECIDED
        self.used = 0
        self.first_work_at = None
        self.renewal_pending = False
        self.decision = None
        self._outstanding.clear()

    def begin_turn(self, *, preserve: bool, settings: CheckpointSettings) -> None:
        """Start a work episode, or keep the live one for a trusted delivery."""
        with self._lock:
            if preserve:
                return
            self.settings = settings
            self._reset_locked()

    def declare(
        self, mode: str, reason: str, *, expected_generation: Optional[int] = None
    ) -> bool:
        """Record a valid ``todo.delegation`` event. Every event renews.

        ``expected_generation`` is the generation captured before the todo call
        ran. If this checkpoint has moved on since (a new turn, a renewal, a
        budget expiry), the declaration is stale: it is not applied, so a
        completion that arrives late can never grant authority to a newer
        work episode. Returns whether the declaration was applied.
        """
        with self._lock:
            if expected_generation is not None and expected_generation != self.generation:
                return False
            self._reset_locked()
            self.decision = {"mode": mode, "reason": reason}
            self.state = SPAWN_REQUIRED if mode == "delegate" else DIRECT_AUTHORIZED
            return True

    def current_generation(self) -> int:
        with self._lock:
            return self.generation

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "generation": self.generation,
                "state": self.state,
                "used": self.used,
                "decision": dict(self.decision) if self.decision else None,
                "outstanding": len(self._outstanding),
            }

    # ── admission ──────────────────────────────────────────────────────
    def admit(self, tool_name: str, tool_call_id: str) -> Verdict:
        with self._lock:
            if self.state == SPAWN_REQUIRED:
                return self._block(DISPATCH_REQUIRED, tool_name)
            if self.state in (DIRECT_AUTHORIZED, DELEGATED_STARTED):
                if self._budget_exhausted_locked():
                    self.state = UNDECIDED
                    self.renewal_pending = True
                    self.decision = None
                    self._outstanding.clear()
                    self.generation += 1
                    return self._block(DECISION_REQUIRED, tool_name)
                return self._reserve_locked(tool_name, tool_call_id)
            return self._block(DECISION_REQUIRED, tool_name)

    def _budget_exhausted_locked(self) -> bool:
        if self.used >= self.settings.max_work_tools:
            return True
        if self.first_work_at is not None:
            elapsed = self._clock() - self.first_work_at
            if elapsed >= self.settings.max_foreground_seconds:
                return True
        return False

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
        if code == DISPATCH_REQUIRED:
            message = (
                f"Delegation checkpoint ({code}): you chose to delegate, so "
                f"'{tool_name}' was not run. Dispatch at least one child with "
                "delegate_task first (list/steer/stop do not count), or record "
                "todo delegation mode 'direct' with the blocker as the reason."
            )
            recovery = {
                "tool": "delegate_task",
                "alternative": _direct_recovery(),
            }
        else:
            renewal = (
                f" Your previous choice covered at most "
                f"{self.settings.max_work_tools} work tools or "
                f"{self.settings.max_foreground_seconds:g} seconds; renew it "
                "(restating the same choice is fine)."
                if self.renewal_pending
                else ""
            )
            message = (
                f"Delegation checkpoint ({code}): '{tool_name}' was not run. "
                "Before using work tools, record your choice with the todo tool: "
                "delegation={mode: 'direct', reason: '<brief operational "
                "rationale>'} to do the work yourself, or mode 'delegate' and "
                f"then call delegate_task.{renewal}"
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
            self.used = max(0, self.used - 1)
            if self.used == 0:
                self.first_work_at = None
            return True

    # ── dispatch crediting ─────────────────────────────────────────────
    def ticket(self) -> DispatchTicket:
        with self._lock:
            return DispatchTicket(self, self.generation)

    def _credit_dispatch(self, generation: int, kind: str) -> bool:
        with self._lock:
            if generation != self.generation or self.state != SPAWN_REQUIRED:
                return False
            self.state = DELEGATED_STARTED
            logger.debug("delegation checkpoint: child start credited (%s)", kind)
            return True


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
        preserve=persist_user_display_kind == PRESERVE_DISPLAY_KIND,
        settings=load_settings(),
    )


def resolve_work_name(function_name: str, function_args: Any) -> str:
    """Peel the Tool Search ``tool_call`` wrapper to the underlying tool name."""
    try:
        from tools import tool_search as _ts

        if function_name == _ts.TOOL_CALL_NAME and isinstance(function_args, dict):
            underlying, _args, err = _ts.resolve_underlying_call(function_args)
            if not err and underlying:
                return underlying
    except Exception:
        pass
    return function_name


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


def admit(agent: Any, function_name: str, function_args: Any, tool_call_id: str) -> Verdict:
    """Gate one resolved tool call. Exempt/ineligible agents pass unchanged."""
    name = resolve_work_name(function_name, function_args)
    if name in CONTROL_PLANE_TOOLS:
        return ALLOWED
    checkpoint = get_checkpoint(agent)
    if checkpoint is None or not checkpoint.settings.enabled:
        return ALLOWED
    if not is_eligible(agent):
        return ALLOWED
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
