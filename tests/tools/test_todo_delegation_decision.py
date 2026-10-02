"""Behavioral tests for todo's bounded delegation decision check."""

import json

from tools.todo_tool import TodoStore, todo_tool


def tasks(*ids):
    return [{"id": value, "content": f"work {value}", "status": "pending"} for value in ids]


def test_capable_root_missing_decision_gets_one_reminder_per_plan():
    store = TodoStore()

    first = json.loads(todo_tool(todos=tasks("a", "b"), store=store, delegation_check=True))
    repeated = json.loads(todo_tool(todos=tasks("a", "b"), merge=True, store=store, delegation_check=True))
    read = json.loads(todo_tool(store=store, delegation_check=True))

    assert first["reminder"]
    assert "delegate" in first["reminder"].lower()
    assert "reminder" not in repeated
    assert "reminder" not in read


def test_declared_direct_decision_is_returned_and_persisted():
    store = TodoStore()
    result = json.loads(todo_tool(
        todos=tasks("a", "b"),
        delegation={"mode": "direct", "reason": "Small, related steps"},
        store=store,
        delegation_check=True,
    ))

    assert result["delegation"] == {"mode": "direct", "reason": "Small, related steps"}
    assert "reminder" not in result
    assert store.snapshot()["delegation"] == result["delegation"]


def test_declared_delegate_decision_suppresses_reminder():
    store = TodoStore()
    result = json.loads(todo_tool(
        todos=tasks("a"),
        delegation={"mode": "delegate", "reason": "Independent research tasks"},
        store=store,
        delegation_check=True,
    ))

    assert result["delegation"]["mode"] == "delegate"
    assert "reminder" not in result


def test_malformed_decision_is_rejected_without_mutating_todos():
    store = TodoStore()
    store.write(tasks("old"))
    before = store.snapshot()

    result = json.loads(todo_tool(
        todos=tasks("new"),
        delegation={"mode": "later", "reason": "hmm"},
        store=store,
        delegation_check=True,
    ))

    assert "error" in result
    assert store.snapshot() == before


def test_replacing_root_plan_resets_one_time_reminder():
    store = TodoStore()
    first = json.loads(todo_tool(todos=tasks("a"), store=store, delegation_check=True))
    replacement = json.loads(todo_tool(todos=tasks("b"), store=store, delegation_check=True))

    assert first["reminder"]
    assert replacement["reminder"]


def test_decision_reason_is_bounded_and_survives_restore():
    store = TodoStore()
    result = json.loads(todo_tool(
        todos=tasks("a"),
        delegation={"mode": "delegate", "reason": "x" * 1000},
        store=store,
    ))
    snapshot = store.snapshot()
    resumed = TodoStore()
    resumed.restore(
        snapshot["todos"], revision=snapshot["revision"],
        delegation=snapshot["delegation"],
        delegation_reminded=snapshot["delegation_reminded"],
    )

    assert len(result["delegation"]["reason"]) == 500
    assert resumed.snapshot()["delegation"] == result["delegation"]
    resumed_result = json.loads(todo_tool(store=resumed, delegation_check=True))
    assert "reminder" not in resumed_result


def test_missing_capability_or_child_identity_disables_check():
    from tools.todo_tool import delegation_check_for_agent

    assert not delegation_check_for_agent(type("Agent", (), {"valid_tool_names": {"todo"}})())
    child = type("Agent", (), {
        "valid_tool_names": {"todo", "delegate_task"}, "_delegate_depth": 1,
    })()
    assert not delegation_check_for_agent(child)


def test_empty_read_does_not_consume_reminder_before_first_plan_write():
    store = TodoStore()
    store.write(tasks("a"))
    read = json.loads(todo_tool(store=store, delegation_check=True))
    written = json.loads(todo_tool(todos=tasks("a"), merge=True, store=store, delegation_check=True))

    assert "reminder" not in read
    assert written["reminder"]


def test_non_string_mode_is_rejected_without_mutation():
    store = TodoStore()
    store.write(tasks("keep"))
    before = store.snapshot()

    result = json.loads(todo_tool(
        todos=tasks("replace"), delegation={"mode": [], "reason": "reason"}, store=store,
    ))

    assert "error" in result
    assert store.snapshot() == before


def test_decision_can_be_added_without_rewriting_todos():
    store = TodoStore()
    store.write(tasks("keep"))
    result = json.loads(todo_tool(
        delegation={"mode": "direct", "reason": "One small task"}, store=store,
    ))

    assert result["todos"] == tasks("keep")
    assert result["delegation"] == {"mode": "direct", "reason": "One small task"}
    assert result["revision"] == 2


def test_restore_does_not_treat_string_false_as_reminder_state():
    store = TodoStore()
    store.restore(tasks("a"), revision=1, delegation_reminded="false")

    assert store.snapshot()["delegation_reminded"] is False


def test_completed_only_plan_does_not_receive_reminder():
    store = TodoStore()
    completed = [{"id": "done", "content": "Finished", "status": "completed"}]

    result = json.loads(todo_tool(todos=completed, store=store, delegation_check=True))

    assert "reminder" not in result


def test_completing_plan_clears_decision_for_next_plan():
    store = TodoStore()
    todo_tool(
        todos=tasks("a"),
        delegation={"mode": "direct", "reason": "Finished scope"},
        store=store,
    )
    todo_tool(
        todos=[{"id": "a", "content": "work a", "status": "completed"}],
        store=store,
    )

    next_plan = json.loads(todo_tool(todos=tasks("a"), store=store, delegation_check=True))

    assert next_plan["reminder"]
    assert "delegation" not in next_plan


def test_decision_survives_compression_injection_for_active_plan():
    store = TodoStore()
    todo_tool(
        todos=tasks("active"),
        delegation={"mode": "direct", "reason": "Focused work"},
        store=store,
    )

    injection = store.format_for_injection()

    assert "mode=direct" in injection
    assert "Focused work" in injection


def test_registry_handler_forwards_decision_and_capability_kw():
    from tools.todo_tool import TODO_SCHEMA
    from tools.registry import registry

    entry = registry.get_entry("todo")
    store = TodoStore()
    result = json.loads(entry.handler({
        "todos": tasks("a"),
        "delegation": {"mode": "direct", "reason": "Small"},
    }, store=store, delegation_check=True))

    assert result["delegation"]["mode"] == "direct"
    assert "delegation" in TODO_SCHEMA["parameters"]["properties"]
