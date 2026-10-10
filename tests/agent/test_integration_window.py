"""Completion integration windows (Patch 3, part 2).

A handed-off request gets a bounded verification window only when a durable,
claimed terminal receipt of ITS OWN job arrives on a trusted internal turn.
The real async registry and its durable table (scratch ``CLOVER_HOME``) are
used and ``begin_turn``'s receipt probe is left at its real default; only the
background job's work function is a stand-in.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

import tools.async_delegation as ad
from agent import delegation_checkpoint as dc
from tools.process_registry import process_registry

INTERNAL = "internal_notification"


@pytest.fixture(autouse=True)
def _clean_registry():
    ad._reset_for_tests()
    _drain_queue()
    yield
    # Tests release their gates in ``finally``; a released job still enqueues
    # its batch-join event from the worker thread.  ``_reset_for_tests`` only
    # shuts the executor down without waiting, so that event could land AFTER
    # the drain and be mistaken for the next test's first event (flaky CI
    # shard failure).  Join the workers (finite: every gate is already open)
    # so nothing can publish past this point.
    executor = ad._executor
    if executor is not None:
        executor.shutdown(wait=True)
    ad._reset_for_tests()
    _drain_queue()


def _drain_queue():
    while True:
        try:
            process_registry.completion_queue.get_nowait()
        except Exception:
            return


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _root():
    return SimpleNamespace(
        valid_tool_names={"todo", "delegate_task"},
        platform="cli",
        _delegate_depth=0,
        _subagent_id=None,
        session_id="root-session",
        _interrupt_requested=False,
        _active_children=[],
        _active_children_lock=None,
        _delegation_checkpoint=dc.DelegationCheckpoint(clock=_Clock()),
    )


def _cp(root):
    return root._delegation_checkpoint


def _work(root, call_id="w"):
    verdict = dc.admit(root, "write_file", {}, call_id)
    if verdict.admission is not None:
        _cp(root).finish(verdict.admission)
    return verdict


def _result(index, summary="ok"):
    return {"task_index": index, "status": "completed", "summary": summary,
            "api_calls": 1, "duration_seconds": 0.0, "model": "m",
            "exit_reason": "completed"}


def _dispatch(goals, runner):
    res = ad.dispatch_async_delegation_batch(
        goals=list(goals), context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=8,
    )
    assert res["status"] == "dispatched"
    return res["delegation_id"]


def _single_job(release=None):
    def runner():
        if release is not None:
            release.wait(30)
        return {"results": [_result(0)]}

    return _dispatch(["Build and verify the explainer."], runner)


def _wait_state(delegation_id, *, terminal=True, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = ad.get_durable_delegation(delegation_id)
        done = row is not None and row["state"] not in ("running", "finalizing")
        if done == terminal:
            return row
        time.sleep(0.02)
    raise AssertionError(f"{delegation_id} never reached terminal={terminal}")


def _next_event(timeout=10.0):
    return process_registry.completion_queue.get(timeout=timeout)


def _claim(evt):
    claim = ad.claim_event_delivery(evt, "gateway-test")
    assert claim, "the durable row must be claimable"
    return claim


def _hand_off(root, delegation_id, goals):
    cp = _cp(root)
    assert cp.declare("delegate", "Long phase.")
    assert cp.ticket().accept_handoff(
        delegation_id=delegation_id, goals=list(goals), subagent_ids=["sa-1"]
    )
    assert cp.phase == dc.PHASE_HANDED_OFF


def _internal_turn(root):
    dc.begin_turn(root, INTERNAL)


def _fanout(n, gates):
    """A fan-out whose children publish their own rows, each behind a gate."""
    holder = {}

    def runner():
        results = []
        for i in range(n):
            gates[i].wait(30)
            res = _result(i, f"child {i}")
            ad.publish_batch_child_completion(res, delegation_id=holder["id"])
            results.append(res)
        return {"results": results}

    delegation_id = _dispatch([f"lane {i}" for i in range(n)], runner)
    holder["id"] = delegation_id
    return delegation_id


# ── authentication by durable terminal receipt ─────────────────────────────

def test_authentic_terminal_receipt_opens_one_window_and_replay_grants_nothing():
    root = _root()
    delegation_id = _single_job()
    _hand_off(root, delegation_id, ["Build and verify the explainer."])
    evt = _next_event()
    _wait_state(delegation_id)
    claim = _claim(evt)
    before = _cp(root).snapshot()

    _internal_turn(root)

    assert _cp(root).snapshot()["phase"] == dc.PHASE_INTEGRATING
    assert not _work(root).blocked, "the verified result must be checkable"
    assert _cp(root)._owned[delegation_id].consumed_receipts == {delegation_id}
    ad.complete_event_delivery(evt, claim)

    _internal_turn(root)  # replay of the same completion

    assert _cp(root).snapshot()["phase"] == dc.PHASE_HANDED_OFF
    assert _work(root).block_code == dc.HANDOFF_ACTIVE
    assert _cp(root).snapshot()["request_id"] == before["request_id"]


def test_running_job_grants_nothing():
    root = _root()
    release = threading.Event()
    delegation_id = _single_job(release)
    try:
        _hand_off(root, delegation_id, ["Build and verify."])
        _internal_turn(root)  # an unrelated internal event
        assert _work(root).block_code == dc.HANDOFF_ACTIVE
    finally:
        release.set()


def test_finalizing_before_persistence_opens_no_window(monkeypatch):
    root = _root()
    persisting = threading.Event()
    proceed = threading.Event()
    real_persist = ad._persist_completion

    def paused(evt, result):
        persisting.set()
        proceed.wait(30)
        return real_persist(evt, result)

    monkeypatch.setattr(ad, "_persist_completion", paused)
    delegation_id = _single_job()
    try:
        _hand_off(root, delegation_id, ["Build and verify."])
        assert persisting.wait(10), "the job never reached finalization"
        assert ad._records[delegation_id]["status"] == "finalizing"

        _internal_turn(root)

        assert _work(root).block_code == dc.HANDOFF_ACTIVE
        assert _cp(root)._owned[delegation_id].consumed_receipts == set()
    finally:
        proceed.set()


def test_terminal_but_unclaimed_row_grants_nothing():
    root = _root()
    delegation_id = _single_job()
    _hand_off(root, delegation_id, ["Build and verify."])
    _next_event()
    _wait_state(delegation_id)

    _internal_turn(root)

    assert _work(root).block_code == dc.HANDOFF_ACTIVE


def test_dropped_row_grants_nothing():
    root = _root()
    delegation_id = _single_job()
    _hand_off(root, delegation_id, ["Build and verify."])
    evt = _next_event()
    _wait_state(delegation_id)
    assert ad.drop_completion_delivery(delegation_id, _claim(evt))

    _internal_turn(root)

    assert _work(root).block_code == dc.HANDOFF_ACTIVE


def test_missing_row_fails_closed():
    root = _root()
    _hand_off(root, "async-never-dispatched", ["Build and verify."])

    _internal_turn(root)

    assert _work(root).block_code == dc.HANDOFF_ACTIVE


def test_receipt_probe_error_fails_closed():
    root = _root()
    delegation_id = _single_job()
    _hand_off(root, delegation_id, ["Build and verify."])
    evt = _next_event()
    _wait_state(delegation_id)
    _claim(evt)

    def broken(_ids):
        raise RuntimeError("database is locked")

    _cp(root).begin_turn(
        preserve=True, kind=INTERNAL, settings=_cp(root).settings,
        receipt_probe=broken,
    )

    assert _work(root).block_code == dc.HANDOFF_ACTIVE



def test_tui_async_delegation_complete_kind_does_not_preserve():
    root = _root()
    delegation_id = _single_job()
    _hand_off(root, delegation_id, ["Build and verify."])
    before = _cp(root).snapshot()

    dc.begin_turn(root, "async_delegation_complete")

    after = _cp(root).snapshot()
    assert after["request_id"] != before["request_id"]
    assert after["phase"] == dc.PHASE_FOREGROUND


# ── fan-outs: the per-child durable rows are the receipts ──────────────────

def test_fanout_child_rows_are_the_receipts():
    root = _root()
    gates = [threading.Event(), threading.Event()]
    delegation_id = _fanout(2, gates)
    _hand_off(root, delegation_id, ["lane 0", "lane 1"])
    assert _cp(root)._owned[delegation_id].receipt_ids == (
        f"{delegation_id}:child:0", f"{delegation_id}:child:1",
    )
    try:
        gates[0].set()
        evt0 = _next_event()
        _claim(evt0)
        assert ad.get_durable_delegation(delegation_id)["state"] == "running"

        _internal_turn(root)

        assert _cp(root).snapshot()["phase"] == dc.PHASE_INTEGRATING
        assert _cp(root)._owned[delegation_id].consumed_receipts == {
            f"{delegation_id}:child:0"
        }
        gates[1].set()
        evt1 = _next_event()
        _claim(evt1)

        _internal_turn(root)

        assert _cp(root).snapshot()["phase"] == dc.PHASE_INTEGRATING
        assert not _work(root).blocked
    finally:
        for gate in gates:
            gate.set()


def test_coalesced_claimed_children_share_one_window():
    root = _root()
    gates = [threading.Event(), threading.Event()]
    delegation_id = _fanout(2, gates)
    _hand_off(root, delegation_id, ["lane 0", "lane 1"])
    for gate in gates:
        gate.set()
    events = [_next_event(), _next_event()]
    for evt in events:
        _claim(evt)

    _internal_turn(root)

    assert _cp(root)._owned[delegation_id].consumed_receipts == {
        f"{delegation_id}:child:0", f"{delegation_id}:child:1",
    }
    assert _cp(root)._windows_by_request[_cp(root).request_id] == 1
    _internal_turn(root)
    assert _work(root).block_code == dc.HANDOFF_ACTIVE


def test_children_beyond_cap_are_report_only():
    root = _root()
    gates = [threading.Event() for _ in range(3)]
    delegation_id = _fanout(3, gates)
    _hand_off(root, delegation_id, ["a", "b", "c"])
    phases = []
    for gate in gates:
        gate.set()
        _claim(_next_event())
        _internal_turn(root)
        phases.append(_cp(root).snapshot()["phase"])
        time.sleep(0.05)

    assert phases[:2] == [dc.PHASE_INTEGRATING, dc.PHASE_INTEGRATING]
    assert phases[2] == dc.PHASE_HANDED_OFF
    assert _work(root).block_code == dc.HANDOFF_ACTIVE


def test_batch_join_event_is_not_a_receipt():
    root = _root()
    gates = [threading.Event(), threading.Event()]
    delegation_id = _fanout(2, gates)
    _hand_off(root, delegation_id, ["lane 0", "lane 1"])
    for gate in gates:
        gate.set()
    _wait_state(delegation_id)
    # Child rows were published but never claimed; the join was acked.
    _internal_turn(root)

    assert _work(root).block_code == dc.HANDOFF_ACTIVE


# ── the window itself ──────────────────────────────────────────────────────

def _open_window(root):
    delegation_id = _single_job()
    _hand_off(root, delegation_id, ["Build and verify."])
    evt = _next_event()
    _wait_state(delegation_id)
    _claim(evt)
    _internal_turn(root)
    assert _cp(root).snapshot()["phase"] == dc.PHASE_INTEGRATING
    return delegation_id


def test_integration_cannot_spawn_or_hand_off_again():
    root = _root()
    _open_window(root)

    for background in (True, False):
        verdict = dc.admit(
            root, "delegate_task", {"goal": "more", "background": background}, "s"
        )
        assert verdict.block_code == dc.SPAWN_CLOSED
    # Control actions on still-live children stay available.
    assert not dc.admit(root, "delegate_task", {"action": "list"}, "l").blocked
    assert _cp(root).ticket().accept_handoff(
        delegation_id="other", goals=["x"], subagent_ids=[]
    ) is False


def test_window_is_bounded_then_closed():
    root = _root()
    _open_window(root)
    limit = _cp(root).settings.max_work_tools

    for i in range(limit):
        assert not _work(root, f"v{i}").blocked
    verdict = _work(root, "over")

    assert verdict.block_code == dc.INTEGRATION_EXHAUSTED
    assert _cp(root).snapshot()["phase"] == dc.PHASE_CLOSED
    assert _work(root, "again").block_code == dc.INTEGRATION_EXHAUSTED


def test_window_work_never_spends_the_stored_ledger():
    root = _root()
    cp = _cp(root)
    _open_window(root)
    stored_used = cp.used

    assert not _work(root).blocked

    assert cp.used == stored_used


def test_late_receipt_does_not_replace_a_newer_requests_ledger():
    root = _root()
    cp = _cp(root)
    release = threading.Event()
    delegation_id = _single_job(release)
    _hand_off(root, delegation_id, ["Build and verify."])
    old_request = cp.request_id
    dc.begin_turn(root, None)  # the human sends a new message
    cp.declare("direct", "unrelated question")
    assert not _work(root).blocked
    new_ledger = (cp.request_id, cp.used, cp.phase)
    assert new_ledger[0] != old_request

    release.set()
    evt = _next_event()
    _wait_state(delegation_id)
    _claim(evt)
    _internal_turn(root)

    assert cp.snapshot()["phase"] == dc.PHASE_INTEGRATING
    assert not _work(root).blocked
    assert (cp.request_id, cp.used, cp.phase) == new_ledger
    assert cp._windows_by_request[old_request] == 1
    assert cp._windows_by_request.get(new_ledger[0], 0) == 0


def test_integration_window_cap_setting_is_validated():
    assert dc.normalize_settings({"max_integration_windows": 3}).max_integration_windows == 3
    default = dc.normalize_settings({}).max_integration_windows
    for bad in (0, -1, True, "two", 1.5, None):
        assert dc.normalize_settings({"max_integration_windows": bad}).max_integration_windows == default
