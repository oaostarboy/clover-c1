"""B1 joined fan-out regression probes (from the Fable 5.1 review).

Real async registry + durable SQLite; fake child work only."""
import json, pathlib, queue, threading, time
import pytest
from tools import async_delegation as ad
from tools.process_registry import process_registry, _format_async_delegation

BATCH = "deleg_fab1e001"
GOALS = ["alpha", "beta", "gamma"]
DEAD = 99999999


@pytest.fixture(autouse=True)
def reg(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    ad._reset_for_tests(drain_timeout=10)
    original = process_registry.completion_queue
    process_registry.completion_queue = queue.Queue()
    try:
        yield process_registry.completion_queue
    finally:
        # Runners released at the end of the test finish (and publish) first,
        # against this test's queue and records, never the next test's.
        ad._reset_for_tests(drain_timeout=10)
        process_registry.completion_queue = original


def _res(i, status="completed", summary=None, error=None):
    return {"task_index": i, "status": status,
            "summary": summary if summary is not None else (f"r{i}" if status == "completed" else None),
            "error": error, "api_calls": 1, "duration_seconds": 1.0, "exit_reason": status}


def _dispatch(runner, mode="joined"):
    h = ad.dispatch_async_delegation_batch(
        goals=GOALS, context="ctx", toolsets=None, role="leaf", model="m",
        session_key="agent:main:telegram:dm:100", parent_session_id="sess-origin",
        runner=runner, delegation_id=BATCH, fanout_delivery=mode,
        request_identity={"turn_id": "t1", "request_id": "r1", "generation": 2})
    assert h["status"] == "dispatched", h


def _drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def _col(did, col):
    with ad._DB_LOCK, ad._transaction() as conn:
        row = conn.execute(f"SELECT {col} FROM async_delegations WHERE delegation_id=?", (did,)).fetchone()
    return row[0] if row else None


def _kill_owner():
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET owner_pid=? WHERE delegation_id=?", (DEAD, BATCH))
    with ad._records_lock:
        ad._records.clear()


def test_stall_force_finalize_vs_restore_vs_runner_return_then_late_child_exactly_once(reg):
    gate, parked = threading.Event(), threading.Event()

    def runner():
        ad.publish_batch_child_completion(_res(2), delegation_id=BATCH)
        ad.publish_batch_child_completion(_res(0), delegation_id=BATCH)
        parked.set()
        gate.wait(30)
        a = ad.publish_batch_child_completion(_res(1, summary="late beta"), delegation_id=BATCH)
        b = ad.publish_batch_child_completion(_res(1, summary="late beta"), delegation_id=BATCH)
        outcomes.extend([a, b])
        return {"results": [_res(0), _res(1, summary="late beta"), _res(2)], "total_duration_seconds": 1}

    outcomes = []
    _dispatch(runner)
    assert parked.wait(10)
    assert _drain(reg) == []
    with ad._records_lock:
        ad._records[BATCH]["status"] = "stalling"
    replay = queue.Queue()
    t1 = threading.Thread(target=ad._finalize_stalled, args=(BATCH,))
    t2 = threading.Thread(target=ad.restore_undelivered_completions, args=(replay,))
    t1.start(); t2.start(); t1.join(10); t2.join(10)
    join = reg.get(timeout=5)
    assert join["delegation_id"] == BATCH and join["fanout_delivery"] == "joined"
    assert [r["task_index"] for r in join["results"]] == [0, 1, 2]
    assert join["incomplete_indexes"] == [1] and join["results"][1]["incomplete"] is True
    assert join["results"][0]["summary"] == "r0" and join["results"][2]["summary"] == "r2"
    assert join["status"] == "stalled" and join["request_turn_id"] == "t1"
    replayed = _drain(replay)
    assert len(replayed) <= 1 and all(e["delegation_id"] == BATCH for e in replayed)
    assert _drain(reg) == []
    gate.set()
    late = reg.get(timeout=10)
    assert late["late_result"] is True and late["batch_delegation_id"] == BATCH
    assert late["delegation_id"] == f"{BATCH}:child:1"
    assert late["request_turn_id"] == "t1" and late["request_generation"] == 2
    time.sleep(0.5)
    assert _drain(reg) == [], "runner return / duplicate publish must not add events"
    assert outcomes == ["late", "duplicate"]
    assert ad.claim_completion_delivery(BATCH, "c1") is True
    assert ad.claim_completion_delivery(BATCH, "c2") is False
    assert _col(f"{BATCH}:child:0", "delivery_claim") == "c1"
    assert ad.complete_completion_delivery(BATCH, "c1") is True
    assert _col(f"{BATCH}:child:0", "delivery_state") == "delivered"
    assert _col(f"{BATCH}:child:2", "delivery_state") == "delivered"
    assert _col(f"{BATCH}:child:1", "delivery_state") == "pending"
    assert ad.claim_completion_delivery(f"{BATCH}:child:1", "c3") is True
    assert ad.complete_completion_delivery(f"{BATCH}:child:1", "c3") is True
    q = queue.Queue(); ad.restore_undelivered_completions(q)
    assert _drain(q) == []


def test_out_of_order_failed_and_duplicate_publishes_make_one_ordered_join(reg):
    seen = []

    def runner():
        seen.append(ad.publish_batch_child_completion(_res(2), delegation_id=BATCH))
        seen.append(ad.publish_batch_child_completion(_res(1, status="error", error="boom"), delegation_id=BATCH))
        seen.append(ad.publish_batch_child_completion(_res(2), delegation_id=BATCH))
        seen.append(ad.publish_batch_child_completion(_res(0), delegation_id=BATCH))
        return {"results": [_res(2), _res(0), _res(1, status="error", error="boom")], "total_duration_seconds": 1}

    _dispatch(runner)
    join = reg.get(timeout=10)
    assert seen == ["parked", "parked", "duplicate", "parked"]
    assert [r["task_index"] for r in join["results"]] == [0, 1, 2]
    assert join["results"][1]["status"] == "error" and join["incomplete_indexes"] == []
    assert join["status"] == "completed"
    time.sleep(0.3); assert _drain(reg) == []
    assert ad.publish_batch_child_completion(_res(0), delegation_id=BATCH) == "duplicate"
    assert _drain(reg) == []
    text = _format_async_delegation(join)
    assert text.index("alpha") < text.index("beta") < text.index("gamma")
    assert "TASK 1/3" in text and "boom" in text


def test_owner_death_with_no_child_published_still_joins_and_later_child_is_late(reg):
    gate = threading.Event()

    def runner():
        gate.wait(30)
        return {"results": []}

    _dispatch(runner)
    _kill_owner()
    q = queue.Queue()
    assert ad.restore_undelivered_completions(q) == 1
    (join,) = _drain(q)
    assert join["restored"] is True and join["incomplete_indexes"] == [0, 1, 2]
    assert join["status"] == "unknown" and join["request_id"] == "r1"
    q2 = queue.Queue(); ad.restore_undelivered_completions(q2)
    assert [e["delegation_id"] for e in _drain(q2)] == [BATCH]
    assert ad.claim_completion_delivery(BATCH, "g") and ad.complete_completion_delivery(BATCH, "g")
    gate.set()
    assert ad.publish_batch_child_completion(_res(1), delegation_id=BATCH) == "late"
    late = reg.get(timeout=5)
    assert late["batch_delegation_id"] == BATCH and late["late_result"] is True
    assert late["parent_session_id"] == "sess-origin"
    assert _drain(reg) == []
    assert ad.publish_batch_child_completion(_res(1), delegation_id=BATCH) == "duplicate"


def test_stall_after_every_child_parked_joins_all_results_without_incomplete(reg):
    gate = threading.Event()

    def runner():
        for i in (1, 0, 2):
            ad.publish_batch_child_completion(_res(i), delegation_id=BATCH)
        gate.wait(30)
        return {"results": []}

    _dispatch(runner)
    time.sleep(0.5)
    with ad._records_lock:
        ad._records[BATCH]["status"] = "stalling"
    ad._finalize_stalled(BATCH)
    join = reg.get(timeout=5)
    assert join["incomplete_indexes"] == []
    assert [r["summary"] for r in join["results"]] == ["r0", "r1", "r2"]
    gate.set()
    time.sleep(0.3)
    assert _drain(reg) == []


def test_each_mode_dispatch_note_unchanged_and_joined_note_is_mode_aware():
    from tools.delegate_tool import _background_dispatch_note
    assert _background_dispatch_note(3) == _background_dispatch_note(3, joined=False)
    assert "together" in _background_dispatch_note(3, joined=True)
    assert _background_dispatch_note(1, joined=True) == _background_dispatch_note(1)
