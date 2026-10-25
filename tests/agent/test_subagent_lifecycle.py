"""Contract tests for the public plugin subagent lifecycle API."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.subagent_lifecycle import (
    SubagentLaunchRequest,
    SubagentLifecycleError,
    SubagentLifecycleService,
    SubagentState,
    bind_subagent_parent,
    get_active_subagent_parent,
)


class FakeChild:
    def __init__(self, ident="sa-test"):
        self._subagent_id = ident
        self._delegate_role = "leaf"
        self._delegate_depth = 1
        self.provider = "test"
        self.model = "test-model"
        self.interrupted = False
        self.interrupt_kind = None
        self.interrupt_message = None
        self.tool_reason = None
        self.closed = False

    def close(self):
        self.closed = True

    def interrupt(self, _reason):
        self.interrupted = True
        self.interrupt_kind = "soft"

    def hard_interrupt(self, reason, *, tool_reason=None):
        self.interrupted = True
        self.interrupt_kind = "hard"
        self.interrupt_message = reason
        self.tool_reason = tool_reason


@pytest.fixture
def lifecycle(monkeypatch):
    parent = SimpleNamespace(session_id="parent-1", enabled_toolsets=["file"])
    counter = iter(range(1000))

    def build(**_kwargs):
        return FakeChild(f"sa-{next(counter)}")

    def run(_index, _goal, child, _parent):
        for _ in range(20):
            if child.interrupted:
                return {
                    "status": "interrupted",
                    "summary": None,
                    "api_calls": 0,
                    "duration_seconds": 0,
                }
            time.sleep(0.002)
        return {
            "status": "completed",
            "summary": "safe summary",
            "api_calls": 1,
            "duration_seconds": 0.01,
        }

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", build)
    monkeypatch.setattr("tools.delegate_tool._run_single_child", run)
    return SubagentLifecycleService(lambda: parent)






def test_cancel_is_cooperative_and_forged_handle_is_unknown(lifecycle):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="x"))
    assert lifecycle.cancel(handle, reason="test").accepted
    terminal = lifecycle.wait(handle, timeout_seconds=1)
    assert terminal.state is SubagentState.CANCELLED
    forged = handle.__class__(**{**handle.to_dict(), "capability": "forged"})
    assert lifecycle.status(forged).state is SubagentState.UNKNOWN
    assert lifecycle.result(forged).error_classification == "UNKNOWN_HANDLE"
    other_parent = SimpleNamespace(session_id="different-parent")
    other_service = SubagentLifecycleService(lambda: other_parent)
    assert other_service.status(handle).state is SubagentState.UNKNOWN


def test_cancel_uses_explicit_hard_interrupt(lifecycle):
    handle = lifecycle.launch(SubagentLaunchRequest(goal="x"))
    record = lifecycle._record(handle)
    assert record is not None and record.agent is not None

    assert lifecycle.cancel(handle, reason="explicit user cancel").accepted

    assert record.agent.interrupt_kind == "hard"
    assert "explicit user cancel" in record.agent.interrupt_message
    assert record.agent.tool_reason == "subagent cancellation requested"
    lifecycle.wait(handle, timeout_seconds=1)








def test_public_lifecycle_runs_host_aggregation(monkeypatch):
    memory = Mock()
    parent = SimpleNamespace(
        session_id="parent-aggregate",
        enabled_toolsets=["file"],
        _memory_manager=memory,
        _current_turn_id="turn-1",
        session_estimated_cost_usd=1.0,
        session_cost_source="none",
        session_cost_status="unknown",
    )
    child = FakeChild("sa-aggregate")
    child.session_id = "child-session"
    hook = Mock()

    monkeypatch.setattr("tools.delegate_tool._build_child_agent", lambda **_kwargs: child)
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda *_args, **_kwargs: {
            "task_index": 0,
            "status": "completed",
            "summary": "aggregated",
            "api_calls": 1,
            "duration_seconds": 0.25,
            "_child_role": "leaf",
            "_child_cost_usd": 2.5,
        },
    )
    monkeypatch.setattr("clover_cli.plugins.invoke_hook", hook)

    service = SubagentLifecycleService(lambda: parent)
    handle = service.launch(SubagentLaunchRequest(goal="aggregate me"))
    assert service.wait(handle, timeout_seconds=1).state is SubagentState.SUCCEEDED

    memory.on_delegation.assert_called_once_with(
        task="aggregate me", result="aggregated", child_session_id="child-session"
    )
    hook.assert_called_once_with(
        "subagent_stop",
        parent_session_id="parent-aggregate",
        parent_turn_id="turn-1",
        child_session_id="child-session",
        child_role="leaf",
        child_summary="aggregated",
        child_status="completed",
        # Redacted tool history rides the shared finalization pipeline
        # (#62011/#72403); empty here because the fabricated result carries
        # no tool_trace.
        tool_call_history=[],
        duration_ms=250,
    )
    assert parent.session_estimated_cost_usd == 3.5
    assert parent.session_cost_source == "subagent"
    assert parent.session_cost_status == "estimated"




def test_agent_turn_binds_and_clears_lifecycle_parent(monkeypatch):
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    observed = []

    def run_conversation(parent, *_args, **_kwargs):
        observed.append(get_active_subagent_parent())
        return {"final_response": "ok"}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", run_conversation)

    assert agent.run_conversation("hello") == {"final_response": "ok"}
    assert observed == [agent]
    assert get_active_subagent_parent() is None


def test_same_correlation_is_reserved_before_child_construction(monkeypatch):
    import threading
    from concurrent.futures import Future

    import agent.subagent_lifecycle as lifecycle_module

    parent = SimpleNamespace(session_id="parent-race", enabled_toolsets=[])
    builder_started = threading.Event()
    release_builder = threading.Event()
    builds = []
    submissions = []

    build_lock = threading.Lock()
    launch_barrier = threading.Barrier(3)
    one_launch_done = threading.Event()

    def build(**_kwargs):
        with build_lock:
            builds.append(object())
            build_number = len(builds)
        if build_number == 1:
            builder_started.set()
            assert release_builder.wait(timeout=2), "builder was not released"
        return FakeChild(f"sa-race-{build_number}")

    class NoDispatchExecutor:
        def submit(self, *args):
            submissions.append(args)
            return Future()

    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", build)
    monkeypatch.setattr(lifecycle_module, "_EXECUTOR", NoDispatchExecutor())
    monkeypatch.setattr(lifecycle_module, "_REGISTRY", lifecycle_module._Registry())
    service = SubagentLifecycleService(lambda: parent)
    request = SubagentLaunchRequest(goal="race", correlation_id="same-request")
    accepted = []
    errors = []

    def launch():
        try:
            launch_barrier.wait(timeout=2)
            accepted.append(service.launch(request))
        except Exception as exc:  # surface worker errors in the main test thread
            errors.append(exc)
        finally:
            one_launch_done.set()

    workers = [threading.Thread(target=launch, daemon=True) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        launch_barrier.wait(timeout=2)
        assert builder_started.wait(timeout=2), "no caller reached the child builder"
        assert one_launch_done.wait(timeout=2), "second caller stayed inside the builder"
    finally:
        release_builder.set()
    for worker in workers:
        worker.join(timeout=2)
        assert not worker.is_alive(), "launch worker leaked"
    assert len(accepted) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], SubagentLifecycleError)
    assert "Duplicate correlation_id" in str(errors[0])
    assert len(builds) == 1
    assert len(submissions) == 1
    with pytest.raises(SubagentLifecycleError, match="Duplicate correlation_id"):
        service.launch(request)
    assert len(builds) == 1
    assert len(submissions) == 1


def test_builder_failure_releases_only_its_correlation_reservation(monkeypatch):
    from concurrent.futures import Future

    import agent.subagent_lifecycle as lifecycle_module

    parent = SimpleNamespace(session_id="parent-build-fail", enabled_toolsets=[])
    submissions = []
    builds = []

    def build(**_kwargs):
        builds.append(None)
        if len(builds) == 1:
            raise RuntimeError("synthetic construction failure")
        return FakeChild("sa-build-retry")

    class NoDispatchExecutor:
        def submit(self, *args):
            submissions.append(args)
            return Future()

    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", build)
    monkeypatch.setattr(lifecycle_module, "_EXECUTOR", NoDispatchExecutor())
    monkeypatch.setattr(lifecycle_module, "_REGISTRY", lifecycle_module._Registry())
    service = SubagentLifecycleService(lambda: parent)
    request = SubagentLaunchRequest(goal="retry build", correlation_id="owned-key")

    with pytest.raises(RuntimeError, match="synthetic construction failure"):
        service.launch(request)
    handle = service.launch(request)

    assert handle.subagent_id == "sa-build-retry"
    assert len(builds) == 2
    assert len(submissions) == 1


def test_executor_submission_failure_removes_record_and_reservation(monkeypatch):
    from concurrent.futures import Future

    import agent.subagent_lifecycle as lifecycle_module

    parent = SimpleNamespace(session_id="parent-submit-fail", enabled_toolsets=[])
    builds = []
    submissions = []
    children = []

    def build(**_kwargs):
        builds.append(None)
        child = FakeChild(f"sa-submit-{len(builds)}")
        children.append(child)
        return child

    class FailOnceExecutor:
        def submit(self, *args):
            submissions.append(args)
            if len(submissions) == 1:
                raise RuntimeError("synthetic submit failure")
            return Future()

    registry = lifecycle_module._Registry()
    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", build)
    monkeypatch.setattr(lifecycle_module, "_EXECUTOR", FailOnceExecutor())
    monkeypatch.setattr(lifecycle_module, "_REGISTRY", registry)
    service = SubagentLifecycleService(lambda: parent)
    request = SubagentLaunchRequest(goal="retry submit", correlation_id="owned-key")

    with pytest.raises(RuntimeError, match="synthetic submit failure"):
        service.launch(request)
    assert registry.records == {}
    assert registry.correlations == {}
    assert children[0].closed
    handle = service.launch(request)

    assert handle.subagent_id == "sa-submit-2"
    assert len(builds) == 2
    assert len(submissions) == 2
    assert not children[1].closed
    assert list(registry.records) == [handle.subagent_id]


def test_distinct_correlation_progresses_while_other_builder_is_slow(monkeypatch):
    import threading
    from concurrent.futures import Future

    import agent.subagent_lifecycle as lifecycle_module

    parent = SimpleNamespace(session_id="parent-distinct", enabled_toolsets=[])
    slow_started = threading.Event()
    release_slow = threading.Event()
    fast_done = threading.Event()
    submissions = []

    def build(**kwargs):
        if kwargs["goal"] == "slow":
            slow_started.set()
            assert release_slow.wait(timeout=2), "slow builder was not released"
        return FakeChild(f"sa-{kwargs['goal']}")

    class NoDispatchExecutor:
        def submit(self, *args):
            submissions.append(args)
            return Future()

    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", build)
    monkeypatch.setattr(lifecycle_module, "_EXECUTOR", NoDispatchExecutor())
    monkeypatch.setattr(lifecycle_module, "_REGISTRY", lifecycle_module._Registry())
    service = SubagentLifecycleService(lambda: parent)
    errors = []

    def launch_slow():
        try:
            service.launch(SubagentLaunchRequest(goal="slow", correlation_id="slow-key"))
        except Exception as exc:
            errors.append(exc)

    def launch_fast():
        try:
            service.launch(SubagentLaunchRequest(goal="fast", correlation_id="fast-key"))
        except Exception as exc:
            errors.append(exc)
        finally:
            fast_done.set()

    slow_worker = threading.Thread(target=launch_slow, daemon=True)
    fast_worker = threading.Thread(target=launch_fast, daemon=True)
    slow_worker.start()
    try:
        assert slow_started.wait(timeout=2), "slow caller never reached builder"
        fast_worker.start()
        assert fast_done.wait(timeout=2), "distinct-key launch waited on slow builder"
    finally:
        release_slow.set()
    slow_worker.join(timeout=2)
    fast_worker.join(timeout=2)
    assert not slow_worker.is_alive(), "slow launch worker leaked"
    assert not fast_worker.is_alive(), "fast launch worker leaked"
    assert errors == []
    assert len(submissions) == 2


@pytest.mark.parametrize("revocation", ["stop", "epoch"])
def test_stop_during_construction_prevents_late_submission(monkeypatch, revocation):
    import threading
    from concurrent.futures import Future

    import agent.subagent_lifecycle as lifecycle_module
    from tools.async_delegation import _records_lock

    parent = SimpleNamespace(
        session_id=f"parent-{revocation}",
        enabled_toolsets=[],
        _interrupt_requested=False,
        _orchestration_stop_epoch=7,
    )
    builder_started = threading.Event()
    release_builder = threading.Event()
    submissions = []
    registry = lifecycle_module._Registry()
    children = []

    def build(**_kwargs):
        builder_started.set()
        assert release_builder.wait(timeout=2), "builder was not released"
        child = FakeChild(f"sa-{revocation}")
        children.append(child)
        return child

    class NoDispatchExecutor:
        def submit(self, *args):
            submissions.append(args)
            return Future()

    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", build)
    monkeypatch.setattr(lifecycle_module, "_EXECUTOR", NoDispatchExecutor())
    monkeypatch.setattr(lifecycle_module, "_REGISTRY", registry)
    service = SubagentLifecycleService(lambda: parent)
    request = SubagentLaunchRequest(goal="stopped", correlation_id="stop-key")
    errors = []

    def launch():
        try:
            service.launch(request)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=launch, daemon=True)
    worker.start()
    try:
        assert builder_started.wait(timeout=2), "caller never reached builder"
        with _records_lock:
            if revocation == "stop":
                parent._interrupt_requested = True
            else:
                parent._orchestration_stop_epoch += 1
    finally:
        release_builder.set()
    worker.join(timeout=2)

    assert not worker.is_alive(), "launch worker leaked"
    assert len(errors) == 1
    assert isinstance(errors[0], SubagentLifecycleError)
    assert "stopped during child construction" in str(errors[0])
    assert submissions == []
    assert registry.records == {}
    assert registry.correlations == {}
    assert children[0].closed
