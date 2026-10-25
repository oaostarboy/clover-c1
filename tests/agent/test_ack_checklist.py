"""The helper ack as a live checklist: rendering, tick mapping, final state."""

from __future__ import annotations

import pytest

from agent import ack_checklist as ac
from agent import foreground_time_limit as fl
from agent import router_backstop as rb

HEAD = "\U0001f504 fixing the helper-card bugs \u2192 test build + PR, ~10\u201325 min"
STEPS = ("found the bugs", "writing the fix", "tests", "test bot")


@pytest.fixture(autouse=True)
def _clean():
    ac.reset_for_tests()
    yield
    ac.reset_for_tests()


def _todos(*statuses):
    return [{"id": f"s{i + 1}", "status": s} for i, s in enumerate(statuses)]


def _receipt(did="d1", summary=HEAD, steps=STEPS, say="", goals=("g",)):
    return fl.DispatchReceipt(
        delegation_id=did, seq=1, goals=tuple(goals), summary=summary, steps=tuple(steps), say=say,
    )


# ── validation ─────────────────────────────────────────────────────────────

def test_steps_are_validated_trimmed_and_capped():
    raw = ["  a  ", "", 5, "x" * 200, "b\nsecond line", "c", "d", "e", "f", "g"]
    out = ac.clean_steps(raw)
    assert len(out) == ac.MAX_STEPS == 6
    assert out[0] == "a" and out[2] == "b" and "second" not in " ".join(out)
    assert all(len(s) <= ac.MAX_STEP_CHARS for s in out)
    assert out[1].endswith("\u2026")
    assert ac.clean_steps("not a list") == () and ac.clean_steps(None) == ()


def test_say_is_preserved_and_blank_is_dropped():
    assert ac.clean_say("  on it,   one sec. ") == "on it,   one sec."
    assert ac.clean_say("   ") == "" and ac.clean_say(None) == ""
    long = ("word " * 80).strip()
    out = ac.clean_say(long)
    assert out == long


# ── rendering ──────────────────────────────────────────────────────────────

def test_render_with_steps_first_active_rest_pending():
    cl = ac.Checklist.start(HEAD, STEPS)
    assert cl.render().splitlines() == [
        HEAD, "\U0001f504 found the bugs", "\u2b1c writing the fix", "\u2b1c tests", "\u2b1c test bot",
    ]


def test_render_without_steps_is_the_one_line_header():
    assert ac.Checklist.start(HEAD, ()).render() == HEAD


def test_say_is_the_first_line_then_a_blank_line_and_never_filled_in():
    cl = ac.Checklist.start(HEAD, ("one",), say="on it, sorting the card bugs now.")
    lines = cl.render().split("\n")
    assert lines[0] == "on it, sorting the card bugs now." and lines[1] == "" and lines[2] == HEAD
    assert ac.Checklist.start(HEAD, ("one",)).render().split("\n")[0] == HEAD


# ── todo event -> checklist state ──────────────────────────────────────────

def test_todo_statuses_map_to_icons_by_position():
    cl = ac.Checklist.start(HEAD, STEPS)
    assert cl.apply_todos(_todos("completed", "in_progress", "pending", "pending"))
    body = cl.render().splitlines()[1:]
    assert body == [
        "\U0001f340 found the bugs", "\U0001f504 writing the fix", "\u2b1c tests", "\u2b1c test bot",
    ]
    assert not cl.apply_todos(_todos("completed", "in_progress", "pending", "pending")), "no change"


def test_helper_added_items_and_unknown_statuses_are_ignored():
    cl = ac.Checklist.start(HEAD, STEPS[:2])
    cl.apply_todos(_todos("completed", "weird", "completed", "completed"))
    assert [r[1] for r in cl.steps] == ["completed", "pending"]


def test_cancelled_step_shows_stopped_icon():
    cl = ac.Checklist.start(HEAD, STEPS[:2])
    cl.apply_todos(_todos("completed", "cancelled"))
    assert cl.render().splitlines()[2].startswith("⏹")


# ── final state ────────────────────────────────────────────────────────────

def test_done_flips_header_and_every_step_and_adds_no_result_line():
    cl = ac.Checklist.start(HEAD, STEPS, say="on it.")
    cl.apply_todos(_todos("completed", "in_progress", "pending", "pending"))
    assert cl.finish(ac.DONE)
    lines = cl.render().split("\n")
    assert lines[0] == "on it." and lines[2].startswith("\U0001f340 fixing the helper-card bugs")
    assert lines[3:] == [f"\U0001f340 {s}" for s in STEPS]
    assert len(lines) == 3 + len(STEPS), "no extra result line"


def test_failure_marks_the_failing_step_and_header_x_and_nothing_else():
    cl = ac.Checklist.start(HEAD, STEPS)
    cl.apply_todos(_todos("completed", "in_progress", "pending", "pending"))
    cl.finish(ac.FAILED)
    lines = cl.render().splitlines()
    assert lines[0].startswith("\U0001f940 ")
    assert lines[1:] == [
        "\U0001f340 found the bugs", "\U0001f940 writing the fix", "\u2b1c tests", "\u2b1c test bot",
    ]


def test_stopped_uses_the_stop_icon_and_a_second_finish_changes_nothing():
    cl = ac.Checklist.start(HEAD, STEPS[:2])
    assert cl.finish(ac.STOPPED)
    assert cl.render().startswith("⏹ ")
    assert not cl.finish(ac.DONE) and cl.state == ac.STOPPED
    assert not cl.apply_todos(_todos("completed", "completed")), "a finished list never regresses"


