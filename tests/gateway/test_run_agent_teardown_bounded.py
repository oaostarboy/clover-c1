"""A finished agent turn must release the gateway turn even if platform I/O hangs.

Production incident (Oct 8-9 2026, Telegram DM): the agent loop ended with
``Turn ended: reason=delegation_handoff`` but the gateway turn for that
message stayed "active" for ~95 minutes (``response ready: time=5679.4s``),
so the completion of the background worker it had just dispatched queued
behind a phantom turn and the user saw only "typing".  The first ``/stop``
found no agent to stop (state already released) and the turn then completed
instantly: the hang was in ``_run_agent`` teardown, awaiting a cancelled
helper task (progress sender / stream consumer) whose CancelledError handler
performs *unbounded* platform I/O.  A hung platform request therefore pinned
the whole turn after the agent was done.

Verified from the incident logs (not the full causal chain): the first
``/stop`` found no running agent and then the old turn "completed" 316 ms
later with ``response ready ... time=5679.4s`` -- i.e. the agent was already
done and the adapter task's cancellation was swallowed in ``_run_agent``
teardown.  The exact hung await is not provable from logs alone; these tests
fault-inject the platform-I/O seams that teardown awaits (stream consumer's
cancel-time final edit) and assert the
documented cleanup deadlines are real.

The tests drive the real ``GatewayRunner._run_agent`` with a real
``BasePlatformAdapter`` subclass whose edit calls never return.
"""

import asyncio
import importlib
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource


class HangingTeardownAdapter(BasePlatformAdapter):
    """Platform whose first send works, then every edit/send hangs forever."""

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent = []
        self.hang_edits = asyncio.Event()  # never set => edits hang forever
        self.edit_calls = 0
        self.hang_sends_after_first = False
        # Thread-safe barriers so the (threaded) fake agent can wait for the
        # platform I/O it is meant to race, instead of sleeping.
        self.first_send_done = threading.Event()
        self.edit_entered = threading.Event()

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        if self.sent and self.hang_sends_after_first:
            await self.hang_edits.wait()
        self.sent.append({"chat_id": chat_id, "content": content})
        self.first_send_done.set()
        return SendResult(success=True, message_id="progress-1")

    async def edit_message(self, chat_id, message_id, content, **kwargs) -> SendResult:
        self.edit_calls += 1
        self.edit_entered.set()
        await self.hang_edits.wait()  # simulates a Telegram request that never returns
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class StreamingAgent:
    """Streams a preview delta, then finishes after the preview is on screen."""

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.stream_delta_callback = None
        self.tools = []
        self._interrupt_requested = False

    @property
    def is_interrupted(self) -> bool:
        return self._interrupt_requested

    adapter = None  # set by the test

    def run_conversation(self, message, conversation_history=None, task_id=None):
        self.stream_delta_callback("working on the handoff, one moment")
        assert self.adapter.first_send_done.wait(10), "preview never posted"
        self.stream_delta_callback(" ... still working")
        # Return only once the preview edit is stuck inside the platform call.
        assert self.adapter.edit_entered.wait(10), "stream edit never started"
        return {"final_response": "handoff text", "messages": [], "api_calls": 1}


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    return runner


async def _run_turn(monkeypatch, tmp_path, adapter, session_id, agent_cls=None, streaming=False):
    monkeypatch.setenv("CLOVER_TOOL_PROGRESS_MODE", "all")
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    fake_run_agent = types.ModuleType("run_agent")
    agent_cls = agent_cls or StreamingAgent
    monkeypatch.setattr(agent_cls, "adapter", adapter, raising=False)
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    runner = _make_runner(adapter)
    if streaming:
        from gateway.config import StreamingConfig

        runner.config.streaming = StreamingConfig(
            enabled=True, transport="edit", edit_interval=0.05, buffer_threshold=1
        )
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="8172525590",
        chat_type="dm",
    )
    key = "agent:main:telegram:dm:8172525590"
    result = await runner._run_agent(
        message="hi",
        context_prompt="",
        history=[],
        source=source,
        session_id=session_id,
        session_key=key,
    )
    return runner, key, result


async def _run_bounded(coro_factory, adapter, *, window):
    """Run a turn; report whether it returned inside ``window`` seconds.

    Uses ``asyncio.wait`` (not ``wait_for``) so a teardown that swallows
    cancellation cannot make this harness itself hang or fake a result.
    The hung platform call is always released before returning.
    """
    task = asyncio.ensure_future(coro_factory())
    started = time.monotonic()
    done, _ = await asyncio.wait({task}, timeout=window)
    elapsed = time.monotonic() - started
    returned = bool(done)
    adapter.hang_edits.set()  # release the fault so nothing leaks
    out = await asyncio.wait_for(task, timeout=10.0)
    return returned, elapsed, out


@pytest.fixture
def fast_teardown(monkeypatch):
    """Shrink the production teardown deadlines so tests stay fast."""
    gateway_run = importlib.import_module("gateway.run")
    from gateway import stream_consumer

    monkeypatch.setattr(gateway_run, "_TEARDOWN_STREAM_DRAIN_SECONDS", 0.5, raising=False)
    monkeypatch.setattr(gateway_run, "_TEARDOWN_TASK_GRACE_SECONDS", 0.5, raising=False)
    monkeypatch.setattr(
        stream_consumer.GatewayStreamConsumer,
        "_CANCEL_FINAL_EDIT_TIMEOUT",
        0.5,
        raising=False,
    )


@pytest.mark.asyncio
async def test_hung_stream_consumer_edit_does_not_pin_finished_turn(monkeypatch, tmp_path, fast_teardown):
    """Stream consumer's cancel-time final edit hangs => turn must still return."""
    adapter = HangingTeardownAdapter()
    returned, elapsed, out = await _run_bounded(
        lambda: _run_turn(
            monkeypatch, tmp_path, adapter, "sess-hung-stream",
            agent_cls=StreamingAgent, streaming=True,
        ),
        adapter,
        window=8.0,
    )
    runner, key, result = out
    assert adapter.edit_calls >= 1, "harness never reached a stream edit"
    assert returned, (
        "finished agent turn was pinned by a hung platform edit during "
        "_run_agent teardown (stream consumer cancelled without bound)"
    )
    assert result["final_response"] == "handoff text"
    assert not runner._running_agents.get(key)
    # The hung edit never confirmed: the normal final send must still be able
    # to deliver the answer exactly once (no false "already delivered").
    assert not result.get("already_sent")


@pytest.mark.asyncio
async def test_stop_during_hung_teardown_is_not_swallowed(monkeypatch, tmp_path, fast_teardown):
    """/stop cancelling a turn stuck in teardown must raise, not return success."""
    adapter = HangingTeardownAdapter()
    task = asyncio.create_task(
        _run_turn(
            monkeypatch, tmp_path, adapter, "sess-stop-in-teardown",
            agent_cls=StreamingAgent, streaming=True,
        )
    )
    try:
        for _ in range(300):
            if adapter.edit_entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert adapter.edit_entered.is_set(), "never reached the hung stream edit"
        await asyncio.sleep(0.25)  # agent returned; gateway is in teardown
        assert not task.done(), "not in blocked teardown"
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=8.0)
        assert done, "cancelled turn did not unwind within the hard bound"
        with pytest.raises(asyncio.CancelledError):
            task.result()
    finally:
        adapter.hang_edits.set()
        if not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=5.0)
