"""Final schema repair evidence must reach entry, callback and activity intact.

Only the scripted child boundary is synthetic; result assembly, outcome resolver,
completion callback and activity tracker are the production consumers.
"""
from copy import deepcopy
import json
from typing import Any

import pytest

from agent.delegation_activity import DelegationActivityTracker
from tools import delegate_tool as dt
from tests.tools.test_delegate_auto_continue import _Child, _Parent, _leg

BLOCKED = {"status": "blocked", "remaining": ["owner approval"],
           "owner": "human owner", "next_step": "Approve the read-only inventory?"}
DONE = {"status": "done", "remaining": [], "owner": "reviewer", "next_step": "Review output"}


def run_retry(monkeypatch, retry, *, first=None, stop=None):
    monkeypatch.setattr(dt, "_auto_continue_limit", lambda: 0)
    if first is None:
        first = _leg("Initial report", exhausted=False)
        first["task_outcome"] = deepcopy(DONE)
    child: Any = _Child([first, retry])
    if stop is not None:
        def stopped_retry(_history):
            if stop == "soft":
                child._interrupt_requested = True
            else:
                import threading
                child._hard_interrupt_requested = threading.Event()
                child._hard_interrupt_requested.set()
            return retry
        child.legs[1] = stopped_retry
    child._delegate_output_schema = {"type": "object", "required": ["answer"]}
    events = []
    child.tool_progress_callback = lambda event, **kw: events.append((event, kw))
    entry = dt._run_single_child(0, "Only the approved inventory", child, _Parent())
    completions = [kw for name, kw in events if name == "subagent.complete"]
    assert len(child.calls) == 2
    assert len(completions) == 1
    assert entry["schema_retries"] == 1
    event = completions[0]
    tracker = DelegationActivityTracker()
    tracker.observe("subagent.complete", delegation_id="schema-authority", task_index=0, **event)
    activity = tracker.snapshot("schema-authority")[0]
    assert event["status"] == entry["status"]
    assert event["exit_reason"] == entry["exit_reason"]
    assert event["task_outcome"] == entry["task_outcome"]
    return entry, event, activity


@pytest.mark.parametrize("wire", ["structured", "text"])
def test_retry_blocker_replaces_old_success_in_every_consumer(monkeypatch, wire):
    retry = _leg("Owner approval is required.", exhausted=False)
    if wire == "structured":
        retry["task_outcome"] = deepcopy(BLOCKED)
    else:
        retry["final_response"] = "Task outcome: " + json.dumps(BLOCKED) + "\nResult: not done"
    entry, event, activity = run_retry(monkeypatch, retry)
    assert entry["task_outcome"] == BLOCKED
    assert event["outcome"] == "not_done"
    assert activity["state"] == "incomplete"
    assert BLOCKED["owner"] in activity["reason"]
    assert BLOCKED["next_step"] in activity["reason"]


@pytest.mark.parametrize("boundary", ["interrupted", "failed", "error", "all"])
@pytest.mark.parametrize("text", ["", '{"answer":"checked"}'])
def test_retry_terminal_evidence_overrides_success_and_schema(monkeypatch, boundary, text):
    retry = _leg(text, exhausted=False)
    retry["task_outcome"] = deepcopy(DONE)
    if boundary == "all":
        retry.update(interrupted=True, failed=True, error="offline storage failure")
    else:
        retry[boundary] = "offline storage failure" if boundary == "error" else True
    entry, event, activity = run_retry(monkeypatch, retry)
    stopped = boundary in {"interrupted", "all"}
    assert entry["status"] == ("interrupted" if stopped else "failed")
    assert entry["exit_reason"] == ("interrupted" if stopped else "error")
    assert entry["task_outcome"] == {"status": "unknown"}
    assert activity["state"] == ("cancelled" if stopped else "failed")
    assert event["outcome"] == ""
    assert entry["schema_valid"] is bool(text)
    if boundary == "error":
        assert entry["error"] == "offline storage failure"


