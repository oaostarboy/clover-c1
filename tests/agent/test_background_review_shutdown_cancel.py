"""Gateway-shutdown cancellation of in-flight background reviews.

Regression coverage for the restart-stall incident (2026-09-28, macOS/Suni):
the gateway logged "Gateway stopped (total teardown 3.92s)" but the process
did not actually exit for ~5 minutes because a background self-improvement
review (agent/background_review.py) kept replaying a ~190k-token context —
an auxiliary compression request timed out, fell back to another provider,
and only then did the review finish and the process exit. Nothing cancelled
the review at shutdown, and nothing bounded how long the process could wait
around it.

These tests exercise ``agent.background_review.cancel_all_background_reviews``
and the process-wide review registry it walks. They must FAIL on
unmodified ``origin/main`` (no such registry/cancellation exists there).
"""

from __future__ import annotations

import threading
import time

import pytest

import agent.background_review as bg
import run_agent as run_agent_module
from run_agent import AIAgent


_REAL_THREAD = threading.Thread


def _bare_agent(session_id: str = "test-session") -> AIAgent:
    agent = object.__new__(AIAgent)
    agent.model = "fake-model"
    agent.platform = "telegram"
    agent.provider = "openai"
    agent.base_url = ""
    agent.api_key = ""
    agent.api_mode = ""
    agent.session_id = session_id
    agent._parent_session_id = ""
    agent._credential_pool = None
    agent._memory_store = object()
    agent._memory_enabled = True
    agent._user_profile_enabled = False
    agent._cached_system_prompt = "test-cached-system-prompt"
    import datetime as _dt

    agent.session_start = _dt.datetime(2026, 1, 1, 12, 0, 0)
    agent._MEMORY_REVIEW_PROMPT = "review memory"
    agent._SKILL_REVIEW_PROMPT = "review skills"
    agent._COMBINED_REVIEW_PROMPT = "review both"
    agent.background_review_callback = None
    agent.status_callback = None
    agent._safe_print = lambda *_args, **_kwargs: None
    agent._background_review_agent = None
    agent._background_review_run = None
    agent._background_review_lock = threading.Lock()
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    return agent


class CapturingThread:
    """Stand-in for ``threading.Thread`` that just remembers its target."""

    targets: list = []

    def __init__(self, *, target, daemon=None, name=None):
        self.targets.append(target)

    def start(self):
        pass


class FakeReviewAgent:
    def __init__(self, **kwargs):
        self._session_messages = []

    def run_conversation(self, **kwargs):
        pass

    def interrupt(self, message=None):
        pass

    def shutdown_memory_provider(self):
        pass

    def close(self):
        pass


def _spawn_review(monkeypatch, agent, review_agent_cls):
    monkeypatch.setattr(run_agent_module, "AIAgent", review_agent_cls)
    CapturingThread.targets = []
    monkeypatch.setattr(run_agent_module.threading, "Thread", CapturingThread)
    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=[{"role": "user", "content": "hello"}],
        review_memory=True,
    )
    assert CapturingThread.targets, "background review did not spawn"
    return CapturingThread.targets[0]


def test_registry_tracks_and_clears_a_live_review(monkeypatch):
    """The registry cancel_all_background_reviews walks reflects live reviews."""
    assert bg.live_background_review_count() == 0

    agent = _bare_agent()
    run = bg.prepare_background_review_run(agent)
    assert run is not None
    assert bg.live_background_review_count() == 1

    bg.finish_background_review_run(agent, run)
    assert bg.live_background_review_count() == 0


def test_cancel_all_background_reviews_noop_when_registry_empty():
    assert bg.live_background_review_count() == 0
    assert bg.cancel_all_background_reviews(grace_seconds=0.05) == []


