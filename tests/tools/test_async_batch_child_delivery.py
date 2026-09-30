"""Background fan-out publishes independent, durable terminal child outcomes."""
import queue
import threading

import pytest

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


@pytest.mark.macos_only
def test_child_delivery_and_recovery_on_macos(tmp_path, monkeypatch):
    test_child_delivered_while_sibling_runs_and_recovered_once(tmp_path, monkeypatch)


@pytest.mark.windows_only
def test_child_delivery_and_recovery_on_windows(tmp_path, monkeypatch):
    test_child_delivered_while_sibling_runs_and_recovered_once(tmp_path, monkeypatch)


def test_delivered_child_prompt_names_silence_marker_and_keeps_failure():
    from tools.process_registry import _format_async_delegation

    done = _format_async_delegation({
        "type": "async_delegation",
        "delegation_id": "deleg_ab12cd34:child:0",
        "goal": "favicon",
        "status": "completed",
        "summary": "favicon ready",
    })
    failed = _format_async_delegation({
        "type": "async_delegation",
        "delegation_id": "deleg_ab12cd34:child:1",
        "goal": "other",
        "status": "error",
        "error": "new failure",
    })
    for text in (done, failed):
        assert "[SILENT]" in text
        assert "must still be reported" in text
    assert "favicon ready" in done
    assert "new failure" in failed
    assert "one combined message" not in done


def test_single_process_completion_prompt_names_silence_marker():
    text = format_process_notification({
        "type": "completion",
        "session_id": "proc_new",
        "command": "failing-job",
        "exit_code": 1,
        "output": "new failure",
    })
    assert text is not None
    assert "[SILENT]" in text
    assert "exit code 1" in text
    assert "new failure" in text
    assert "must still be reported" in text


def test_dispatch_copy_matches_per_child_delivery():
    from tools.delegate_tool import (
        _background_dispatch_note,
        _build_top_level_description,
    )

    note = _background_dispatch_note(2)
    schema = _build_top_level_description()
    assert "as soon as that child finishes" in note
    assert "second combined message" in note
    assert "once ALL" not in note
    assert "not as one combined" in schema
    assert "one consolidated message" not in schema


def test_partial_batch_prompt_is_not_the_full_set():
    text = format_process_notification({
        "type": "async_delegation",
        "delegation_id": "deleg_ab12cd34",
        "is_batch": True,
        "partial_child_delivery": True,
        "goals": ["favicon", "other"],
        "results": [{"task_index": 1, "status": "error", "error": "publish failed"}],
    }) or ""
    assert "as the full set" in text
    assert "waited on each other" not in text
    assert "publish failed" in text
    assert "[SILENT]" in text