@pytest.mark.parametrize("stop", ["soft", "hard"])
def test_stop_set_during_schema_retry_wins_over_returned_success(monkeypatch, stop):
    retry = _leg('{"answer":"checked"}', exhausted=False)
    retry["task_outcome"] = deepcopy(DONE)
    entry, event, activity = run_retry(monkeypatch, retry, stop=stop)
    assert entry["status"] == "interrupted"
    assert entry["exit_reason"] == "interrupted"
    assert entry["task_outcome"] == {"status": "unknown"}
    assert event["outcome"] == ""
    assert activity["state"] == "cancelled"


@pytest.mark.parametrize("retry", [None, [], "not a result", {},
                                   {"final_response": None},
                                   {"final_response": {"answer": "not text"}},
                                   {"final_response": ["not text"]}],
                         ids=["none", "list", "string", "empty", "null-text", "object-text", "list-text"])
def test_missing_or_malformed_retry_never_reuses_old_success(monkeypatch, retry):
    entry, event, activity = run_retry(monkeypatch, retry)
    assert entry["status"] == "failed"
    assert entry["task_outcome"] == {"status": "unknown"}
    assert activity["state"] == "failed"
    assert event["outcome"] == ""
    assert entry["schema_valid"] is False
    assert entry["api_calls"] >= 2  # retain known first-leg accounting


@pytest.mark.parametrize("raw", [None, {}, [], {"status": []}, {"status": "done"},
                                 {"status": "blocked", "remaining": []}])
def test_retry_missing_or_malformed_outcome_does_not_inherit_old_done(monkeypatch, raw):
    retry = _leg('{"answer":"checked"}', exhausted=False)
    retry["task_outcome"] = raw
    entry, event, activity = run_retry(monkeypatch, retry)
    assert entry["schema_valid"] is True
    assert entry["task_outcome"] == {"status": "unknown"}
    assert event["outcome"] == ""
    assert activity["state"] == "incomplete"


def test_schema_retry_exception_is_failure_not_old_success(monkeypatch):
    def failed_retry(_history):
        raise RuntimeError("offline retry storage failure")
    entry, event, activity = run_retry(monkeypatch, failed_retry)
    assert entry["status"] == "failed"
    assert entry["exit_reason"] == "error"
    assert "offline retry storage failure" in entry["error"]
    assert entry["task_outcome"] == {"status": "unknown"}
    assert event["outcome"] == ""
    assert activity["state"] == "failed"
    assert entry["api_calls"] == 2


def test_retry_current_termination_replaces_old_completed_flag(monkeypatch):
    retry = _leg("Still missing required data", exhausted=True)
    entry, event, activity = run_retry(monkeypatch, retry)
    assert entry["truncated"] is True
    assert entry["exit_reason"] == "max_iterations"
    assert entry["task_outcome"] == {"status": "unknown"}
    assert event["outcome"] == ""
    assert activity["state"] == "incomplete"


def test_successful_schema_retry_keeps_accounting_trace_and_pending_evidence(monkeypatch):
    first = _leg("Result: done", exhausted=True)
    first.update(api_calls=3, pending_steer="first pending request", auto_continuations=1,
                 continuation_kinds=["budget"])
    retry = _leg('{"answer":"checked"}', exhausted=False)
    retry.update(api_calls=4, task_outcome=deepcopy(DONE), pending_steer="second pending request")
    receipt = {"path": "/private/progress.json", "automatic_replay_allowed": False}
    retry["storage_recovery"] = receipt
    entry, event, activity = run_retry(monkeypatch, retry, first=first)
    assert entry["task_outcome"] == DONE
    assert entry["schema_valid"] is True
    assert entry["exit_reason"] == "completed"
    assert entry["truncated"] is False
    assert entry["api_calls"] == event["api_calls"] == 7
    assert len(entry["tool_trace"]) == 2
    assert entry["auto_continuations"] == 1
    assert entry["storage_recovery"] == receipt
    assert "first pending request" in entry["missed_steer"]
    assert "second pending request" in entry["missed_steer"]
    assert activity["state"] == "completed"


