"""Cowork-inspired bounded automatic re-runs for cron fires that never reached the model.

Contract (cron/unreachable_retry.py): a recurring job whose run fails with a transient
network/DNS error before ANY model call gets its ``next_run_at`` pulled earlier along a
bounded ladder (5/15/30 min); a run that reaches the model resets the ladder, and the
ladder never fires past its last rung.

Ported from Hermes's tests/cron/test_unreachable_retry.py. Clover has no separate
scheduler_preflight module — the classifier is cron.scheduler._is_transient_provider_resolve_error,
reused as-is (cron/unreachable_retry.py wraps it). CLOVER_HOME isolation comes from the
global autouse _isolate_clover_home fixture (tests/conftest.py); no per-file HOME fixture
needed.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import cron.jobs as cron_jobs
import cron.scheduler as sched
from cron import unreachable_retry as ur
from cron.jobs import create_job, get_due_jobs, get_job, load_jobs, mark_job_run, save_jobs


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_unreachable_failure_pulls_next_run_earlier_then_ladder_exhausts(monkeypatch):
    """Failed-unreachable runs re-fire on the 5/15/30-minute ladder instead of waiting a
    full period, and the ladder stops after its last rung (falls back to the schedule). A
    cron job's ladder instant is off its lattice yet must be due, not re-anchored as a stale
    expression edit."""
    # Interval, not a cron expression: the natural next fire is always a full day out. A
    # fixed clock time ("0 3 * * *") makes the 30-minute rung land past the natural fire
    # in the half hour before it, and plan_retry rightly yields to the schedule (CI red).
    job = create_job("nightly report", "every 24h")
    job_id = job["id"]

    now = datetime.now(timezone.utc)
    for i, delay in enumerate(ur.RETRY_DELAYS_SECONDS):
        assert mark_job_run(job_id, False, "ConnectError: dns", model_unreachable=True)
        j = get_job(job_id)
        nxt = datetime.fromisoformat(j["next_run_at"])
        # Pulled to roughly now + ladder delay, far before the daily occurrence.
        assert timedelta(0) < nxt - now <= timedelta(seconds=delay + 120), (
            f"attempt {i}: expected retry ~{delay}s out, got {nxt - now}")
        assert j[ur.STATE_KEY]["attempt"] == i + 1

    # Ladder exhausted: the next unreachable failure keeps the natural schedule.
    assert mark_job_run(job_id, False, "ConnectError: dns", model_unreachable=True)
    j = get_job(job_id)
    assert j.get(ur.STATE_KEY) is None
    assert datetime.fromisoformat(j["next_run_at"]) - now > timedelta(hours=1)

    pinned = datetime(2026, 9, 18, 12, 1, tzinfo=timezone.utc)
    monkeypatch.setattr("cron.jobs._clover_now", lambda: pinned)
    monkeypatch.setattr(ur, "_clover_now", lambda: pinned)
    weekly = create_job("weekly digest", "0 12 * * 5")
    assert mark_job_run(weekly["id"], False, "ConnectError: dns", model_unreachable=True)
    retry_at = datetime.fromisoformat(get_job(weekly["id"])["next_run_at"])
    assert retry_at == pinned + timedelta(seconds=ur.RETRY_DELAYS_SECONDS[0])
    monkeypatch.setattr("cron.jobs._clover_now", lambda: retry_at + timedelta(seconds=1))
    assert weekly["id"] in {due["id"] for due in get_due_jobs()}

    # A direct jobs.json expression edit while a retry is parked re-anchors without firing.
    assert mark_job_run(weekly["id"], False, "ConnectError: dns", model_unreachable=True)
    retry_at = datetime.fromisoformat(get_job(weekly["id"])["next_run_at"])
    jobs = load_jobs()
    next(j for j in jobs if j["id"] == weekly["id"])["schedule"]["expr"] = "0 9 * * 1"
    save_jobs(jobs)
    monkeypatch.setattr("cron.jobs._clover_now", lambda: retry_at + timedelta(seconds=1))
    assert weekly["id"] not in {due["id"] for due in get_due_jobs()}


def test_will_retry_mirrors_plan_retry_yield():
    """``will_retry`` answers True only when ``plan_retry`` would park a re-run. Called after
    ``mark_job_run`` — valid, the predictor reads only persisted job state."""
    fast = create_job("fast poll", "every 2m")
    assert mark_job_run(fast["id"], False, "ConnectError: dns", model_unreachable=True)
    j = get_job(fast["id"])
    assert j is not None
    assert j.get(ur.STATE_KEY) is None, "2m cadence beats the 5m rung: plan_retry yields"
    assert ur.will_retry(j) is False, "yielded: no re-run is scheduled, notice must go out"

    slow = create_job("nightly report", "every 24h")
    assert mark_job_run(slow["id"], False, "ConnectError: dns", model_unreachable=True)
    js = get_job(slow["id"])
    assert js is not None
    assert js[ur.STATE_KEY]["attempt"] == 1
    assert ur.will_retry(js) is True, "5m rung beats the 24h cadence: re-run is scheduled"

    mid = create_job("ten minute sync", "every 10m")
    assert mark_job_run(mid["id"], False, "ConnectError: dns", model_unreachable=True)
    jm = get_job(mid["id"])
    assert jm is not None
    # 10m cadence beats the 15m and 30m rungs: the ladder can never climb past attempt 1,
    # so the exhaustion escape is unreachable. At attempt 1 the next (15m) rung loses to the
    # 10m run, so this failure's notice goes out rather than being held for a retry.
    assert jm[ur.STATE_KEY]["attempt"] == 1
    assert ur.will_retry(jm) is False, "10m cadence beats the 15m rung: yielded, notice goes out"

    last = create_job("final run", "every 24h", repeat=1)
    assert ur.will_retry(get_job(last["id"])) is False, "final finite repeat completes the job"


def test_reaching_the_model_resets_ladder_and_oneshots_never_retry():
    """Any run that reached the model clears retry state; one-shots (pre-claimed
    dispatch, at-most-times #38758) never enter the ladder."""
    job = create_job("hourly sync", "every 12h")
    job_id = job["id"]
    assert mark_job_run(job_id, False, "ConnectError: dns", model_unreachable=True)
    assert get_job(job_id)[ur.STATE_KEY]["attempt"] == 1

    # A normal failed run (model reached) resets the ladder and stays on schedule.
    assert mark_job_run(job_id, False, "agent error")
    j = get_job(job_id)
    assert j.get(ur.STATE_KEY) is None
    now = datetime.now(timezone.utc)
    assert datetime.fromisoformat(j["next_run_at"]) - now > timedelta(hours=11)

    # One-shot: flag is ignored, no retry state, no resurrection.
    once = create_job("one shot", _iso(datetime.now(timezone.utc) + timedelta(minutes=1)))
    assert mark_job_run(once["id"], False, "ConnectError: dns", model_unreachable=True)
    remaining = get_job(once["id"])
    assert remaining is None or remaining.get(ur.STATE_KEY) is None


def _run_one_job_with_transient_failure(tmp_path, deliveries, *, api_calls=0):
    """Create a real recurring job in a throwaway cron store and drive a real
    cron.scheduler.run_one_job() tick whose agent's first model call fails
    with a transient DNS/connect error and made ``api_calls`` calls (0 = never
    reached the model). Mirrors test_cron_incidents.py's harness so the real
    run_job -> mark_job_run wiring is exercised, not just the store functions.
    Returns the job id."""
    fake_db = MagicMock()

    def fake_deliver(jb, content, adapters=None, loop=None):
        deliveries.append(content)
        return None

    with cron_jobs.use_cron_store(tmp_path), \
         patch("cron.scheduler._clover_home", tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("clover_cli.env_loader.load_clover_dotenv"), \
         patch("clover_cli.env_loader.reset_secret_source_cache"), \
         patch("clover_state.SessionDB", return_value=fake_db), \
         patch("tools.mcp_tool.discover_mcp_tools", return_value=[]), \
         patch("clover_cli.runtime_provider.resolve_runtime_provider",
               return_value={
                   "api_key": "test-key",
                   "base_url": "https://example.invalid/v1",
                   "provider": "openrouter",
                   "api_mode": "chat_completions",
               }), \
         patch.object(sched, "_deliver_result", side_effect=fake_deliver), \
         patch("run_agent.AIAgent") as mock_agent_cls:
        job = create_job("nightly report", "every 24h")
        mock_agent = MagicMock()
        mock_agent.session_api_calls = api_calls
        mock_agent.run_conversation.side_effect = ConnectionError(
            "Temporary failure in name resolution"
        )
        mock_agent_cls.return_value = mock_agent
        sched.run_one_job(dict(job))
        return job["id"], get_job(job["id"])


def test_run_one_job_wires_model_unreachable_through_to_mark_job_run(tmp_path):
    """End-to-end: a real run_one_job() tick whose model call fails with a
    transient network error and zero completed API calls (a) suppresses the
    failure notice and (b) pulls next_run_at onto the retry ladder — proving
    the out-parameter wiring between run_job and _run_one_job_body actually
    reaches mark_job_run, not just the unit-level jobs.py contract."""
    deliveries: list = []
    now = datetime.now(timezone.utc)
    _job_id, updated = _run_one_job_with_transient_failure(tmp_path, deliveries, api_calls=0)

    assert deliveries == [], "a pending retry must stay silent, not alert on every blip"
    assert updated.get(ur.STATE_KEY) is not None, "the retry ladder must have armed"
    assert updated[ur.STATE_KEY]["attempt"] == 1
    nxt = datetime.fromisoformat(updated["next_run_at"])
    assert timedelta(0) < nxt - now <= timedelta(seconds=ur.RETRY_DELAYS_SECONDS[0] + 120)


def test_run_one_job_with_prior_api_calls_never_retries(tmp_path):
    """The same transient network error, but the agent already made a model
    call before it failed: this is a real failure, not an unreachable fire —
    it must deliver the normal failure notice and NOT arm the retry ladder."""
    deliveries: list = []
    _job_id, updated = _run_one_job_with_transient_failure(tmp_path, deliveries, api_calls=1)

    assert updated.get(ur.STATE_KEY) is None, "any API call means this is not an unreachable fire"
    assert len(deliveries) == 1, "a real failure with progress must alert normally"


def test_is_model_unreachable_failure_requires_zero_api_calls():
    """The narrow gate: a transient network error only counts as 'unreachable' when the
    agent made zero API calls. Any progress means real work may have happened, so a
    generic failure retry (never a bounded ladder) is the correct mechanism instead."""
    class _FakeAgent:
        def __init__(self, calls):
            self.session_api_calls = calls

    transient = ConnectionError("Temporary failure in name resolution")

    assert ur.is_model_unreachable_failure(transient, None) is True
    assert ur.is_model_unreachable_failure(transient, _FakeAgent(0)) is True
    assert ur.is_model_unreachable_failure(transient, _FakeAgent(1)) is False

    non_transient = ValueError("invalid API key")
    assert ur.is_model_unreachable_failure(non_transient, None) is False