def test_cancel_all_background_reviews_bounded_against_a_wedged_60s_review(monkeypatch):
    """A fake slow review (simulating the live 60s+ wedge) must not delay
    cancellation past the configured grace — cancellation is requested and
    the caller gets control back quickly regardless of what the review
    thread is doing."""
    review_entered = threading.Event()
    interrupt_entered = threading.Event()
    allow_review_return = threading.Event()

    class WedgedReviewAgent(FakeReviewAgent):
        def run_conversation(self, **kwargs):
            review_entered.set()
            # Simulates the live incident's multi-minute stall (auxiliary
            # compression timeout + provider fallback) without actually
            # burning 60s of CI time: released explicitly at teardown.
            allow_review_return.wait(60.0)

        def interrupt(self, message=None):
            interrupt_entered.set()
            # A broken/no-op abort path — never actually stops the review.

    target = _spawn_review(monkeypatch, agent := _bare_agent(), WedgedReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", _REAL_THREAD)
    worker = _REAL_THREAD(target=target, daemon=True)
    worker.start()
    try:
        assert review_entered.wait(2.0)
        assert bg.live_background_review_count() == 1

        started = time.monotonic()
        still_running = bg.cancel_all_background_reviews(grace_seconds=0.3)
        elapsed = time.monotonic() - started

        # Bounded: nowhere near the simulated 60s wedge.
        assert elapsed < 2.0
        assert interrupt_entered.wait(2.0)
        assert still_running, "wedged review should be reported as still running"
        assert any("bg-review" in name for name in still_running)
    finally:
        allow_review_return.set()
        worker.join(timeout=2.0)


def test_cancel_all_background_reviews_returns_empty_once_acknowledged(monkeypatch):
    """A review that finishes promptly is not reported as still-running, and
    the registry is cleared without waiting out the full grace."""
    review_done = threading.Event()

    class QuickReviewAgent(FakeReviewAgent):
        def run_conversation(self, **kwargs):
            pass

    target = _spawn_review(monkeypatch, agent := _bare_agent(), QuickReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", _REAL_THREAD)
    worker = _REAL_THREAD(target=target, daemon=True)
    worker.start()
    worker.join(timeout=2.0)
    assert not worker.is_alive()

    assert bg.live_background_review_count() == 0
    assert bg.cancel_all_background_reviews(grace_seconds=1.0) == []


def test_memory_write_stays_intact_if_interrupted_mid_write(tmp_path, monkeypatch):
    """A background review cancelled mid-write must never leave the memory
    file partially written. ``MemoryStore._write_file`` goes through
    ``atomic_write_text`` (temp file + fsync + atomic rename) — simulate the
    interruption landing right where a cancelled review would hit it (after
    the new content is fully buffered, before it becomes visible) and assert
    the target is left exactly as it was: never a partial blend."""
    from tools.memory_tool import MemoryStore
    import utils as utils_module

    target = tmp_path / "MEMORY.md"
    target.write_text("original content\n", encoding="utf-8")

    def _interrupted_replace(*args, **kwargs):
        raise OSError("simulated interruption mid-write")

    monkeypatch.setattr(utils_module, "atomic_replace", _interrupted_replace)

    with pytest.raises(RuntimeError):
        MemoryStore._write_file(target, ["new entry one", "new entry two"])

    assert target.read_text(encoding="utf-8") == "original content\n"
    assert list(tmp_path.glob(".mem_*")) == []


def test_memory_write_succeeds_fully_when_not_interrupted(tmp_path):
    """Sanity counterpart: an uninterrupted write is fully applied — the
    atomicity guarantee is 'all or nothing', not 'nothing'."""
    from tools.memory_tool import ENTRY_DELIMITER, MemoryStore

    target = tmp_path / "MEMORY.md"
    target.write_text("original content\n", encoding="utf-8")

    MemoryStore._write_file(target, ["new entry one", "new entry two"])

    assert target.read_text(encoding="utf-8") == ENTRY_DELIMITER.join(
        ["new entry one", "new entry two"]
    )
