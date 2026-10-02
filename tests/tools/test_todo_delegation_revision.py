"""Regression tests for delegation snapshot revision and plan boundaries."""

import json

from tools.todo_tool import TodoStore, todo_tool


def tasks(*ids, status="pending"):
    return [
        {"id": item_id, "content": f"work {item_id}", "status": status}
        for item_id in ids
    ]


def test_clearing_decision_on_unchanged_completed_list_increments_revision():
    store = TodoStore()
    completed = tasks("root", status="completed")
    todo_tool(todos=completed, store=store)
    todo_tool(
        delegation={"mode": "direct", "reason": "Final record"}, store=store,
    )
    before = store.snapshot()["revision"]

    result = json.loads(todo_tool(todos=completed, store=store))

    assert "delegation" not in result
    assert result["revision"] > before


def test_new_active_plan_reusing_completed_ids_resets_final_decision():
    store = TodoStore()
    todo_tool(todos=tasks("root"), store=store)
    final_result = json.loads(todo_tool(
        todos=tasks("root", status="completed"),
        delegation={"mode": "direct", "reason": "Final decision"},
        store=store,
    ))
    assert final_result["delegation"]["reason"] == "Final decision"

    next_plan = json.loads(todo_tool(
        todos=tasks("root"), store=store, delegation_check=True,
    ))

    assert "delegation" not in next_plan
    assert next_plan["reminder"]


def test_progress_writes_and_reads_preserve_decision_without_reminder():
    store = TodoStore()
    decision = {"mode": "delegate", "reason": "Independent work"}
    todo_tool(todos=tasks("root", "child"), delegation=decision, store=store)

    progress = json.loads(todo_tool(
        todos=[{"id": "root", "status": "in_progress"}],
        merge=True, store=store, delegation_check=True,
    ))
    read = json.loads(todo_tool(store=store, delegation_check=True))

    assert progress["delegation"] == decision
    assert "reminder" not in progress
    assert read["delegation"] == decision
    assert "reminder" not in read


def test_reason_whitespace_is_collapsed_before_capping():
    store = TodoStore()
    long_reason = "  " + "first line\n second\tline " * 100

    result = json.loads(todo_tool(
        todos=tasks("root"),
        delegation={"mode": "direct", "reason": long_reason},
        store=store,
    ))
    reason = result["delegation"]["reason"]

    assert reason == " ".join(long_reason.split())[:500]
    assert len(reason) == 500
    assert "\n" not in reason
