"""Every terminal ``dropped`` on an async delegation also lands in the inbox.

Real durable ``async_delegations`` rows and a real ``SessionDB`` in the same
isolated ``CLOVER_HOME``. A result that is dropped (attempt cap, replay-age
cap, undeliverable) must leave an inbox ``dropped`` record with a pending user
notice, never only a bare ``delivery_state='dropped'``.
"""

from __future__ import annotations

import logging
import queue
import time

import pytest

from clover_state import SessionDB
from tools import async_delegation as ad
from tools.process_registry import process_registry

KEY = "agent:main:telegram:dm:100:555"


@pytest.fixture(autouse=True)
def _clean_state():
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _completed_delegation(goal="Audit the repo"):
    # Dispatched from a gateway turn on telegram chat 100 / topic 555: the route
    # is captured from the turn's session vars and persisted on the row.
    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(
        platform="telegram", chat_id="100", chat_type="dm", thread_id="555",
        session_key=KEY, user_id="u1",
    )
    try:
        handle = ad.dispatch_async_delegation(
            goal=goal, context=None, toolsets=None, role="leaf", model="m",
            session_key=KEY, parent_session_id="child-pin",
            runner=lambda: {"status": "completed", "summary": "found 3 issues"},
        )
    finally:
        clear_session_vars(tokens)
    did = handle["delegation_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process_registry.completion_queue.empty():
            time.sleep(0.02)
            continue
        if process_registry.completion_queue.get_nowait().get("delegation_id") == did:
            return did
    raise AssertionError("delegation never completed")


def _inbox(did):
    db = SessionDB()
    try:
        return db.inbox_get(f"deleg:{did}")
    finally:
        db.close()


def _assert_noticed_drop(did, reason):
    rec = _inbox(did)
    assert rec is not None, "dropped delegation left no inbox record"
    assert (rec["state"], rec["notice_state"], rec["kind"]) == ("dropped", "pending", "delegation")
    assert rec["drop_reason"] == reason
    assert (rec["profile"], rec["platform"], rec["chat_id"], rec["thread_id"]) == (
        "default", "telegram", "100", "555",
    )
    assert rec["session_key"] == KEY
    assert rec["owner_root_id"] == "child-pin"
    assert rec["title"] == "Audit the repo"
    assert "found 3 issues" in rec["payload_json"]
    assert ad.get_durable_delegation(did)["delivery_state"] == "dropped"


def test_attempt_cap_exhaustion_records_and_notices():
    did = _completed_delegation()
    for _ in range(ad._MAX_DELIVERY_ATTEMPTS):
        claim = f"c-{time.monotonic_ns()}"
        assert ad.claim_completion_delivery(did, claim)
        ad.release_completion_delivery(did, claim)

    _assert_noticed_drop(did, "delivery_attempts_exhausted")


def test_below_the_cap_a_release_records_nothing():
    did = _completed_delegation()
    claim = "c-1"
    assert ad.claim_completion_delivery(did, claim)
    ad.release_completion_delivery(did, claim)

    assert _inbox(did) is None
    assert ad.get_durable_delegation(did)["delivery_state"] == "pending"


def test_replay_age_cap_records_and_notices(monkeypatch):
    did = _completed_delegation()
    monkeypatch.setattr(ad, "_MAX_COMPLETION_REPLAY_AGE_S", -1.0)

    assert ad.restore_undelivered_completions(queue.Queue()) == 0

    _assert_noticed_drop(did, "stale_replay")


def test_drop_completion_delivery_records_and_notices():
    did = _completed_delegation()
    assert ad.claim_completion_delivery(did, "c-1")

    assert ad.drop_completion_delivery(did, "c-1") is True

    _assert_noticed_drop(did, "undeliverable")


def test_an_earlier_gateway_record_is_not_overwritten():
    did = _completed_delegation()
    db = SessionDB()
    try:
        db.inbox_put({
            "key": f"deleg:{did}", "profile": "default", "platform": "telegram",
            "chat_id": "100", "thread_id": "555", "session_key": KEY,
            "owner_root_id": "resolved-root", "kind": "delegation", "wake": 0,
            "title": "Audit the repo", "payload_json": "{}", "shown_to_user": 0,
        })
        db.inbox_drop(f"deleg:{did}", "unowned:owned_elsewhere")
    finally:
        db.close()
    assert ad.claim_completion_delivery(did, "c-1")

    ad.drop_completion_delivery(did, "c-1")

    rec = _inbox(did)
    assert (rec["owner_root_id"], rec["drop_reason"]) == ("resolved-root", "unowned:owned_elsewhere")


def test_inbox_failure_only_warns(monkeypatch, caplog):
    did = _completed_delegation()
    assert ad.claim_completion_delivery(did, "c-1")

    def boom(*_a, **_k):
        raise RuntimeError("inbox unavailable")

    monkeypatch.setattr(SessionDB, "inbox_put", boom)
    with caplog.at_level(logging.WARNING, logger="tools.async_delegation"):
        assert ad.drop_completion_delivery(did, "c-1") is True

    assert ad.get_durable_delegation(did)["delivery_state"] == "dropped"
    assert any("session inbox" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Startup repair: the window between the drop commit and the inbox write
# ---------------------------------------------------------------------------
def _crash_after_drop_commit(monkeypatch, did):
    """The process dies right after ``drop_completion_delivery`` commits."""
    assert ad.claim_completion_delivery(did, "c-1")
    with monkeypatch.context() as m:
        def die(_drops):
            raise SystemExit("process died after the drop committed")

        m.setattr(ad, "_record_drops_in_inbox", die)
        with pytest.raises(SystemExit):
            ad.drop_completion_delivery(did, "c-1")
    assert ad.get_durable_delegation(did)["delivery_state"] == "dropped"
    assert _inbox(did) is None


def test_crash_between_drop_commit_and_inbox_write_is_repaired_once_at_startup(monkeypatch):
    did = _completed_delegation()
    _crash_after_drop_commit(monkeypatch, did)

    assert ad.repair_dropped_without_inbox() == 1
    _assert_noticed_drop(did, "repaired_after_drop")
    assert ad.repair_dropped_without_inbox() == 0  # exactly once
    db = SessionDB()
    try:
        assert len(db.inbox_for_route("default", "telegram", "100", "555")) == 1
    finally:
        db.close()


class _Clock:
    """Wall clock for ``tools.async_delegation`` that a test can move."""

    def __init__(self):
        self.now = time.time()

    def __getattr__(self, name):
        return getattr(time, name)

    def time(self):
        return self.now


DAY = 24 * 3600.0


def test_repair_has_no_age_limit_after_the_rollout_marker(monkeypatch):
    """Astra's probe: the process crashes after the drop commit and stays down
    eight days. Eligibility is the rollout boundary, not a rolling age."""
    clock = _Clock()
    monkeypatch.setattr(ad, "time", clock)
    did = _completed_delegation()          # first P1 touch of this ledger writes the marker
    _crash_after_drop_commit(monkeypatch, did)

    clock.now += 8 * DAY

    assert ad.repair_dropped_without_inbox() == 1
    _assert_noticed_drop(did, "repaired_after_drop")


def test_a_pre_marker_drop_is_left_to_the_reviewed_backfill(monkeypatch):
    did_old = _completed_delegation("Old audit")
    _crash_after_drop_commit(monkeypatch, did_old)
    with ad._DB_LOCK, ad._transaction() as conn:
        marker = float(conn.execute(
            "SELECT value FROM state_meta WHERE key = ?", (ad._MIRROR_MARKER_KEY,)
        ).fetchone()[0])
        conn.execute(
            "UPDATE async_delegations SET updated_at = ? WHERE delegation_id = ?",
            (marker - 10 * DAY, did_old),
        )
    did_new = _completed_delegation("New audit")
    _crash_after_drop_commit(monkeypatch, did_new)

    assert ad.repair_dropped_without_inbox() == 1
    assert _inbox(did_old) is None, "rows from before the rollout are the Ops backfill"
    assert _inbox(did_new) is not None


def test_the_marker_is_written_once_and_never_moves(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(ad, "time", clock)
    _completed_delegation()

    def marker():
        with ad._DB_LOCK, ad._transaction() as conn:
            return conn.execute(
                "SELECT value FROM state_meta WHERE key = ?", (ad._MIRROR_MARKER_KEY,)
            ).fetchone()[0]

    first = marker()
    clock.now += 30 * DAY
    _completed_delegation("again")
    ad.repair_dropped_without_inbox()
    assert marker() == first


@pytest.mark.asyncio
async def test_runtime_inbox_failure_is_repaired_by_the_periodic_sweep_after_eight_days(monkeypatch):
    """Fable's probe: the inbox write fails at runtime (no crash), the gateway
    stays up eight days; the periodic notice sweep still mirrors and announces it."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from clover_state import AsyncSessionDB
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner

    clock = _Clock()
    monkeypatch.setattr(ad, "time", clock)
    did = _completed_delegation()
    assert ad.claim_completion_delivery(did, "c-1")
    with monkeypatch.context() as m:
        def boom(*_a, **_k):
            raise RuntimeError("inbox unavailable")

        m.setattr(SessionDB, "inbox_put", boom)
        assert ad.drop_completion_delivery(did, "c-1") is True
    assert _inbox(did) is None
    clock.now += 8 * DAY

    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    db = SessionDB()
    try:
        runner = object.__new__(GatewayRunner)
        runner._running = True
        runner.config = GatewayConfig()
        runner._session_db = AsyncSessionDB(db)
        runner.adapters = {Platform.TELEGRAM: adapter}

        await runner._inbox_notice_sweep_all()
        await runner._inbox_notice_sweep_all()  # idempotent: nothing new

        rec = db.inbox_get(f"deleg:{did}")
        assert rec is not None and rec["notice_state"] == "sent"
        assert rec["drop_reason"] == "repaired_after_drop"
        assert adapter.send.await_count == 1
    finally:
        db.close()


def test_a_pending_inbox_row_left_by_a_failed_drop_is_completed(monkeypatch):
    did = _completed_delegation()
    assert ad.claim_completion_delivery(did, "c-1")
    with monkeypatch.context() as m:
        def boom(self, *_a, **_k):
            raise RuntimeError("drop failed after put")

        m.setattr(SessionDB, "inbox_drop", boom)
        ad.drop_completion_delivery(did, "c-1")
    assert _inbox(did)["state"] == "pending"

    assert ad.repair_dropped_without_inbox() == 1

    assert _inbox(did)["state"] == "dropped"


@pytest.mark.asyncio
async def test_gateway_startup_repairs_dropped_delegations(monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner

    did = _completed_delegation()
    _crash_after_drop_commit(monkeypatch, did)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()

    await runner._repair_dropped_delegations()

    _assert_noticed_drop(did, "repaired_after_drop")
