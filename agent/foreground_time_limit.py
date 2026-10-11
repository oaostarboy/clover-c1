"""Per-message foreground time limit for the main chat agent.

``delegation.foreground_time_limit_seconds`` (default 300; 0/null/false/invalid
= off) gives a root conversational agent a time limit for each human message,
so the user gets a final answer close to that limit while any remaining work
runs in a background helper:

* The clock is monotonic and starts when a turn that consumes a HUMAN message
  begins.  Turns the gateway flags ``internal_notification`` (helper
  completions, watches, restart wake-ups) never start a clock: they inherit the
  episode's remaining time, or -- when none is left, or this instance has no
  episode state at all -- run in finish mode from their first iteration.
  Steer and in-turn redirects do not reset it; a queued human message does
  when it is consumed as its own turn.
* At 80% the agent gets a one-time notice, attached ONLY to a tool result
  produced by the current iteration that has not been sent yet
  (``agent.turn_notices``): no earlier turn, cached prefix or caller-owned dict
  is ever touched.
* At 100% ("finish mode") heavy work tools, a foreground ``delegate_task`` and
  ``process wait`` are refused for the rest of the turn; background hand-off
  and control tools, reads and the final answer stay open.
* Hard stop: 60 s after the limit or 3 model iterations into finish mode,
  whichever comes first, the loop makes one final no-tools call (the
  max-iterations summary path) at the next iteration boundary.  Tools admitted
  before the limit run to completion; their own timeouts apply.
* Whole-turn hand-off: while the limit applies, every ``delegate_task`` of the
  root runs as a background helper (and errors rather than falling back
  inline).
* Surface scope: the limit exists to hand work to a helper, so it applies only
  where a detached helper can be delivered (async-capable surfaces, or an
  API-server request carrying a wake session id).  Elsewhere it is OFF for the
  turn -- decided once at ``begin_turn``, inherited by internal turns -- and
  behaviour is exactly as before.

The limit is a backstop, never something the user hears about: the system
prompt tells an eligible root to hand clearly big jobs to a helper at the
start (``handoff_guidance_applies``), and every model-facing limit text says
not to mention the limit, timeouts or failed attempts to the user.

Scope is deliberately narrow: root conversational agents (``_delegate_depth ==
0``) on gateway messaging platforms and the interactive CLI / TUI.  Subagents,
cron, ``--oneshot`` / non-TTY ``-q``, batch / eval runners, background-review
forks and kanban workers are never limited.

Independent of ``delegation.checkpoint`` and ``agent.run_budget_seconds``; the
refusal rides the executor's existing admission funnel
(``agent.tool_executor`` / ``agent.agent_runtime_helpers.invoke_tool``).
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_LIMIT_SECONDS = 300.0
NOTICE_FRACTION = 0.8
HARD_STOP_GRACE_SECONDS = 60.0
HARD_STOP_MAX_ITERATIONS = 3
BLOCK_CODE = "foreground_time_limit_reached"
HARD_STOP_EXIT_REASON = "foreground_time_limit_hard_stop"
NOTICE_MARKER = "[SYSTEM NOTICE: time limit]"
# What every human-facing surface (activity feed, TUI / desktop tool rows) shows
# for a refused call: a neutral "skipped", never the model-facing refusal text.
SKIPPED_CODE = "skipped"


def _now() -> float:
    """The only clock this module reads; monotonic, patched by tests."""
    return time.monotonic()


# Interactive local surfaces (a human is typing to this agent).
_LOCAL_PLATFORMS = frozenset({"cli", "tui", "webui", "desktop"})
# Gateway platforms with no human in the loop for this agent run.
_AUTOMATED_GATEWAY_PLATFORMS = frozenset({"local", "webhook", "msgraph_webhook"})

# Heavy work. Everything not listed here (and not MCP / browser) stays open, so
# an unknown tool can never block the hand-off.
_HEAVY_TOOLS = frozenset({
    "terminal", "execute_code", "computer_use",
    "web_search", "web_extract",
    "write_file", "patch",
})
_HEAVY_PREFIXES = ("browser_", "mcp__")
_DELEGATE_CONTROL_ACTIONS = frozenset({"list", "steer", "stop"})


@dataclass
class _Episode:
    """One human message's time budget; survives across its internal turns."""

    started_at: float
    limit: float
    notice_sent: bool = False
    # Clock reading when finish mode began for the CURRENT turn window. ``None``
    # means "exactly at started_at + limit" (the natural expiry).
    finish_at: Optional[float] = None
    finish_iterations: int = 0
    # Decided ONCE, at the human turn that started the episode: can a detached
    # helper be delivered from this surface/session? When it cannot (the API
    # server without a wake session id) the limit is OFF for the episode and
    # every internal turn that inherits it.
    enabled: bool = True

    def elapsed(self) -> float:
        return max(0.0, _now() - self.started_at)

    def expired(self) -> bool:
        return self.elapsed() >= self.limit

    def finish_started_at(self) -> float:
        if self.finish_at is not None:
            return self.finish_at
        return self.started_at + self.limit


