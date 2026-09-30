"""Background fan-out publishes independent, durable terminal child outcomes."""
import queue
import threading

from tools import async_delegation as async_d
from tools.process_registry import process_registry, format_process_notification


def test_pending_children_survive_history_pruning(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    monkeypatch.setattr(async_d, "_MAX_RETAINED_COMPLETED", 1)
    async_d._reset_for_tests()
    with async_d._transaction() as conn:
        for i in range(2):
            conn.execute(
                """INSERT INTO async_delegations
                   (delegation_id, origin_session, state, dispatched_at, updated_at,
                    delivery_state) VALUES (?, 'owner', 'completed', 1, 1, 'pending')""",
                (f"batch:child:{i}",),
            )
    async_d._prune_durable_records()
    assert all(
        async_d.get_durable_delegation(f"batch:child:{i}") is not None
        for i in range(2)
    )


def test_late_consumed_notice_does_not_hide_new_failed_process(monkeypatch):
    from types import SimpleNamespace
    from gateway.run import _stale_observed_process_notification
    from tools.process_registry import process_registry as registry

    monkeypatch.setattr(registry, "is_exit_observed", lambda sid: sid == "old-process")
    def notice(sid):
        return SimpleNamespace(internal=True, metadata={"process_session_id": sid})

    assert _stale_observed_process_notification(notice("old-process")) == "old-process"
    assert _stale_observed_process_notification(notice("new-failed-process")) == ""
    assert "exit code 1" in (format_process_notification({
        "type": "completion", "session_id": "new-failed-process",
        "command": "failing-job", "exit_code": 1, "output": "new failure",
    }) or "")


def test_owner_loss_keeps_committed_child_and_marks_only_unfinished_unknown(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    async_d._reset_for_tests()
    import time
    now = time.time()
    with async_d._transaction() as conn:
        conn.execute(
            """INSERT INTO async_delegations
               (delegation_id, origin_session, state, dispatched_at, updated_at,
                delivery_state, owner_pid, task_json)
               VALUES ('batch', 'owner', 'running', ?, ?, 'pending', 99999999,
                       '{"goals":["favicon","other"],"is_batch":true}')""",
            (now, now),
        )
        evt = {"type": "async_delegation", "delegation_id": "batch:child:0",
               "session_key": "owner", "summary": "favicon ready", "status": "completed"}
        import json
        conn.execute(
            """INSERT INTO async_delegations
               (delegation_id, origin_session, state, dispatched_at, completed_at,
                updated_at, delivery_state, event_json)
               VALUES ('batch:child:0', 'owner', 'completed', ?, ?, ?,
                       'pending', ?)""", (now, now, now, json.dumps(evt)),
        )
    replay = queue.Queue()
    assert async_d.restore_undelivered_completions(replay) == 2
    by_id = {item["delegation_id"]: item for item in list(replay.queue)}
    assert by_id["batch:child:0"]["summary"] == "favicon ready"
    assert by_id["batch"]["status"] == "unknown"
    assert async_d.get_durable_delegation("batch:child:0")["delivery_state"] == "pending"


def test_child_delivered_while_sibling_runs_and_recovered_once(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    async_d._reset_for_tests()
    gate = threading.Event()
    first = threading.Event()
    finished = threading.Event()
    original_queue = process_registry.completion_queue
    process_registry.completion_queue = queue.Queue()
    try:
        def runner(publish):
            publish({"task_index": 0, "status": "completed", "summary": "favicon ready", "api_calls": 2})
            first.set()
            assert gate.wait(5), "sibling never released"
            publish({"task_index": 1, "status": "error", "error": "new failure"})
            finished.set()
            return {"results": [
                {"task_index": 0, "status": "completed", "summary": "favicon ready"},
                {"task_index": 1, "status": "error", "error": "new failure"},
            ]}

        # The production batch runner receives a per-child publish callback.
        from functools import partial
        dispatch = async_d.dispatch_async_delegation_batch(
            goals=["favicon", "other"], context=None, toolsets=None, role="leaf",
            model="test", session_key="owner", runner=lambda: runner(
                partial(async_d.publish_batch_child_completion, delegation_id=delegation_id)
            ), delegation_id=(delegation_id := "test-batch"),
        )
        assert dispatch["status"] == "dispatched"
        assert first.wait(5)
        first_evt = process_registry.completion_queue.get(timeout=5)
        assert first_evt["summary"] == "favicon ready"
        assert first_evt["delegation_id"] == "test-batch:child:0"
        assert "favicon ready" in format_process_notification(first_evt)
        assert not finished.is_set()
        async_d.publish_batch_child_completion(
            {"task_index": 0, "status": "completed", "summary": "favicon ready"},
            delegation_id="test-batch",
        )
        assert process_registry.completion_queue.empty()  # no second publish
        assert async_d.get_durable_delegation("test-batch:child:0")["delivery_state"] == "pending"
        # Simulate owner loss after SQLite commit and before adapter injection.
        replay = queue.Queue()
        assert async_d.restore_undelivered_completions(replay) >= 1
        restored = next(evt for evt in list(replay.queue) if evt["delegation_id"] == first_evt["delegation_id"])
        claim = async_d.claim_event_delivery(restored, "new-owner")
        assert claim
        async_d.complete_event_delivery(restored, claim)
        assert async_d.claim_event_delivery(first_evt, "old-owner") is None
        gate.set()
        assert finished.wait(5)
        second_evt = process_registry.completion_queue.get(timeout=5)
        assert second_evt["error"] == "new failure"
        assert "new failure" in (format_process_notification(second_evt) or "")
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            parent = async_d.get_durable_delegation("test-batch")
            if parent and parent["delivery_state"] == "delivered":
                break
            time.sleep(0.02)
        assert parent["delivery_state"] == "delivered"
        assert process_registry.completion_queue.empty()
    finally:
        gate.set()
        process_registry.completion_queue = original_queue
        async_d._reset_for_tests()
