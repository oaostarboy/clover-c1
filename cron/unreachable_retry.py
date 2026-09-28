"""Automatic bounded re-runs for scheduled fires that never reached the model.

Inspired by Claude Cowork (desktop changelog v1.46388.1, 2026-09-04): "automatic re-runs
(after 5, 15, and 30 minutes) for a scheduled task that could not reach the model at all,
for example right after the computer wakes behind a VPN."

The class is deliberately narrow: the run must have FAILED with a transient network / DNS
error (``cron.scheduler._is_transient_provider_resolve_error`` — Clover's own classifier;
no ``scheduler_preflight`` module exists here) AND the agent must have completed zero API
calls. Nothing was executed and nothing was spent, so re-running cannot double a side
effect — unlike a generic failure retry, which has to answer for one-shot dispatch
accounting and mid-run side effects. Recurring jobs only.

While a retry is pending the failure notice is suppressed (Cowork re-runs silently); a run
that reaches the model — success or not — resets the ladder. Disable with
``cron.retry_unreachable: false`` in config.yaml (Clover ships no key: on by default).

Ported from Hermes's cron/unreachable_retry.py; adapted to Clover's cron/jobs.py, which has
no ``_instant_at_or_before``/``_seconds_after``/``_parse_aware`` siblings (small local
helpers replace them). ``plan_retry``/``clear_state`` are called from inside
``cron/jobs.py::_mark_job_run_locked`` — the same single locked save Hermes folds them
into — via the ``model_unreachable`` kwarg on ``mark_job_run``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from clover_time import now as _clover_now

logger = logging.getLogger("cron.scheduler")

# Cowork's ladder: re-run after 5, 15, then 30 minutes; then give up until the
# schedule's own next occurrence.
RETRY_DELAYS_SECONDS: tuple[int, ...] = (300, 900, 1800)

# Persisted on the job while a retry cycle is active: {"attempt": <1-based count of
# retries already scheduled>, "at": <ISO instant of the pending retry>, "expr": <the cron
# expression it was planned under>}. Cleared by any run that reached the model.
STATE_KEY = "unreachable_retry"


def retry_enabled(cfg: Optional[dict] = None) -> bool:
    """``cron.retry_unreachable`` — default ON (spend-neutral: only fires when zero
    model calls were made)."""
    if cfg is None:
        try:
            from clover_cli.config import load_config

            cfg = load_config() or {}
        except Exception:  # config unreadable — keep the reliability default
            return True
    cron_cfg = (cfg or {}).get("cron")
    if not isinstance(cron_cfg, dict):
        return True
    return cron_cfg.get("retry_unreachable") is not False


def is_model_unreachable_failure(exc: BaseException, agent: Any = None) -> bool:
    """True when *exc* is a transient network/DNS failure and *agent* (may be ``None``)
    never completed a model call — the run consumed nothing and executed nothing."""
    if int(getattr(agent, "session_api_calls", 0) or 0) > 0:
        return False
    from cron.scheduler import _is_transient_provider_resolve_error

    return _is_transient_provider_resolve_error(exc)


def _is_recurring(job: Dict[str, Any]) -> bool:
    return (job.get("schedule") or {}).get("kind") in {"cron", "interval"}


def _ladder_applies(job: Dict[str, Any]) -> bool:
    """The ladder's applicability gate: recurring, not paused, and enabled in config."""
    return _is_recurring(job) and job.get("state") != "paused" and retry_enabled()


def _instant_at_or_before(a: datetime, b: datetime) -> bool:
    return a <= b


def _seconds_after(now: datetime, seconds: int) -> datetime:
    return now + timedelta(seconds=seconds)


