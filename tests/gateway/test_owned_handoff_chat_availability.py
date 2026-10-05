"""A handed-off request leaves the chat free and its result still integrates.

Real pieces: a real ``AIAgent`` conversation loop (scripted provider), the real
``todo`` / ``delegate_task`` / ``read_file`` tools, the real async-delegation
registry with its durable table in a scratch ``CLOVER_HOME``, and the gateway's
real completion watcher + delivery code (durable claim, ``internal`` event
injection). Stand-ins: the child's own conversation (it writes a real artifact
file) and the platform adapter, whose ``handle_message`` runs the agent turn the
way the gateway does for an inbound event (``internal`` events persist their
user row as ``internal_notification``).

NOT exercised here: ``GatewayRunner._handle_message`` and the busy-slot
bookkeeping around it. This test proves what the root and the delivery code do,
not the platform's turn plumbing.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tools.async_delegation as ad
import tools.delegate_tool as dt
from agent import delegation_checkpoint as dc
from gateway.config import Platform
from gateway.run import GatewayRunner
from tests.agent.test_handoff_completion_seam import _call, _declare, _root, _text, _tools
from tests.tools.test_delegate_strict_background import harness  # noqa: F401  (fixture)

SESSION_KEY = "agent:main:telegram:dm:12345:678"
ARTIFACT_TEXT = "EXPLAINER v1: sha-of-fixture 4f9c; 3 sections; verified by child"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path / "home"))
    import tools.process_registry as pr_module

    registry = pr_module.ProcessRegistry()
    monkeypatch.setattr(pr_module, "CHECKPOINT_PATH", tmp_path / "processes.json")
    monkeypatch.setattr(pr_module, "process_registry", registry)
    monkeypatch.setattr(
        "tools.approval.get_current_session_key", lambda default="default": SESSION_KEY
    )
    ad._reset_for_tests()
    yield registry
    ad._reset_for_tests()


@pytest.fixture
def artifact(tmp_path, harness, monkeypatch):  # noqa: F811
    """The stand-in child writes a real file, then completes when released."""
    path = tmp_path / "explainer.txt"
    harness.hold = threading.Event()

    def run(task_index, goal, child, parent_agent, **kw):
        harness.runs.append(goal)
        harness.hold.wait(60)
        path.write_text(ARTIFACT_TEXT)
        return {
            "task_index": task_index, "status": "completed",
            "summary": f"wrote {path}", "api_calls": 1,
            "duration_seconds": 0.0, "model": "m", "exit_reason": "completed",
        }

    monkeypatch.setattr(dt, "_run_single_child", run)
    yield path
    harness.hold.set()


def _runner(adapter):
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.session_store = SimpleNamespace(_ensure_loaded=lambda: None, _entries={})
    runner._session_source_cache = {}
    runner._completion_delivery_lock = threading.Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 2048
    runner._background_tasks = set()
    # Session-ownership resolution belongs to the session store, which is out
    # of scope here: the completion targets the session that spawned it.
    runner._preflight_completion = AsyncMock(
        return_value=(None, SimpleNamespace(verdict="owned", reason=""))
    )
    return runner


def _watch_once(monkeypatch, runner):
    calls = 0

    async def bounded_sleep(_delay):
        nonlocal calls
        calls += 1
        if calls >= 2:
            runner._running = False

    monkeypatch.setattr(asyncio, "sleep", bounded_sleep)
    asyncio.run(runner._async_delegation_watcher(interval=0))


def _run_turn(agent, text, *, kind=None):
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        if kind is None:
            return agent.run_conversation(text)
        return agent.run_conversation(text, persist_user_display_kind=kind)


def _tool_payloads(result):
    out = []
    for message in result["messages"]:
        if message.get("role") == "tool":
            try:
                out.append(json.loads(message["content"]))
            except ValueError:
                out.append(message["content"])
    return out


def _wait_terminal(delegation_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = ad.get_durable_delegation(delegation_id)
        if row and row["state"] not in ("running", "finalizing"):
            return row
        time.sleep(0.02)
    raise AssertionError("job never finished")


def _hand_off_script():
    return [
        _tools(_declare("delegate")),
        _tools(_call("delegate_task", {
            "goal": "Build the explainer and verify every output.",
            "context": "PRIVATE-CONTEXT-DO-NOT-ECHO",
        })),
    ]


def test_chat_stays_available_and_the_late_result_is_integrated(
    harness, artifact, tmp_path, monkeypatch, _isolated,  # noqa: F811
):
    agent = _root([
        *_hand_off_script(),
        _text("Four."),                                              # turn 2 (human)
        _tools(_call("read_file", {"path": str(artifact)}, "verify")),  # turn 3 (internal)
        _text("Verified: the explainer exists and matches."),
        _tools(_call("read_file", {"path": str(artifact)}, "replay")),  # turn 4 (replay)
        _text("Nothing to add."),
    ])

    # Turn 1: the request is handed off and the root's turn ends normally.
    first = _run_turn(agent, "Make me a reusable explainer and a phone video.")
    assert first["turn_exit_reason"] == "delegation_handoff"
    delegation_id = json.loads(
        [m for m in first["messages"] if m["role"] == "tool"][-1]["content"]
    )["delegation_id"]
    assert "PRIVATE-CONTEXT-DO-NOT-ECHO" not in first["final_response"]
    handed_off_request = dc.get_checkpoint(agent).request_id

    # Turn 2: a new human message is answered while the worker still runs.
    second = _run_turn(agent, "Unrelated: what is 2+2?")
    assert second["final_response"] == "Four."
    assert ad.get_durable_delegation(delegation_id)["state"] == "running"
    assert ad.active_count() == 1
    assert all(not child.interrupted for child in harness.built)
    assert not artifact.exists(), "the worker has not finished yet"
    assert dc.get_checkpoint(agent).request_id != handed_off_request

    # The worker finishes; the gateway's real watcher claims and injects it.
    harness.hold.set()
    _wait_terminal(delegation_id)
    delivered = []

    async def handle_message(event):
        delivered.append(event)
        # The platform turn for an inbound event; internal events persist as
        # internal_notification (gateway/run.py persist_user_display_kind).
        kind = "internal_notification" if getattr(event, "internal", False) else None
        delivered_result.append(_run_turn(agent, event.text, kind=kind))
        return None

    delivered_result: list = []
    runner = _runner(SimpleNamespace(handle_message=handle_message))
    _watch_once(monkeypatch, runner)

    assert len(delivered) == 1 and delivered[0].internal is True
    integration = delivered_result[0]
    payloads = _tool_payloads(integration)
    assert ARTIFACT_TEXT in json.dumps(payloads), "the produced artifact was verified"
    assert integration["final_response"].startswith("Verified")
    checkpoint = dc.get_checkpoint(agent)
    assert checkpoint._owned[delegation_id].consumed_receipts == {delegation_id}
    assert checkpoint._windows_by_request[handed_off_request] == 1
    assert ad.get_durable_delegation(delegation_id)["delivery_state"] == "delivered"

    # A replayed delivery of the same completion grants no second window.
    replay = _run_turn(agent, "[replayed completion]", kind="internal_notification")
    (blocked,) = _tool_payloads(replay)[:1]
    assert isinstance(blocked, dict) and blocked.get("error_type", "").startswith("delegation_")
    assert checkpoint._windows_by_request[handed_off_request] == 1


def test_cancellation_still_reaches_the_job_after_the_root_ended(
    harness, artifact, monkeypatch,  # noqa: F811
):
    interrupted = []
    monkeypatch.setattr(
        dt, "request_hard_interrupt", lambda child, reason="": interrupted.append(child) or True
    )
    agent = _root(_hand_off_script())

    result = _run_turn(agent, "Make me a reusable explainer.")

    assert result["turn_exit_reason"] == "delegation_handoff"
    assert ad.active_count() == 1
    assert not interrupted, "ending the root must not interrupt the job"
    assert ad.interrupt_for_session(session_key=SESSION_KEY) == 1
    assert interrupted == harness.built


def test_ending_the_root_never_interrupts_the_detached_job(harness, artifact):  # noqa: F811
    agent = _root(_hand_off_script())

    _run_turn(agent, "Make me a reusable explainer.")
    agent.close() if hasattr(agent, "close") else None

    assert ad.active_count() == 1
    assert all(
        not child.interrupted and not getattr(child, "_interrupt_requested", False)
        for child in harness.built
    )