@pytest.mark.parametrize("status", ["blocked", "partial"])
def test_empty_retry_preserves_current_structured_obligation(monkeypatch, status):
    retry = _leg("", exhausted=False)
    report = dict(BLOCKED, status=status)
    retry["task_outcome"] = report
    entry, event, activity = run_retry(monkeypatch, retry)
    assert entry["task_outcome"] == report
    assert event["outcome"] == "not_done"
    assert activity["state"] == "incomplete"
    assert report["owner"] in activity["reason"]
    assert report["next_step"] in activity["reason"]


def test_empty_failed_retry_preserves_specific_failure_reason(monkeypatch):
    retry = _leg("", exhausted=False)
    retry.update(failed=True, failure_reason="offline persistence denied")
    entry, event, activity = run_retry(monkeypatch, retry)
    assert entry["error"] == "offline persistence denied"
    assert event["summary"] == "offline persistence denied"
    assert activity["reason"] == "offline persistence denied"


@pytest.mark.parametrize("count", [None, "invalid", {}, float("inf")])
def test_malformed_retry_accounting_keeps_known_first_leg_count(monkeypatch, count):
    retry = _leg('{"answer":"checked"}', exhausted=False)
    retry["api_calls"] = count
    entry, event, _ = run_retry(monkeypatch, retry)
    assert entry["api_calls"] == event["api_calls"] == 2


@pytest.mark.parametrize("boundary", ["interrupted", "failed", "error"])
def test_retry_terminal_flags_take_priority_over_blocker(monkeypatch, boundary):
    retry = _leg("Approval needed", exhausted=False)
    retry["task_outcome"] = deepcopy(BLOCKED)
    retry[boundary] = "offline failure" if boundary == "error" else True
    entry, _, activity = run_retry(monkeypatch, retry)
    assert entry["task_outcome"] == {"status": "unknown"}
    assert activity["state"] == ("cancelled" if boundary == "interrupted" else "failed")


@pytest.mark.parametrize("boundary", ["blocked", "error", "done"])
def test_bounded_continuation_then_schema_retry_uses_only_final_evidence(monkeypatch, boundary):
    monkeypatch.setattr(dt, "_auto_continue_limit", lambda: 2)
    initial = _leg("Approved work in progress", exhausted=True)
    def continuation(history):
        second = _leg("Initial report", exhausted=False, prior=history)
        second["task_outcome"] = deepcopy(DONE)
        return second
    retry = _leg('{"answer":"checked"}', exhausted=False)
    if boundary == "error":
        retry["error"] = "offline storage failure"
    else:
        retry["task_outcome"] = deepcopy(BLOCKED if boundary == "blocked" else DONE)
    child: Any = _Child([initial, continuation, retry])
    child._delegate_output_schema = {"type": "object", "required": ["answer"]}
    events = []
    child.tool_progress_callback = lambda name, **kw: events.append((name, kw))
    entry = dt._run_single_child(0, "Only the approved inventory", child, _Parent())
    assert len(child.calls) == 3  # one approved continuation, one schema retry, no new run
    assert entry["auto_continuations"] == 1
    assert entry["schema_retries"] == 1
    assert entry["api_calls"] == 6
    assert len(entry["tool_trace"]) == 3
    event = next(kw for name, kw in events if name == "subagent.complete")
    assert event["task_outcome"] == entry["task_outcome"]
    tracker = DelegationActivityTracker()
    tracker.observe("subagent.complete", delegation_id="continued-schema", task_index=0, **event)
    expected = {"blocked": "incomplete", "error": "failed", "done": "completed"}[boundary]
    assert tracker.snapshot("continued-schema")[0]["state"] == expected
    assert entry["task_outcome"] == ({"status": "unknown"} if boundary == "error"
                                       else BLOCKED if boundary == "blocked" else DONE)

