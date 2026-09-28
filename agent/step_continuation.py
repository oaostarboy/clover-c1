"""Keep a worker going until the task is actually done.

Two ways a worker stops early:

1. **Out of steps.** It hits its iteration cap mid-task and hands back a
   partial summary. The fix: refresh its budget and let it resume from its
   own transcript.
2. **Quits with steps left.** It still has budget, but its final answer
   admits the job isn't finished ("I didn't finish", "not verified"). The
   fix: one nudge to keep going, or to name exactly what blocks it.

Both are bounded by ``delegation.auto_continue`` (default 2). Every resume
must make progress (call at least one tool), or the loop stops. Used by
``delegate_task`` children and ``clover -z`` one-shot workers.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_AUTO_CONTINUE = 2

AUTO_CONTINUE_MESSAGE = (
    "[SYSTEM NOTICE: step budget refreshed] You ran out of tool-calling steps "
    "before finishing, and your step budget has been reset. Continue the "
    "ORIGINAL task from where you stopped. Do not redo finished work or "
    "repeat your previous summary. When the whole task is done, give the "
    "final answer in the format the task asked for."
)

UNFINISHED_NUDGE_MESSAGE = (
    "[SYSTEM NOTICE: task not finished] Your answer says the task is not "
    "finished, and you still have steps left. Keep going and finish the "
    "remaining items now. Only stop early if something is truly blocked "
    "(missing access, a decision only the user can make, or a hard error you "
    "cannot work around). In that case, name exactly what is blocked and why. "
    "Then give the final answer in the format the task asked for."
)

# A final answer that admits the work is unfinished. Kept narrow: phrases
# workers actually use when they stop early, not any use of "incomplete".
_UNFINISHED_RE = re.compile(
    r"(?ix)"
    r"\b(?:i\s+)?(?:did\s*n[o']?t|did\s+not|have\s*n[o']?t|have\s+not|could\s*n[o']?t|could\s+not)"
    r"\s+(?:yet\s+)?(?:finish|complete|get\s+to|verify|run\s+the\s+(?:required\s+)?(?:\w+\s+)?(?:tests?|set|suite|checks?))\b"
    r"|\b(?:this|the\s+(?:task|wave|work|job|unit))\s+is\s+not\s+(?:yet\s+)?(?:complete|finished|done)\b"
    r"|\bnot\s+(?:fully\s+)?(?:verified|complete)\b"
    r"|\bremains?\s+(?:incomplete|unfinished)\b"
    r"|\bstill\s+needs?\s+(?:finishing|to\s+be\s+(?:finished|done|completed))\b"
    r"|\bcan(?:'|no)?t\s+honestly\s+report\s+(?:this|it)\s+as\s+(?:an?\s+)?(?:complete|done)"
)


def auto_continue_limit(cfg: Optional[dict] = None) -> int:
    """``delegation.auto_continue`` as an int (0 disables)."""
    if cfg is None:
        try:
            from clover_cli.config import load_config

            cfg = (load_config() or {}).get("delegation") or {}
        except Exception:
            cfg = {}
    raw = (cfg or {}).get("auto_continue", DEFAULT_AUTO_CONTINUE)
    if isinstance(raw, bool):
        return DEFAULT_AUTO_CONTINUE if raw else 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_AUTO_CONTINUE


def stopped_on_step_budget(result: Any) -> bool:
    """True only when a run ended because it ran out of iterations."""
    if not isinstance(result, dict):
        return False
    if result.get("interrupted") or result.get("failed"):
        return False
    reason = str(result.get("turn_exit_reason") or "")
    return reason.startswith("max_iterations_reached") or reason == "budget_exhausted"


def admits_unfinished(text: Any) -> bool:
    """True when a final answer says the task isn't done."""
    return isinstance(text, str) and bool(_UNFINISHED_RE.search(text))


def quit_with_steps_left(result: Any, agent: Any = None) -> bool:
    """Finished normally, still had budget, but admits the work is unfinished."""
    if not isinstance(result, dict):
        return False
    if result.get("interrupted") or result.get("failed") or stopped_on_step_budget(result):
        return False
    if not str(result.get("turn_exit_reason") or "").startswith("text_response"):
        return False
    budget = getattr(agent, "iteration_budget", None)
    remaining = getattr(budget, "remaining", None)
    if isinstance(remaining, int) and remaining <= 0:
        return False
    return admits_unfinished(result.get("final_response"))


def made_tool_progress(result: Dict[str, Any], prior_len: int) -> bool:
    """True when messages after ``prior_len`` include a tool call."""
    msgs = result.get("messages")
    if not isinstance(msgs, list):
        return False
    for m in msgs[prior_len:]:
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
            return True
    return False


def refresh_budget(agent: Any) -> None:
    """Give the agent a fresh iteration budget of its original size."""
    budget = getattr(agent, "iteration_budget", None)
    max_total = getattr(budget, "max_total", None) or getattr(agent, "max_iterations", None)
    if not max_total:
        return
    try:
        from agent.iteration_budget import IterationBudget

        agent.iteration_budget = IterationBudget(int(max_total))
    except Exception:
        logger.debug("Could not refresh iteration budget", exc_info=True)
    try:
        agent._budget_grace_call = False
    except Exception:
        pass


def needs_continuation(result: Any, agent: Any = None) -> Optional[str]:
    """Which continuation a run needs: ``"budget"``, ``"unfinished"`` or None."""
    if stopped_on_step_budget(result):
        return "budget"
    if quit_with_steps_left(result, agent):
        return "unfinished"
    return None


def continue_until_done(
    agent: Any,
    result: Dict[str, Any],
    *,
    limit: int,
    run: Callable[[str, list], Dict[str, Any]],
    on_continue: Optional[Callable[[str, int, int], None]] = None,
) -> Dict[str, Any]:
    """Resume ``agent`` up to ``limit`` times while it stopped early.

    ``run(message, history)`` performs one more leg and returns its result.
    Returns the merged result with ``auto_continuations`` and
    ``continuation_kinds`` set.
    """
    kinds: list = []
    leg_start = 0
    unfinished_nudges = 0
    while len(kinds) < limit:
        kind = needs_continuation(result, agent)
        if kind is None:
            break
        # One "keep going" nudge is enough; a second admission means it is
        # genuinely blocked, and the answer should say why.
        if kind == "unfinished" and unfinished_nudges >= 1:
            break
        history = result.get("messages")
        if not isinstance(history, list) or not history:
            break
        if kinds and not made_tool_progress(result, leg_start):
            break  # the last resume did no real work; another won't help
        if kind == "budget":
            refresh_budget(agent)
            message = AUTO_CONTINUE_MESSAGE
        else:
            unfinished_nudges += 1
            message = UNFINISHED_NUDGE_MESSAGE
        kinds.append(kind)
        if on_continue is not None:
            try:
                on_continue(kind, len(kinds), limit)
            except Exception:
                pass
        try:
            nxt = run(message, history)
        except Exception as exc:
            logger.warning("Auto-continue leg failed: %s", exc)
            break
        if not isinstance(nxt, dict):
            break
        try:
            api_total = int(result.get("api_calls", 0) or 0) + int(nxt.get("api_calls", 0) or 0)
        except (TypeError, ValueError):
            api_total = nxt.get("api_calls", 0)
        if not (nxt.get("final_response") or "").strip():
            nxt["final_response"] = result.get("final_response") or ""
        nxt["api_calls"] = api_total
        leg_start = len(history)
        result = nxt
    result["auto_continuations"] = len(kinds)
    result["continuation_kinds"] = kinds
    return result