def _parse_instant(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    from cron.jobs import _ensure_aware  # late: jobs imports this module's helpers

    try:
        return _ensure_aware(datetime.fromisoformat(value))
    except Exception:
        return None


def _ladder_instant(job: Dict[str, Any], natural_next: Optional[datetime],
                    now: datetime) -> Optional[datetime]:
    """The ladder instant ``plan_retry`` parks for this flagged failure, or None when it
    parks nothing: not recurring, paused, disabled, ladder exhausted, or the schedule's
    own *natural_next* fires at or before the rung."""
    attempt = int((job.get(STATE_KEY) or {}).get("attempt") or 0)
    if attempt >= len(RETRY_DELAYS_SECONDS) or not _ladder_applies(job):
        return None
    retry_dt = _seconds_after(now, RETRY_DELAYS_SECONDS[attempt])
    if natural_next is not None and _instant_at_or_before(natural_next, retry_dt):
        return None  # the natural occurrence IS the retry
    return retry_dt


def will_retry(job: Dict[str, Any]) -> bool:
    """True iff ``plan_retry`` will park a re-run for this flagged failure, so the scheduler
    may hold the interim failure notice. Called BEFORE ``mark_job_run``, so it computes the
    natural next occurrence independently rather than reading ``job["next_run_at"]``."""
    repeat = job.get("repeat") or {}
    times = repeat.get("times")
    if times is not None and times > 0 and int(repeat.get("completed") or 0) + 1 >= times:
        return False  # the job completes on this run; plan_retry never runs

    from cron.jobs import compute_next_run

    now = _clover_now()
    natural_next = _parse_instant(compute_next_run(job.get("schedule") or {}, now.isoformat()))
    if natural_next is None:
        # The natural occurrence is uncomputable (e.g. croniter missing): mark_job_run
        # leaves the record state=error — terminal — so plan_retry never runs either.
        return False
    return _ladder_instant(job, natural_next, now) is not None


def clear_state(job: Dict[str, Any]) -> None:
    """A run reached the model (any outcome): the ladder resets."""
    job.pop(STATE_KEY, None)


def is_retry_fire(job: Dict[str, Any], next_run: str) -> bool:
    """True for the exact ladder instant parked by ``plan_retry`` (off the cron lattice).

    The expression fingerprint keeps a direct ``jobs.json`` schedule edit from inheriting the
    exception, as in ``cron.quota_hold.is_recovery_fire``-style guards.
    """
    state = job.get(STATE_KEY) or {}
    return state.get("at") == next_run and state.get("expr") == (job.get("schedule") or {}).get("expr")


def plan_retry(job: Dict[str, Any]) -> bool:
    """Mutates *job* in place: pulls ``next_run_at`` earlier to the ladder instant when
    that is sooner than the schedule's own natural occurrence (already computed by
    ``mark_job_run`` and read off ``job["next_run_at"]``). Exhausted or inapplicable
    cycles clear state and leave the schedule untouched. Returns True when a retry was
    scheduled. Caller persists the mutation."""
    natural_next = _parse_instant(job.get("next_run_at"))
    now = _clover_now()
    retry_dt = _ladder_instant(job, natural_next, now)
    attempt = int((job.get(STATE_KEY) or {}).get("attempt") or 0)
    if retry_dt is None:
        if attempt >= len(RETRY_DELAYS_SECONDS) and _ladder_applies(job):
            # Ladder exhausted: fall back to the natural schedule and reset so the NEXT
            # occurrence gets a fresh ladder if the network is still down.
            logger.warning(
                "Job '%s': model unreachable after %d automatic re-runs — waiting for the "
                "scheduled occurrence at %s",
                job.get("name", job.get("id", "?")), attempt, job.get("next_run_at"))
        clear_state(job)
        return False
    delay = RETRY_DELAYS_SECONDS[attempt]
    retry_at = retry_dt.isoformat()
    job[STATE_KEY] = {"attempt": attempt + 1, "at": retry_at, "expr": (job.get("schedule") or {}).get("expr")}
    job["next_run_at"] = retry_at
    if job.get("state") != "paused":
        job["state"] = "scheduled"
    logger.info(
        "Job '%s': model unreachable with zero API calls — automatic re-run %d/%d in %ds "
        "(at %s)",
        job.get("name", job.get("id", "?")), attempt + 1, len(RETRY_DELAYS_SECONDS),
        delay, retry_at)
    return True
