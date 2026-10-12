"""A helper that finishes after its fan-out already reported keeps a card note."""

from agent.delegation_activity import DelegationActivityTracker, TERMINAL_STATES


def _tracker():
    return DelegationActivityTracker()


def _start(tracker, index):
    tracker.observe(
        "subagent.start", preview=f"goal {index}",
        delegation_id="batch-1", subagent_id=f"sa-{index}", task_index=index,
        task_count=2, goal=f"goal {index}",
    )


def _complete(tracker, index, summary="all done"):
    tracker.observe(
        "subagent.complete", preview=summary, status="completed",
        delegation_id="batch-1", subagent_id=f"sa-{index}", task_index=index,
        task_count=2, goal=f"goal {index}", duration_seconds=4.0,
    )


def test_late_result_marks_the_finished_child_without_regressing_it():
    tracker = _tracker()
    _start(tracker, 0)
    _start(tracker, 1)
    _complete(tracker, 0)
    _complete(tracker, 1, summary="beta findings")

    changed, alerts = tracker.observe(
        "subagent.late_result", delegation_id="batch-1", subagent_id="sa-1",
        task_index=1, task_count=2, goal="goal 1",
    )

    assert changed == "batch-1"
    assert alerts == []
    children = {c["index"]: c for c in tracker.snapshot("batch-1")}
    assert children[1]["late_result"] is True
    assert children[1]["state"] in TERMINAL_STATES
    assert children[0].get("late_result") in (None, False)
    rendered = tracker.render("batch-1")
    assert "after the combined report" in rendered
    assert "beta findings" in rendered


def test_late_result_is_idempotent_and_never_creates_a_card():
    tracker = _tracker()
    assert tracker.observe(
        "subagent.late_result", delegation_id="never-seen", subagent_id="sa-9",
        task_index=0, task_count=1,
    ) == (None, [])
    assert tracker.group_ids() == []

    _start(tracker, 0)
    _complete(tracker, 0)
    first = tracker.observe("subagent.late_result", delegation_id="batch-1",
                            subagent_id="sa-0", task_index=0, task_count=2)
    second = tracker.observe("subagent.late_result", delegation_id="batch-1",
                             subagent_id="sa-0", task_index=0, task_count=2)
    assert first[0] == "batch-1"
    assert second == (None, [])


def test_child_callback_relays_the_late_note_with_the_helper_identity():
    from types import SimpleNamespace

    from tools.delegate_tool import _build_child_progress_callback

    seen = []

    def parent_cb(event_type, tool_name=None, preview=None, args=None, **kw):
        seen.append((event_type, kw))

    parent = SimpleNamespace(tool_progress_callback=parent_cb, _delegate_spinner=None)
    cb = _build_child_progress_callback(
        1, "audit beta", parent, task_count=3, subagent_id="sa-1",
        session_ref={"delegation_id": "batch-1"},
    )

    cb("subagent.late_result")

    assert [e for e, _ in seen] == ["subagent.late_result"]
    _event, kw = seen[0]
    assert kw["task_index"] == 1 and kw["subagent_id"] == "sa-1"
    assert kw["delegation_id"] == "batch-1"


def test_tui_and_desktop_never_see_the_card_only_late_note(monkeypatch):
    from tui_gateway import server

    emitted = []
    monkeypatch.setattr(server, "_emit", lambda et, sid, payload: emitted.append(et))
    monkeypatch.setattr(server, "_mirror_subagent_to_child", lambda *a, **k: None)
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda sid: True, raising=False)

    server._on_tool_progress("sid-1", "subagent.late_result", None, None, None,
                             subagent_id="sa-1", goal="g")

    assert emitted == []


def test_straggler_wait_shows_waiting_on_helper_until_it_ends_or_is_given_up():
    tracker = _tracker()
    _start(tracker, 0)
    _start(tracker, 1)
    _complete(tracker, 0)

    changed, _ = tracker.observe(
        "subagent.awaiting", awaiting=True, delegation_id="batch-1",
        subagent_id="sa-1", task_index=1, task_count=2, goal="goal 1",
    )
    assert changed == "batch-1"
    assert "waiting on subagent 2" in tracker.render("batch-1")
    assert {c["index"]: c["awaited"] for c in tracker.snapshot("batch-1")}[1] is True

    # Giving up the wait clears the note (the root reported without it).
    tracker.observe(
        "subagent.awaiting", awaiting=False, delegation_id="batch-1",
        subagent_id="sa-1", task_index=1, task_count=2, goal="goal 1",
    )
    assert "waiting on subagent" not in tracker.render("batch-1")


def test_awaiting_note_never_regresses_a_finished_helper_or_makes_a_card():
    tracker = _tracker()
    assert tracker.observe(
        "subagent.awaiting", awaiting=True, delegation_id="nope",
        subagent_id="sa-9", task_index=0, task_count=1) == (None, [])
    assert tracker.group_ids() == []

    _start(tracker, 0)
    _complete(tracker, 0)
    assert tracker.observe(
        "subagent.awaiting", awaiting=True, delegation_id="batch-1",
        subagent_id="sa-0", task_index=0, task_count=2) == (None, [])


def test_tui_and_desktop_never_see_the_card_only_awaiting_note(monkeypatch):
    from tui_gateway import server

    emitted = []
    monkeypatch.setattr(server, "_emit", lambda et, sid, payload: emitted.append(et))
    monkeypatch.setattr(server, "_mirror_subagent_to_child", lambda *a, **k: None)
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda sid: True, raising=False)

    server._on_tool_progress("sid-1", "subagent.awaiting", None, None, None,
                             subagent_id="sa-1", goal="g", awaiting=True)

    assert emitted == []
