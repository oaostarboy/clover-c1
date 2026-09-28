"""Subagents that run out of steps resume with a fresh budget (delegation.auto_continue)."""

from __future__ import annotations

from tools import delegate_tool as dt
from tools.delegate_tool import (
    AUTO_CONTINUE_MESSAGE,
    _auto_continue_child,
    _auto_continue_limit,
    _run_single_child,
    _stopped_on_step_budget,
)


def _leg(final, *, exhausted, tool_calls=1, prior=None):
    msgs = list(prior or [])
    msgs.append({"role": "user", "content": "go"})
    for i in range(tool_calls):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{len(msgs)}{i}", "function": {"name": "terminal", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{len(msgs)-1}{i}", "content": "ok"})
    msgs.append({"role": "assistant", "content": final})
    return {
        "final_response": final,
        "completed": not exhausted,
        "api_calls": tool_calls + 1,
        "messages": msgs,
        "turn_exit_reason": "max_iterations_reached(5/5)" if exhausted else "text_response(finish_reason=stop)",
    }


class _Budget:
    def __init__(self, n):
        self.max_total = n


class _Child:
    model = "m"
    session_prompt_tokens = 0
    session_completion_tokens = 0
    session_estimated_cost_usd = 0.0
    session_reasoning_tokens = 0
    _delegate_output_schema = None

    def __init__(self, legs):
        self.legs = list(legs)
        self.calls = []
        self.iteration_budget = _Budget(5)
        self.max_iterations = 5

    def get_activity_summary(self):
        return {"api_call_count": 1, "max_iterations": 5, "current_tool": None}

    def run_conversation(self, user_message, task_id=None, conversation_history=None, **_kw):
        self.calls.append((user_message, len(conversation_history or [])))
        leg = self.legs.pop(0)
        return leg(conversation_history) if callable(leg) else leg

    def close(self):
        return None


class _Parent:
    _current_task_id = None
    _delegate_depth = 0

    def _touch_activity(self, _d):
        return None


def test_stopped_on_step_budget_only_for_budget_reasons():
    assert _stopped_on_step_budget({"turn_exit_reason": "max_iterations_reached(5/5)"})
    assert _stopped_on_step_budget({"turn_exit_reason": "budget_exhausted"})
    assert not _stopped_on_step_budget({"turn_exit_reason": "text_response(finish_reason=stop)"})
    assert not _stopped_on_step_budget({"turn_exit_reason": "budget_exhausted", "interrupted": True})
    assert not _stopped_on_step_budget({"turn_exit_reason": "budget_exhausted", "failed": True})
    assert not _stopped_on_step_budget(None)


def test_limit_reads_config_and_disables(monkeypatch):
    assert _auto_continue_limit({}) == dt.DEFAULT_AUTO_CONTINUE
    assert _auto_continue_limit({"auto_continue": 0}) == 0
    assert _auto_continue_limit({"auto_continue": False}) == 0
    assert _auto_continue_limit({"auto_continue": "3"}) == 3
    assert _auto_continue_limit({"auto_continue": "junk"}) == dt.DEFAULT_AUTO_CONTINUE


def test_exhausted_child_resumes_and_finishes(monkeypatch):
    monkeypatch.setattr(dt, "_load_config", lambda: {"auto_continue": 2})
    first = _leg("partial: did half", exhausted=True)
    child = _Child([first, lambda h: _leg("DONE all of it", exhausted=False, prior=h)])
    entry = _run_single_child(0, "do the task", child, _Parent())
    assert entry["summary"] == "DONE all of it"
    assert entry["exit_reason"] == "completed"
    assert entry["truncated"] is False
    assert entry["auto_continuations"] == 1
    # resumed with its own transcript and the continue notice
    assert child.calls[1][0] == AUTO_CONTINUE_MESSAGE
    assert child.calls[1][1] == len(first["messages"])


def test_budget_is_refreshed_before_resume(monkeypatch):
    monkeypatch.setattr(dt, "_load_config", lambda: {"auto_continue": 1})
    child = _Child([_leg("half", exhausted=True), lambda h: _leg("done", exhausted=False, prior=h)])
    old = child.iteration_budget
    _run_single_child(0, "t", child, _Parent())
    assert child.iteration_budget is not old
    assert child.iteration_budget.max_total == 5
    assert child.iteration_budget.remaining == 5


def test_stops_at_limit_and_reports_truncated(monkeypatch):
    monkeypatch.setattr(dt, "_load_config", lambda: {"auto_continue": 2})
    child = _Child([
        _leg("a", exhausted=True),
        lambda h: _leg("b", exhausted=True, prior=h),
        lambda h: _leg("c", exhausted=True, prior=h),
        lambda h: _leg("never", exhausted=False, prior=h),
    ])
    entry = _run_single_child(0, "t", child, _Parent())
    assert len(child.calls) == 3
    assert entry["auto_continuations"] == 2
    assert entry["truncated"] is True
    assert entry["summary"] == "c"


def test_stops_when_a_leg_makes_no_progress(monkeypatch):
    monkeypatch.setattr(dt, "_load_config", lambda: {"auto_continue": 5})
    child = _Child([
        _leg("a", exhausted=True),
        lambda h: _leg("b", exhausted=True, tool_calls=0, prior=h),
        lambda h: _leg("never", exhausted=False, prior=h),
    ])
    entry = _run_single_child(0, "t", child, _Parent())
    assert len(child.calls) == 2
    assert entry["auto_continuations"] == 1


def test_disabled_keeps_old_behaviour(monkeypatch):
    monkeypatch.setattr(dt, "_load_config", lambda: {"auto_continue": 0})
    child = _Child([_leg("partial", exhausted=True)])
    entry = _run_single_child(0, "t", child, _Parent())
    assert len(child.calls) == 1
    assert entry["truncated"] is True
    assert entry["auto_continuations"] == 0


def test_finished_child_is_not_continued(monkeypatch):
    monkeypatch.setattr(dt, "_load_config", lambda: {"auto_continue": 2})
    child = _Child([_leg("done", exhausted=False)])
    entry = _run_single_child(0, "t", child, _Parent())
    assert len(child.calls) == 1
    assert entry["auto_continuations"] == 0


def test_empty_continuation_keeps_previous_summary(monkeypatch):
    monkeypatch.setattr(dt, "_load_config", lambda: {"auto_continue": 1})
    def blank(h):
        r = _leg("", exhausted=True, prior=h)
        r["final_response"] = ""
        return r
    child = _Child([_leg("half done", exhausted=True), blank])
    entry = _run_single_child(0, "t", child, _Parent())
    assert entry["summary"] == "half done"


def test_config_default_is_on():
    from clover_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["delegation"]["auto_continue"] == 2