def normalize_limit_seconds(value: Any) -> Optional[float]:
    """Positive finite float, or ``None`` (off). ``bool`` is rejected so YAML
    ``true`` can never become a 1-second limit."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


def load_limit_seconds() -> Optional[float]:
    """Read ``delegation.foreground_time_limit_seconds`` (default 300)."""
    if os.environ.get("CLOVER_IGNORE_USER_CONFIG") == "1":
        return DEFAULT_LIMIT_SECONDS
    try:
        from clover_cli.config import load_config_readonly

        delegation = (load_config_readonly() or {}).get("delegation")
        if not isinstance(delegation, dict) or "foreground_time_limit_seconds" not in delegation:
            return DEFAULT_LIMIT_SECONDS
        return normalize_limit_seconds(delegation["foreground_time_limit_seconds"])
    except Exception:
        logger.debug("foreground time limit config unreadable; using default", exc_info=True)
        return DEFAULT_LIMIT_SECONDS


def _platform_in_scope(platform: Any) -> bool:
    if not isinstance(platform, str) or not platform.strip():
        return False
    platform = platform.strip().lower()
    if platform in _LOCAL_PLATFORMS:
        return True
    if platform == "api_server":
        return True
    if platform in _AUTOMATED_GATEWAY_PLATFORMS:
        return False
    try:
        from gateway.config import Platform

        Platform(platform)  # raises ValueError for non-gateway names
        return True
    except Exception:
        return False


def applies(agent: Any) -> bool:
    """Is this agent a root conversational agent the limit applies to?

    Unknown shapes fail OPEN (no limit): the gate must never break an agent it
    cannot identify.
    """
    try:
        if getattr(agent, "_delegate_depth", 0):
            return False
        if getattr(agent, "_subagent_id", None):
            return False
        if getattr(agent, "_is_background_review_fork", False) is True:
            return False
        # `clover chat -q` that answers once and exits, /background tasks.
        if getattr(agent, "_foreground_limit_exempt", None):
            return False
        # --oneshot / batch_runner mark themselves noninteractive roots.
        if getattr(agent, "_delegation_checkpoint_exempt", None):
            return False
        if os.environ.get("CLOVER_KANBAN_TASK"):
            return False
        if not _platform_in_scope(getattr(agent, "platform", None)):
            return False
        from agent.delegation_context import is_delegated_child_context

        return not is_delegated_child_context()
    except Exception:
        return False


def handoff_guidance_applies(agent: Any) -> bool:
    """Should this agent's system prompt carry the up-front hand-off guidance?

    The static half of the limit's own decision, evaluated once when the
    prompt is built: ``delegate_task`` is available, the agent is a root the
    limit applies to (:func:`applies`), the limit is on in config, and the
    surface always has async delivery.  The API server is left out even when
    a request carries a wake id -- that is a per-request fact, and the prompt
    must not vary turn to turn.
    """
    try:
        return bool(
            _can_delegate(agent)
            and applies(agent)
            and load_limit_seconds() is not None
            and _async_delivery_available()
        )
    except Exception:
        return False


def _internal_kinds() -> frozenset:
    # Same trusted source the delegation checkpoint keys on.
    from agent.delegation_checkpoint import PRESERVE_KINDS

    return frozenset(PRESERVE_KINDS)


# ── per-turn lifecycle ─────────────────────────────────────────────────────

def begin_turn(agent: Any, display_kind: Optional[str] = None) -> None:
    """Start (or inherit) the episode for a turn.

    ``display_kind`` is the trusted ``persist_user_display_kind`` of the turn.
    A human message starts a fresh clock.  An ``internal_notification`` turn
    never does: it inherits the instance's episode, or -- with no state (a
    fresh instance, a restart wake-up) -- starts already expired so it can read
    and answer but never mint a new budget.
    """
    agent._fg_turn_active = False
    try:
        if not applies(agent):
            agent._fg_episode = None
            return
        limit = load_limit_seconds()
        if limit is None:
            agent._fg_episode = None
            return
        now = _now()
        episode = getattr(agent, "_fg_episode", None)
        internal = display_kind is not None and display_kind in _internal_kinds()
        if internal and isinstance(episode, _Episode):
            if not episode.enabled:
                # Inherit the decision: this episode's surface cannot deliver a
                # detached helper, so the limit stays off (base behaviour).
                agent._fg_episode = episode
                return
        elif not _detached_delivery_available():
            # Scope the limit to surfaces where "hand the rest to a background
            # helper" is real. Anywhere else the turn is exactly as before:
            # no notice, no refusal, no hard stop, no inline-child bound.
            agent._fg_episode = _Episode(started_at=now, limit=limit, enabled=False)
            return
        if internal:
            if not isinstance(episode, _Episode):
                episode = _Episode(started_at=now - limit, limit=limit,
                                   notice_sent=True)  # no real elapsed time to report
            if now - episode.started_at >= episode.limit:
                # Finish mode "from the start" of THIS turn: its own grace
                # window and iteration allowance, never an already-spent one.
                episode.finish_at = now
                episode.finish_iterations = 0
        else:
            episode = _Episode(started_at=now, limit=limit)
        agent._fg_episode = episode
        agent._fg_turn_active = True
    except Exception:
        agent._fg_episode = None
        logger.debug("foreground time limit begin_turn failed", exc_info=True)


def end_turn(agent: Any) -> None:
    """The turn is over: stray tool calls between turns are not limited.

    The episode itself is kept so a following ``internal_notification`` turn
    inherits it.
    """
    try:
        agent._fg_turn_active = False
    except Exception:
        pass


def _active(agent: Any) -> Optional[_Episode]:
    episode = getattr(agent, "_fg_episode", None)
    if not isinstance(episode, _Episode) or not episode.enabled:
        return None
    if getattr(agent, "_fg_turn_active", False) is not True:
        return None
    return episode


def remaining_seconds(agent: Any) -> Optional[float]:
    """Seconds left in the active episode (``0.0`` once spent); ``None`` when
    no limit applies to this turn."""
    episode = _active(agent)
    if episode is None:
        return None
    return max(0.0, episode.limit - episode.elapsed())


def _async_delivery_available() -> bool:
    try:
        from gateway.session_context import async_delivery_supported

        return bool(async_delivery_supported())
    except Exception:
        # Unknown -> assume a helper can be delivered: the failure mode is an
        # honest error to the model, never an unbounded foreground wait.
        return True


def forces_background(agent: Any) -> bool:
    """Must every ``delegate_task`` of this turn run as a background helper?

    True whenever the limit applies: it only applies where a detached helper
    can be delivered (see :func:`_detached_delivery_available`), so no
    foreground child wait exists in a limited turn.
    """
    return _active(agent) is not None


def _detached_delivery_available() -> bool:
    """Can a detached helper's result find its way back to this session?

    Async-capable surfaces always can; the API server only when the request
    carries a raw session id the completion can wake (``gateway.wake``).
    """
    if _async_delivery_available():
        return True
    try:
        from tools.async_delegation import _current_origin_session_id

        return bool(_current_origin_session_id())
    except Exception:
        return False


# ── notice (80%) ───────────────────────────────────────────────────────────

def _duration(seconds: float) -> str:
    if seconds >= 90:
        minutes = max(1, int(round(seconds / 60.0)))
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    secs = max(1, int(round(seconds)))
    return f"{secs} second" + ("" if secs == 1 else "s")


def _can_delegate(agent: Any) -> bool:
    return "delegate_task" in (getattr(agent, "valid_tool_names", None) or ())


# Appended to every model-facing limit text: the user must never hear about the
# limit, a timeout or a failed attempt.
_QUIET_WITH_HELPER = (
    "Do not mention this limit, timeouts or failed attempts to the user; just "
    "say what the helper is doing and that it will report back."
)
_QUIET_NO_HELPER = (
    "Do not mention this limit to the user; give your best answer now."
)


def notice_text(elapsed: float, limit: float, *, can_delegate: bool = True) -> str:
    head = (
        f"{NOTICE_MARKER} This reply has been working about {_duration(elapsed)}; "
        f"the limit is {_duration(limit)}. "
    )
    if not can_delegate:
        return head + (
            "Finish now: answer with what you have and say plainly what is "
            f"left. {_QUIET_NO_HELPER}"
        )
    return head + (
        "If what's left is more than a quick finish, hand it to a background "
        "helper now with delegate_task (background=true, give it full "
        "context), tell the user what the helper is doing, and end your "
        f"reply. Otherwise finish now. {_QUIET_WITH_HELPER}"
    )


def maybe_inject_notice(agent: Any, messages: List[Dict[str, Any]]) -> bool:
    """Attach the one-time 80% notice to this iteration's unsent tool result.

    Latches only on a successful attach, so an iteration without a fresh tool
    result retries on the next one.
    """
    episode = _active(agent)
    if episode is None or episode.notice_sent:
        return False
    elapsed = episode.elapsed()
    if elapsed < NOTICE_FRACTION * episode.limit:
        return False
    from agent.turn_notices import attach_to_fresh_tool_result

    text = notice_text(elapsed, episode.limit, can_delegate=_can_delegate(agent))
    if not attach_to_fresh_tool_result(agent, messages, text):
        return False
    episode.notice_sent = True
    logger.info(
        "Foreground time limit notice injected (limit=%.0fs, elapsed=%.0fs)",
        episode.limit, elapsed,
    )
    return True


# ── refusal (100%) ─────────────────────────────────────────────────────────

def _is_heavy(name: str, args: Any) -> bool:
    if name == "terminal":
        # A background terminal is itself a hand-off (process tools poll it).
        return not (isinstance(args, dict) and args.get("background"))
    if name in _HEAVY_TOOLS:
        return True
    if name.startswith(_HEAVY_PREFIXES):
        return True
    try:
        from tools.registry import registry

        toolset = registry.get_toolset_for_tool(name)
        return bool(toolset and toolset.startswith("mcp-"))
    except Exception:
        return False


def _refusal_kind(agent: Any, name: str, args: Any) -> Optional[str]:
    if name == "delegate_task":
        # Every spawn of a limited turn is a background helper (the limit only
        # applies where one can be delivered; the root dispatcher also ignores
        # the model's own flag), so there is never a foreground delegate_task
        # to refuse.
        return None
    if name == "process":
        action = str((args or {}).get("action") or "").strip().lower() if isinstance(args, dict) else ""
        return "process_wait" if action == "wait" else None
    return "heavy" if _is_heavy(name, args) else None


def refusal(agent: Any, function_name: str, function_args: Any):
    """A blocked :class:`~agent.delegation_checkpoint.Verdict`, or ``None``.

    ``None`` means "not limited": the limit is off, not reached, out of scope,
    or the tool is one of the always-open hand-off / control tools.
    """
    episode = _active(agent)
    if episode is None or not episode.expired():
        return None
    from agent import delegation_checkpoint as dc

    name, args = dc.resolve_work_call(function_name, function_args)
    kind = _refusal_kind(agent, name, args)
    if kind is None:
        return None
    limit = episode.limit
    can_delegate = _can_delegate(agent)
    lead = f"The time limit is reached: this reply has been working longer than {_duration(limit)}, "
    if kind == "process_wait":
        what = (
            f"{lead}so 'process wait' was not run (it would block past the limit). "
            "Use process poll or log to check on it, "
        )
    else:
        what = f"{lead}so '{name}' was not run. "
        what += (
            "Hand the rest to a background helper with delegate_task "
            "(background=true, give it full context) and tell the user what "
            "the helper is doing, "
            if can_delegate
            else "Say plainly what is left, "
        )
    message = (
        what
        + "or answer now with what you have. "
        + (
            "delegate_task (background), cronjob, todo, memory, clarify, "
            "process poll/log/kill/list, read_file, search_files and skill_view "
            "still work. "
            if can_delegate
            else "cronjob, todo, memory, clarify, read_file, search_files and "
            "skill_view still work. "
        )
        + f"A follow-up message gets a fresh {_duration(limit)}. "
        + (_QUIET_WITH_HELPER if can_delegate else _QUIET_NO_HELPER)
    )
    payload: Dict[str, Any] = {
        "error": message,
        "error_type": BLOCK_CODE,
        "blocked_tool": name,
    }
    if can_delegate:
        payload["recovery"] = {"tool": "delegate_task", "background": True}
    return dc.Verdict(
        block_result=json.dumps(payload, ensure_ascii=False),
        block_code=BLOCK_CODE,
        block_message=message,
    )


def display_result(result: Any) -> Any:
    """The result a human-facing surface should show for a tool result.

    A foreground-limit refusal (``error_type == BLOCK_CODE``) is model-facing
    mechanics -- the user must never read about the limit -- so surfaces get a
    neutral ``skipped`` payload instead.  Every other result passes through
    unchanged; the model always receives the original text.
    """
    if not isinstance(result, str) or BLOCK_CODE not in result:
        return result
    try:
        data = json.loads(result)
    except ValueError:
        return result
    if isinstance(data, dict) and data.get("error_type") == BLOCK_CODE:
        return json.dumps(
            {"error": SKIPPED_CODE, "error_type": SKIPPED_CODE, "skipped": True}
        )
    return result


# ── hard stop ──────────────────────────────────────────────────────────────

def note_model_call(agent: Any) -> None:
    """The loop is about to make a provider call: count it toward the finish
    window when the limit has been reached."""
    episode = _active(agent)
    if episode is not None and episode.expired():
        episode.finish_iterations += 1


def hard_stop_due(
    agent: Any, messages: List[Dict[str, Any]], *, pending_answer: bool = False
) -> bool:
    """Should the loop end the turn with a final no-tools call right now?

    Evaluated at the iteration boundary only.  The summary request is a user
    message, so it is deferred unless the transcript tail is a tool/assistant
    message -- a composed ``pending_answer`` needs no model call and fires
    regardless of the tail.
    """
    episode = _active(agent)
    if episode is None or not episode.expired():
        return False
    due = (
        _now() >= episode.finish_started_at() + HARD_STOP_GRACE_SECONDS
        or episode.finish_iterations >= HARD_STOP_MAX_ITERATIONS
    )
    if not due:
        return False
    if pending_answer:
        return True
    last = messages[-1] if messages else None
    return isinstance(last, dict) and last.get("role") in ("tool", "assistant")


def codex_turn_budget(agent: Any) -> Optional[float]:
    """Seconds the Codex app-server turn may run under the limit, or ``None``.

    Codex executes its own tools, so Clover cannot refuse them mid-turn; the
    whole turn is bounded instead (``min(its own timeout, this)``) and
    interrupted at expiry.  A turn that starts with the episode already spent
    (an internal notification after the limit) gets the finish window.
    """
    episode = _active(agent)
    if episode is None:
        return None
    left = episode.limit - episode.elapsed()
    if left > 0:
        # Never cut an inherited turn at a sliver: the normal loop grants the
        # finish window at expiry, so does this.
        return max(left, HARD_STOP_GRACE_SECONDS)
    window = episode.finish_started_at() + HARD_STOP_GRACE_SECONDS - _now()
    return max(1.0, window)


def run_hard_stop(
    agent: Any,
    messages: List[Dict[str, Any]],
    api_call_count: int,
    *,
    pending_answer: Optional[str] = None,
    pending_previewed: bool = False,
) -> Tuple[str, str]:
    """Produce the turn's final answer; returns ``(text, exit_reason)``.

    A composed answer a verification gate withheld is preferred to another
    fallible model call (same rule as the finalizer's budget fallback).
    Otherwise this is the max-iterations summary path -- one no-tools call --
    under a distinct exit reason so ``finalize_turn`` does not run it again.
    """
    logger.info(
        "Foreground time limit hard stop (limit=%.0fs, finish_iterations=%d)",
        getattr(_active(agent), "limit", 0.0) or 0.0,
        getattr(_active(agent), "finish_iterations", 0) or 0,
    )
    if isinstance(pending_answer, str) and pending_answer.strip():
        if pending_previewed:
            agent._response_was_previewed = True
        return pending_answer, HARD_STOP_EXIT_REASON
    text = agent._handle_max_iterations(messages, api_call_count, reason="time_limit")
    return text, HARD_STOP_EXIT_REASON