@pytest.mark.parametrize("status,state", [
    ("completed", ac.DONE), ("interrupted", ac.STOPPED), ("failed", ac.FAILED),
    ("timeout", ac.FAILED), ("error", ac.FAILED),
])
def test_child_status_to_end_state(status, state):
    assert ac.end_state_for(status) == state


# ── dispatch wiring: events -> part -> card text ───────────────────────────

def test_ack_text_with_steps_and_say_and_without():
    text = rb._ack_text([_receipt(say="on it.")])
    assert text.split("\n")[:3] == ["on it.", "", HEAD]
    assert text.endswith("\u2b1c test bot")
    plain = rb._ack_text([_receipt(did="d2", summary=HEAD, steps=(), say="")])
    assert plain == HEAD, "no steps, no say: the one-line version, unchanged"


def test_events_tick_the_card_and_complete_finishes_it():
    receipt = _receipt()
    card = ac.card_for([receipt])
    changed = []
    card.on_change = lambda: changed.append(card.render())
    assert ac.on_child_event("subagent.todo", delegation_id="d1",
                             todos=_todos("completed", "in_progress", "pending", "pending"))
    assert "\U0001f340 found the bugs" in changed[-1]
    assert ac.on_child_event("subagent.complete", delegation_id="d1", status="completed", outcome="done")
    assert card.finished
    assert changed[-1].splitlines()[0].startswith("\U0001f340 ")
    assert not ac.on_child_event("subagent.todo", delegation_id="other", todos=[])


def test_a_helper_that_says_not_done_never_gets_a_clover():
    card = ac.card_for([_receipt()])
    assert ac.on_child_event("subagent.complete", delegation_id="d1",
                             status="completed", outcome="not_done")
    first = card.render().splitlines()[0]
    assert first.startswith("☘️ ") and "unfinished" in first and not first.startswith("\U0001f340")


@pytest.mark.parametrize("text, want", [
    ("did it\nResult: done\nPlain summary: ok.", "done"),
    ("Result: not done\nPlain summary: wrong folder.", "not_done"),
    ("**Result:** Not done - blocked", "not_done"),
    ("Result: done\n...\nResult: not done", "not_done"),
    ("no line at all", ""),
    (None, ""),
])
def test_helper_outcome_reads_the_result_line(text, want):
    assert ac.helper_outcome(text) == want


def test_events_that_arrive_before_the_receipt_are_replayed():
    ac.on_child_event("subagent.todo", delegation_id="d1",
                      todos=_todos("completed", "completed", "in_progress", "pending"))
    card = ac.card_for([_receipt()])
    assert card.render().splitlines()[2] == "\U0001f340 writing the fix"


def test_a_fan_out_finishes_only_when_every_child_reported():
    receipt = _receipt(steps=(), goals=("a", "b"))
    card = ac.card_for([receipt])
    ac.on_child_event("subagent.complete", delegation_id="d1", status="completed", outcome="done")
    assert not card.finished
    ac.on_child_event("subagent.complete", delegation_id="d1", status="failed")
    assert card.finished and card.render().startswith("\U0001f940 ")


def test_multi_task_dispatch_never_gets_steps():
    steps = fl._handoff_steps({"steps": ["a", "b"]}, ["g1", "g2"])
    assert steps == ()
    assert fl._handoff_steps({"steps": ["a", "b"]}, ["g1"]) == ("a", "b")


def test_receipt_carries_steps_and_say_from_the_handoff():
    class A:
        pass

    a = A()
    fl.note_own_dispatch(
        a, "d7", goals=["g"],
        handoff={"work": "fixing x", "outcome": "a PR", "steps": ["one", "two"], "say": "on it."},
    )
    (r,) = fl.own_dispatch_receipts(a)
    assert r.steps == ("one", "two") and r.say == "on it." and r.summary.startswith("\U0001f504 fixing x")
    assert "d7" in ac._PARTS


# ── the real relay: child todo tool result -> checklist ────────────────────

def test_child_todo_tool_result_ticks_the_checklist_through_the_real_relay():
    import json

    from tools.delegate_tool import _build_child_progress_callback

    ac.card_for([_receipt()])
    seen = []
    parent = type("P", (), {"tool_progress_callback": lambda self, *a, **k: seen.append(a[0]),
                            "_delegate_spinner": None})()
    cb = _build_child_progress_callback(
        0, "goal", parent, 1, subagent_id="sa-1", session_ref={"delegation_id": "d1"},
    )
    todo_result = json.dumps({"todos": [
        {"id": "s1", "content": "found the bugs", "status": "completed"},
        {"id": "s2", "content": "writing the fix", "status": "in_progress"},
        {"id": "s3", "content": "tests", "status": "pending"},
        {"id": "s4", "content": "test bot", "status": "pending"},
    ]})
    cb("tool.completed", "todo", None, None, duration=0.1, is_error=False, result=todo_result)
    card = ac.card_for([_receipt()])
    assert card.render().splitlines()[1:3] == ["\U0001f340 found the bugs", "\U0001f504 writing the fix"]
    assert "subagent.todo" not in seen, "the todo relay stays in-process"

    cb("subagent.complete", preview="done", status="completed", outcome="done", summary="All good.")
    assert card.finished and card.render().startswith("\U0001f340 ")


def test_seeding_and_instruction_for_the_child():
    seeded = ac.seed_todos(STEPS)
    assert [t["status"] for t in seeded] == ["in_progress", "pending", "pending", "pending"]
    assert [t["content"] for t in seeded] == list(STEPS)
    text = ac.child_instruction(STEPS)
    assert "todo" in text and "found the bugs" in text
