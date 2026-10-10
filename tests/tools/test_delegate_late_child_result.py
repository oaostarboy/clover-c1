"""A child whose result lands moments AFTER the wait gave up must be collected.

When the configured child timeout elapses while the worker is still unwinding
from its final answer, the real result used to be thrown away for a synthesized
timeout entry — in an async batch that strands a finished child. After the
timeout we signal the cooperative stop, then poll the worker for a short grace
window and prefer the real result.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from tools import delegate_tool


class _Child:
    def __init__(self, session_id: str) -> None:
        self.tool_progress_callback = None
        self._credential_pool = None
        self._delegate_saved_tool_names = []
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self._subagent_id = None
        self.session_id = session_id
        self.release = threading.Event()
        self.interrupted = threading.Event()

    def run_conversation(self, **_kwargs):
        self.release.wait()
        return {"final_response": f"DONE-{self.session_id}", "completed": True, "api_calls": 3, "messages": []}

    def get_activity_summary(self):
        return {"api_call_count": 3, "current_tool": None, "last_activity_ts": 1000.0, "max_iterations": 50}

    unwind_delay: float | None = None  # set => worker lands this long after the stop signal

    def hard_interrupt(self, *_args, **_kwargs):
        self.interrupted.set()
        if self.unwind_delay is not None:
            t = threading.Timer(self.unwind_delay, self.release.set)
            t.daemon = True
            t.start()

    def close(self):
        self.release.set()


def _parent():
    return SimpleNamespace(
        session_id="parent",
        _current_task_id=None,
        _active_children=[],
        _active_children_lock=threading.Lock(),
        _touch_activity=lambda _desc: None,
        _interrupt_requested=False,
    )


def _setup(monkeypatch, *, timeout: float, grace: float):
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: timeout)
    monkeypatch.setattr(delegate_tool, "_get_worktree_isolation", lambda: False)
    # raising=False so the test fails on BEHAVIOR on the unfixed base.
    monkeypatch.setattr(delegate_tool, "_STALE_RESULT_GRACE_SECONDS", grace, raising=False)


def _release_later(child: _Child, delay: float) -> threading.Timer:
    t = threading.Timer(delay, child.release.set)
    t.daemon = True
    t.start()
    return t


def test_result_landing_inside_grace_after_timeout_is_collected(monkeypatch):
    child = _Child("late")
    child.unwind_delay = 0.3  # lands after the stop signal, inside the grace
    _setup(monkeypatch, timeout=0.2, grace=2.0)
    try:
        entry = delegate_tool._run_single_child(0, "review", child=child, parent_agent=_parent())
    finally:
        child.release.set()
    assert entry["status"] == "completed", entry
    assert entry["summary"] == "DONE-late", entry
    assert child.interrupted.is_set()  # cooperative stop was still signalled


def test_no_result_inside_grace_is_still_a_timeout(monkeypatch):
    child = _Child("wedged")
    _setup(monkeypatch, timeout=0.2, grace=0.1)
    valve = _release_later(child, 5.0)
    started = time.monotonic()
    try:
        entry = delegate_tool._run_single_child(0, "review", child=child, parent_agent=_parent())
    finally:
        valve.cancel()
        child.release.set()
    assert time.monotonic() - started < 4
    assert entry["status"] == "timeout", entry
    assert entry["summary"] is None