def test_owner_loss_acks_batch_when_every_child_is_committed(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    async_d._reset_for_tests()
    import json
    import time
    now = time.time()
    with async_d._transaction() as conn:
        conn.execute(
            """INSERT INTO async_delegations
               (delegation_id, origin_session, state, dispatched_at, updated_at,
                delivery_state, owner_pid, task_json)
               VALUES ('deleg_deadbeef', 'owner', 'running', ?, ?, 'pending', 99999999,
                       ?)""",
            (now, now, json.dumps({
                "goals": ["favicon", "other"], "is_batch": True,
            })),
        )
        for index, summary in ((0, "favicon ready"), (1, "other ready")):
            evt = {
                "type": "async_delegation",
                "delegation_id": f"deleg_deadbeef:child:{index}",
                "summary": summary, "status": "completed",
            }
            conn.execute(
                """INSERT INTO async_delegations
                   (delegation_id, origin_session, state, dispatched_at, completed_at,
                    updated_at, delivery_state, event_json)
                   VALUES (?, 'owner', 'completed', ?, ?, ?, 'pending', ?)""",
                (evt["delegation_id"], now, now, now, json.dumps(evt)),
            )
        # A LIKE-confusable id must not count as this batch's child.
        conn.execute(
            """INSERT INTO async_delegations
               (delegation_id, origin_session, state, dispatched_at, updated_at,
                delivery_state)
               VALUES ('delegXdeadbeef:child:0', 'owner', 'completed', ?, ?, 'pending')""",
            (now, now),
        )
    replay = queue.Queue()
    assert async_d.restore_undelivered_completions(replay) == 2
    ids = {item["delegation_id"] for item in list(replay.queue)}
    assert ids == {
        "deleg_deadbeef:child:0",
        "deleg_deadbeef:child:1",
    }
    parent = async_d.get_durable_delegation("deleg_deadbeef")
    assert parent is not None
    assert parent["delivery_state"] == "delivered"
    assert parent["state"] == "completed"


def test_owner_loss_names_committed_child_without_hiding_unfinished(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    async_d._reset_for_tests()
    import json
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
        conn.execute(
            """INSERT INTO async_delegations
               (delegation_id, origin_session, state, dispatched_at, completed_at,
                updated_at, delivery_state, event_json)
               VALUES ('batch:child:0', 'owner', 'completed', ?, ?, ?, 'pending',
                       '{"delegation_id":"batch:child:0","summary":"favicon ready","status":"completed"}')""",
            (now, now, now),
        )
    replay = queue.Queue()
    async_d.restore_undelivered_completions(replay)
    by_id = {item["delegation_id"]: item for item in list(replay.queue)}
    assert by_id["batch"]["status"] == "unknown"
    assert "not unknown" in by_id["batch"]["error"]
    assert by_id["batch:child:0"]["summary"] == "favicon ready"


def test_receiver_delivers_each_child_once_before_slow_sibling(tmp_path, monkeypatch):
    """Fake child callbacks, real SQLite, and a CLI-shaped receiver.

    No live gateway or external message. The receiver writes each delivered
    prompt to disk, claims it, and must not see a second batch summary.
    """
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    async_d._reset_for_tests()
    gate = threading.Event()
    first = threading.Event()
    delivered_dir = tmp_path / "delivered"
    delivered_dir.mkdir()
    original_queue = process_registry.completion_queue
    process_registry.completion_queue = queue.Queue()

    def receiver_once():
        pairs = process_registry.drain_notifications(session_key="owner")
        written = []
        for evt, text in pairs:
            claim = async_d.claim_event_delivery(evt, "review-receiver")
            if claim is None:
                continue
            path = delivered_dir / f"{evt['delegation_id'].replace(':', '_')}.txt"
            path.write_text(text, encoding="utf-8")
            async_d.complete_event_delivery(evt, claim)
            written.append((evt, text, path))
        return written

    try:
        def runner(publish):
            publish({
                "task_index": 0, "status": "completed",
                "summary": "favicon ready", "api_calls": 1,
            })
            first.set()
            assert gate.wait(5)
            publish({
                "task_index": 1, "status": "error", "error": "new failure",
            })
            return {"results": [
                {"task_index": 0, "status": "completed", "summary": "favicon ready"},
                {"task_index": 1, "status": "error", "error": "new failure"},
            ]}

        from functools import partial
        delegation_id = "deleg_ab12cd34"
        dispatch = async_d.dispatch_async_delegation_batch(
            goals=["favicon", "other"], context="shared", toolsets=None,
            role="leaf", model="test", session_key="owner",
            runner=lambda: runner(partial(
                async_d.publish_batch_child_completion, delegation_id=delegation_id,
            )),
            delegation_id=delegation_id,
        )
        assert dispatch["status"] == "dispatched"
        assert first.wait(5)
        first_delivery = receiver_once()
        assert len(first_delivery) == 1
        evt, text, path = first_delivery[0]
        assert path.is_file()
        assert path.read_text(encoding="utf-8") == text
        assert evt["delegation_id"] == "deleg_ab12cd34:child:0"
        assert "favicon ready" in text
        assert "[SILENT]" in text
        assert "must still be reported" in text
        assert not gate.is_set()
        # A second drain before the sibling finishes must not invent a batch summary.
        assert receiver_once() == []
        gate.set()
        import time
        deadline = time.monotonic() + 5
        second = []
        while time.monotonic() < deadline and not second:
            second = receiver_once()
            if not second:
                time.sleep(0.02)
        assert len(second) == 1
        assert second[0][0]["error"] == "new failure"
        assert "new failure" in second[0][1]
        assert "[SILENT]" in second[0][1]
        parent = None
        while time.monotonic() < deadline:
            parent = async_d.get_durable_delegation(delegation_id)
            if parent and parent["delivery_state"] == "delivered":
                break
            time.sleep(0.02)
        assert parent is not None and parent["delivery_state"] == "delivered"
        assert receiver_once() == []
        # Restart replay cannot claim a result the receiver already acked.
        replay = queue.Queue()
        async_d.restore_undelivered_completions(replay)
        assert all(
            item.get("delegation_id") != "deleg_ab12cd34:child:0"
            for item in list(replay.queue)
        )
        names = sorted(p.name for p in delivered_dir.iterdir())
        assert names == [
            "deleg_ab12cd34_child_0.txt",
            "deleg_ab12cd34_child_1.txt",
        ]
    finally:
        gate.set()
        process_registry.completion_queue = original_queue
        async_d._reset_for_tests()
