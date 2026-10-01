"""Tests for opt-in cleanup of temporary progress bubbles.

When ``display.platforms.<plat>.cleanup_progress: true`` is set for a
platform whose adapter supports message deletion (e.g. Telegram), the
tool-progress bubble, "⏳ Working — N min" heartbeats, and status-callback
messages sent during a run are deleted after the final response is
delivered.

Failed runs skip cleanup so the bubbles remain as breadcrumbs.
Adapters without ``delete_message`` silently no-op.
"""

import asyncio
import importlib
import inspect as _inspect
import sys
import time
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig


async def _fire_post_delivery_cb(cb):
    """Invoke a popped post-delivery callback, awaiting if it's async.

    Chained registrations return an async wrapper; single registrations
    return the raw sync callable. Either way, await any awaitable result.
    """
    result = cb()
    if _inspect.isawaitable(result):
        await result
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource


# ---------------------------------------------------------------------------
# Test fakes — mirror those in test_run_progress_topics.py but add a
# delete_message implementation that records ids instead of hitting a bot.
# ---------------------------------------------------------------------------


class CleanupCaptureAdapter(BasePlatformAdapter):
    """Adapter that records every delete_message call for inspection."""

    _next_mid = 100

    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(PlatformConfig(enabled=True, token="***"), platform)
        self.sent = []
        self.edits = []
        self.deleted = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    def _mint_id(self) -> str:
        CleanupCaptureAdapter._next_mid += 1
        return str(CleanupCaptureAdapter._next_mid)

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        mid = self._mint_id()
        self.sent.append(
            {"chat_id": chat_id, "content": content, "message_id": mid, "metadata": metadata}
        )
        return SendResult(success=True, message_id=mid)

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "content": content})
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id, message_id) -> bool:
        self.deleted.append({"chat_id": chat_id, "message_id": str(message_id)})
        return True

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class NoDeleteAdapter(CleanupCaptureAdapter):
    """Adapter that inherits the base no-op delete_message (used to prove
    the cleanup path skips adapters without deletion support)."""

    async def delete_message(self, chat_id, message_id) -> bool:  # type: ignore[override]
        # Pretend to be an adapter whose platform doesn't support deletion:
        # match the base class behavior exactly. gateway/run.py checks
        # ``type(adapter).delete_message is BasePlatformAdapter.delete_message``
        # to detect this, so we re-assign at class body level below.
        raise AssertionError("should not be called — cleanup must skip this adapter")


# Re-bind so the class's delete_message identity equals the base's.
NoDeleteAdapter.delete_message = BasePlatformAdapter.delete_message


class ProgressAgent:
    """Emits two tool-progress events and returns a normal final response."""

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        if cb is not None:
            cb("tool.started", "terminal", "pwd", {})
            time.sleep(0.2)
            cb("tool.started", "terminal", "ls", {})
            time.sleep(0.2)
        return {"final_response": "done", "messages": [], "api_calls": 1}


class FailingAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        if cb is not None:
            cb("tool.started", "terminal", "pwd", {})
            time.sleep(0.2)
        # Empty final_response + failed=True is the shape the gateway
        # actually returns on provider errors (see gateway/run.py where
        # failed keys are only propagated when final_response is empty).
        return {
            "final_response": "",
            "messages": [],
            "api_calls": 1,
            "failed": True,
            "error": "simulated provider failure",
        }


class InterimAgent:
    """Emits visible commentary before returning the final response."""

    def __init__(self, **kwargs):
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.interim_assistant_callback is not None:
            self.interim_assistant_callback("Checking the live configuration.")
            time.sleep(0.2)
        return {"final_response": "done", "messages": [], "api_calls": 1}


class CodexCommentaryToolAgent(InterimAgent):
    """Sanitized second smoke-test shape: commentary + opaque reasoning + tool.

    Codex sends phase=commentary through the interim callback, not through
    reasoning.available; the assistant's top-level content is empty.
    """
    mirror_reasoning = False
    trailing_commentary = False
    provider_summary = False
    summary_first = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tool_progress_callback = kwargs.get("tool_progress_callback")

    def run_conversation(self, message, conversation_history=None, task_id=None):
        assert self.tool_progress_callback is not None
        if self.summary_first:
            self.reasoning_callback("Inspecting the request.\n")
        self.interim_assistant_callback("Checking one item.", already_streamed=False)
        if self.provider_summary and not self.summary_first:
            self.reasoning_callback("Inspecting the request.\n")
        if self.mirror_reasoning:
            self.tool_progress_callback("reasoning.available", "_thinking", "Checking one item.", None)
        self.tool_progress_callback("tool.started", "terminal", "pwd", {})
        if self.trailing_commentary:
            self.interim_assistant_callback("Closing note.", already_streamed=False)
        time.sleep(0.2)
        return {"final_response": "done", "messages": [], "api_calls": 2}


class MirroredCommentaryToolAgent(CodexCommentaryToolAgent):
    mirror_reasoning = True
    trailing_commentary = True


class ProviderSummaryCommentaryToolAgent(CodexCommentaryToolAgent):
    provider_summary = True


class SummaryFirstCommentaryToolAgent(ProviderSummaryCommentaryToolAgent):
    summary_first = True


class SummaryThenAvailableAgent(CodexCommentaryToolAgent):
    """Live summary, then reasoning.available, then one tool."""

    order = "summary-first"

    def run_conversation(self, message, conversation_history=None, task_id=None):
        assert self.reasoning_callback is not None
        if self.order == "summary-first":
            self.reasoning_callback("Inspecting the request.\n")
            self.interim_assistant_callback("Checking one item.", already_streamed=False)
            self.tool_progress_callback(
                "reasoning.available", "_thinking", "Checking one item.", None,
            )
        else:
            self.tool_progress_callback(
                "reasoning.available", "_thinking", "Checking one item.", None,
            )
            self.reasoning_callback("Inspecting the request.\n")
            self.interim_assistant_callback("Checking one item.", already_streamed=False)
        self.tool_progress_callback("tool.started", "terminal", "pwd", {})
        time.sleep(0.2)
        return {"final_response": "done", "messages": [], "api_calls": 2}


class AvailableThenSummaryAgent(SummaryThenAvailableAgent):
    order = "available-first"


class DisplayOffAvailableAgent(CodexCommentaryToolAgent):
    """Display off: no provider callback, so only the pending marker counts."""

    def run_conversation(self, message, conversation_history=None, task_id=None):
        assert self.reasoning_callback is None
        self.tool_progress_callback(
            "reasoning.available", "_thinking", "Checking one item.", None,
        )
        self.interim_assistant_callback("Checking one item.", already_streamed=False)
        self.tool_progress_callback("tool.started", "terminal", "pwd", {})
        time.sleep(0.2)
        return {"final_response": "done", "messages": [], "api_calls": 2}


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner
    runner = object.__new__(GatewayRunner)
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


def _install_fakes(
    monkeypatch,
    agent_cls,
    *,
    cleanup_on: bool,
    interim_on: bool = False,
    cleanup_platform: Platform = Platform.TELEGRAM,
):
    """Wire up the module stubs every _run_agent test needs."""
    monkeypatch.setenv("CLOVER_TOOL_PROGRESS_MODE", "all")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    import tools.terminal_tool  # noqa: F401 — register tool emoji

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    # Wire the per-platform cleanup_progress flag via the config loader the
    # gateway actually reads (``_load_gateway_config`` returns user config).
    platform_display = {}
    if cleanup_on:
        platform_display["cleanup_progress"] = True
    if interim_on:
        platform_display["interim_assistant_messages"] = True
    cfg = {
        "display": {"platforms": {cleanup_platform.value: platform_display}}
    } if platform_display else {}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: cfg)
    return gateway_run


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_cls", [
    CodexCommentaryToolAgent, MirroredCommentaryToolAgent,
    ProviderSummaryCommentaryToolAgent, SummaryFirstCommentaryToolAgent,
])
async def test_codex_commentary_before_tool_is_counted_in_collapsed_card(monkeypatch, tmp_path, agent_cls):
    adapter = FinalizingCleanupAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(
        monkeypatch, agent_cls, cleanup_on=True, interim_on=True,
    )
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {
        "display": {
            "tool_progress": "all", "thinking_progress": True,
            "live_reasoning": issubclass(agent_cls, ProviderSummaryCommentaryToolAgent),
            "platforms": {"telegram": {
                "cleanup_progress": True, "interim_assistant_messages": True,
            }},
        },
    })
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
    session_key = "agent:main:telegram:group:-1001"

    result = await runner._run_agent(
        message="hello", context_prompt="", history=[], source=source,
        session_id="sess-codex-commentary-tool", session_key=session_key,
    )
    assert result["final_response"] == "done"
    assert any("Checking one item." in item["content"] for item in adapter.sent)
    cb = adapter.pop_post_delivery_callback(session_key)
    assert callable(cb)
    await _fire_post_delivery_cb(cb)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if adapter.edits:
            break
    assert adapter.edits
    card = adapter.edits[-1]["content"]
    assert "1 thought" in card and "1 tool call" in card
    assert "2 thought" not in card


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_cls", [
    SummaryThenAvailableAgent, AvailableThenSummaryAgent,
])
async def test_live_summary_and_reasoning_available_count_once(
    monkeypatch, tmp_path, agent_cls,
):
    adapter = FinalizingCleanupAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(
        monkeypatch, agent_cls, cleanup_on=True, interim_on=True,
    )
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {
        "display": {
            "tool_progress": "all", "thinking_progress": True,
            "live_reasoning": True,
            "platforms": {"telegram": {
                "cleanup_progress": True, "interim_assistant_messages": True,
            }},
        },
    })
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
    session_key = "agent:main:telegram:group:-1001"
    result = await runner._run_agent(
        message="hello", context_prompt="", history=[], source=source,
        session_id="sess-summary-available-order", session_key=session_key,
    )
    assert result["final_response"] == "done"
    cb = adapter.pop_post_delivery_callback(session_key)
    await _fire_post_delivery_cb(cb)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if adapter.edits:
            break
    card = adapter.edits[-1]["content"]
    assert "1 thought" in card and "1 tool call" in card
    assert "2 thought" not in card


@pytest.mark.asyncio
async def test_display_off_does_not_install_callback_count(monkeypatch, tmp_path):
    adapter = FinalizingCleanupAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(
        monkeypatch, DisplayOffAvailableAgent, cleanup_on=True, interim_on=True,
    )
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {
        "display": {
            "tool_progress": "all", "thinking_progress": True,
            "live_reasoning": False,
            "platforms": {"telegram": {
                "cleanup_progress": True, "interim_assistant_messages": True,
            }},
        },
    })
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
    session_key = "agent:main:telegram:group:-1001"
    result = await runner._run_agent(
        message="hello", context_prompt="", history=[], source=source,
        session_id="sess-display-off-available", session_key=session_key,
    )
    assert result["final_response"] == "done"
    cb = adapter.pop_post_delivery_callback(session_key)
    await _fire_post_delivery_cb(cb)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if adapter.edits:
            break
    card = adapter.edits[-1]["content"]
    assert "1 thought" in card and "1 tool call" in card
    assert "2 thought" not in card


@pytest.mark.asyncio
async def test_cleanup_removes_interim_commentary_after_final_delivery(monkeypatch, tmp_path):
    adapter = CleanupCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(
        monkeypatch, InterimAgent, cleanup_on=True, interim_on=True,
    )
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
    session_key = "agent:main:telegram:group:-1001"

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-interim-cleanup",
        session_key=session_key,
    )
    assert result["final_response"] == "done"
    commentary = next(
        item for item in adapter.sent
        if item["content"] == "💭 *Checking the live configuration.*"
    )

    cb = adapter.pop_post_delivery_callback(session_key)
    assert callable(cb)
    await _fire_post_delivery_cb(cb)
    for _ in range(20):
        await asyncio.sleep(0.01)
        if adapter.deleted:
            break

    assert commentary["message_id"] in {
        item["message_id"] for item in adapter.deleted
    }


@pytest.mark.asyncio
async def test_messaging_agent_forwards_checkpoint_config(monkeypatch, tmp_path):
    """Writable gateway agents must receive the configured checkpoint limits."""
    captured = {}

    class CheckpointCaptureAgent(ProgressAgent):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)

    adapter = CleanupCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(
        monkeypatch, CheckpointCaptureAgent, cleanup_on=False,
    )
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "checkpoints": {
                "enabled": True,
                "max_snapshots": 9,
                "max_total_size_mb": 444,
                "max_file_size_mb": 6,
            }
        },
    )

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-checkpoints",
        session_key="agent:main:telegram:group:-1001",
    )

    assert result["final_response"] == "done"
    assert captured["checkpoints_enabled"] is True
    assert captured["checkpoint_max_snapshots"] == 9
    assert captured["checkpoint_max_total_size_mb"] == 444
    assert captured["checkpoint_max_file_size_mb"] == 6


@pytest.mark.asyncio
async def test_cleanup_chains_with_existing_callback(monkeypatch, tmp_path):
    """When a bg-review-style callback is already registered, the cleanup
    callback chains with it — both fire, neither clobbers the other."""
    adapter = CleanupCaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, ProgressAgent, cleanup_on=True)
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
    session_key = "agent:main:telegram:group:-1001"

    pre_existing_fired = []

    def _preexisting_callback() -> None:
        pre_existing_fired.append(True)

    # Pre-register a callback with the same generation the run will use
    # (run_generation=None in this test path — matches the default slot).
    adapter.register_post_delivery_callback(session_key, _preexisting_callback)

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key=session_key,
    )

    assert result["final_response"] == "done"
    cb = adapter.pop_post_delivery_callback(session_key)
    assert callable(cb)
    await _fire_post_delivery_cb(cb)
    for _ in range(20):
        await asyncio.sleep(0.01)
        if adapter.deleted:
            break

    # Both effects land: the pre-existing callback fires AND the cleanup
    # deletes at least one progress bubble.
    assert pre_existing_fired == [True]
    assert len(adapter.deleted) >= 1


class FinalizingCleanupAdapter(CleanupCaptureAdapter):
    """Accepts ``finalize=`` like the real Telegram adapter does."""

    async def edit_message(self, chat_id, message_id, content, *, finalize=False,
                           metadata=None) -> SendResult:
        return await super().edit_message(chat_id, message_id, content)


@pytest.mark.asyncio
async def test_slack_cleanup_progress_on_by_default_without_config(monkeypatch, tmp_path):
    """Slack's Bolt adapter really implements both edit_message
    (chat.update) and delete_message (chat.delete), so cleanup_progress is
    on by default there too — no explicit config.yaml override needed."""
    adapter = FinalizingCleanupAdapter(platform=Platform.SLACK)
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, ProgressAgent, cleanup_on=False)
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)

    source = SessionSource(platform=Platform.SLACK, chat_id="C1")
    session_key = "agent:main:slack:channel:C1"

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-slack-default-cleanup",
        session_key=session_key,
    )
    assert result["final_response"] == "done"
    cb = adapter.pop_post_delivery_callback(session_key)
    assert callable(cb)
    await _fire_post_delivery_cb(cb)
    for _ in range(20):
        await asyncio.sleep(0.01)
        if any("tool call" in e["content"] for e in adapter.edits):
            break

    assert any("tool call" in e["content"] for e in adapter.edits), (
        "Slack's progress bubbles must collapse into a summary card by default"
    )


class QueuesFollowUpAgent(ProgressAgent):
    """First run emits tool progress, then a follow-up lands in the queue
    (the shape of an async delegation result arriving mid-turn). The
    follow-up run does no tool work."""

    adapter = None
    session_key = None
    runs = 0

    def run_conversation(self, message, conversation_history=None, task_id=None):
        type(self).runs += 1
        if type(self).runs == 1:
            result = super().run_conversation(message, conversation_history, task_id)
            from gateway.platforms.base import MessageEvent

            src = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
            type(self).adapter._pending_messages[type(self).session_key] = MessageEvent(
                text="[ASYNC DELEGATION BATCH COMPLETE — deleg_x]", source=src,
            )
            return result
        return {"final_response": "follow-up handled", "messages": [], "api_calls": 1}


@pytest.mark.asyncio
async def test_queued_follow_up_still_collapses_first_turn_into_card(monkeypatch, tmp_path):
    """Regression: the queued follow-up branch returned before the cleanup
    callback was registered, so the first turn's tool bubbles were never
    collapsed into the summary card."""
    adapter = FinalizingCleanupAdapter()
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, QueuesFollowUpAgent, cleanup_on=True)
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
    session_key = "agent:main:telegram:group:-1001"
    QueuesFollowUpAgent.adapter = adapter
    QueuesFollowUpAgent.session_key = session_key
    QueuesFollowUpAgent.runs = 0

    await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-queued-card",
        session_key=session_key,
    )
    assert QueuesFollowUpAgent.runs == 2, "the queued follow-up must have run"
    progress_ids = {
        item["message_id"] for item in adapter.sent if item["content"] not in ("done", "follow-up handled")
    }
    assert progress_ids, "first turn must have posted tool progress"
    for _ in range(50):
        await asyncio.sleep(0.01)
        if any("tool call" in e["content"] for e in adapter.edits):
            break
    touched = {e["message_id"] for e in adapter.edits} | {
        d["message_id"] for d in adapter.deleted
    }
    assert progress_ids <= touched, (
        "first turn's progress bubbles were left in the chat: "
        f"{progress_ids - touched}"
    )
    cards = [e["content"] for e in adapter.edits if "tool call" in e["content"]]
    assert cards, "first turn's progress must collapse into the summary card"


# ---------------------------------------------------------------------------
# "⏳ Working — N min" heartbeat must never outlive a successful turn.
# ---------------------------------------------------------------------------


class SlowAgent:
    """Runs long enough for several heartbeats, then finishes normally."""

    run_seconds = 0.5

    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        time.sleep(type(self).run_seconds)
        return {"final_response": "done", "messages": [], "api_calls": 1}


class SlowToolAgent(SlowAgent):
    """Like SlowAgent but does real tool work, so the turn collapses into a
    summary card (the live shape: tool bubble + Working heartbeat)."""

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cb = self.tool_progress_callback
        if cb is not None:
            cb("tool.started", "terminal", "pwd", {})
        return super().run_conversation(message, conversation_history, task_id)


class SlowFailingAgent(SlowAgent):
    def run_conversation(self, message, conversation_history=None, task_id=None):
        time.sleep(type(self).run_seconds)
        return {"final_response": "", "messages": [], "api_calls": 1,
                "failed": True, "error": "simulated provider failure"}


class FlakyNetworkAdapter(FinalizingCleanupAdapter):
    """Real adapters (Telegram) signal API failure via the return value, not
    an exception: ``edit_message`` -> ``SendResult(success=False)`` and
    ``delete_message`` -> ``False``. A flaky network fails the first N calls."""

    def __init__(self, *, edit_failures=0, delete_failures=0):
        super().__init__()
        self._edit_failures = edit_failures
        self._delete_failures = delete_failures

    async def edit_message(self, chat_id, message_id, content, *, finalize=False,
                           metadata=None) -> SendResult:
        if finalize and self._edit_failures > 0:
            self._edit_failures -= 1
            return SendResult(success=False, error="ConnectError", retryable=True)
        return await super().edit_message(
            chat_id, message_id, content, finalize=finalize, metadata=metadata
        )

    async def delete_message(self, chat_id, message_id) -> bool:
        if self._delete_failures > 0:
            self._delete_failures -= 1
            return False
        return await super().delete_message(chat_id, message_id)


def _heartbeat_ids(adapter):
    return [i["message_id"] for i in adapter.sent if i["content"].startswith("⏳ Working")]


def _gone(adapter, mid):
    """True when the bubble was deleted, or edited into something that is no
    longer the Working text (the collapsed card)."""
    if any(d["message_id"] == mid for d in adapter.deleted):
        return True
    edits = [e for e in adapter.edits if e["message_id"] == mid]
    return bool(edits) and not edits[-1]["content"].startswith("⏳ Working")


async def _run_turn(monkeypatch, tmp_path, adapter, agent_cls, *, notify="0.1"):
    monkeypatch.setenv("CLOVER_AGENT_NOTIFY_INTERVAL", notify)
    runner = _make_runner(adapter)
    gateway_run = _install_fakes(monkeypatch, agent_cls, cleanup_on=True)
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="-1001")
    session_key = "agent:main:telegram:group:-1001"
    result = await runner._run_agent(
        message="hello", context_prompt="", history=[], source=source,
        session_id="sess-heartbeat", session_key=session_key,
    )
    return result, session_key


async def _drain_post_delivery(adapter, session_key):
    cb = adapter.pop_post_delivery_callback(session_key)
    if callable(cb):
        await _fire_post_delivery_cb(cb)
    for _ in range(100):
        await asyncio.sleep(0.01)
    return cb


@pytest.mark.asyncio
async def test_heartbeat_then_final_reply_leaves_no_working_bubble(monkeypatch, tmp_path):
    adapter = CleanupCaptureAdapter()
    SlowAgent.run_seconds = 0.35
    result, key = await _run_turn(monkeypatch, tmp_path, adapter, SlowAgent)
    assert result["final_response"] == "done"
    await _drain_post_delivery(adapter, key)
    hb = _heartbeat_ids(adapter)
    assert hb, "heartbeat must have fired"
    for mid in hb:
        assert _gone(adapter, mid), f"Working bubble {mid} survived the turn"


@pytest.mark.asyncio
async def test_repeatedly_edited_heartbeat_is_gone_after_final_reply(monkeypatch, tmp_path):
    adapter = CleanupCaptureAdapter()
    SlowAgent.run_seconds = 0.6
    result, key = await _run_turn(monkeypatch, tmp_path, adapter, SlowAgent, notify="0.1")
    await _drain_post_delivery(adapter, key)
    hb = _heartbeat_ids(adapter)
    assert len(hb) == 1, "later heartbeats edit the first bubble in place"
    assert len([e for e in adapter.edits if e["message_id"] == hb[0]
                and e["content"].startswith("⏳ Working")]) >= 2
    assert _gone(adapter, hb[0])


@pytest.mark.asyncio
async def test_failed_card_edit_does_not_leave_working_bubble(monkeypatch, tmp_path):
    """Root cause of the live '⏳ Working — 3 min' leftover: the collapsed-card
    edit returned SendResult(success=False) (no exception), so the bubble was
    still treated as the kept card and never deleted."""
    from gateway import progress_cleanup
    monkeypatch.setattr(progress_cleanup, "_RETRY_DELAYS", (0.01, 0.01))
    adapter = FlakyNetworkAdapter(edit_failures=1)
    SlowToolAgent.run_seconds = 0.35
    result, key = await _run_turn(monkeypatch, tmp_path, adapter, SlowToolAgent)
    await _drain_post_delivery(adapter, key)
    hb = _heartbeat_ids(adapter)
    assert hb
    for mid in hb:
        assert _gone(adapter, mid), f"Working bubble {mid} survived a failed card edit"


@pytest.mark.asyncio
async def test_failed_delete_is_retried_so_working_bubble_does_not_survive(monkeypatch, tmp_path):
    from gateway import progress_cleanup
    monkeypatch.setattr(progress_cleanup, "_RETRY_DELAYS", (0.01, 0.01))
    # Card edits keep failing -> the Working bubble (first tracked id) falls
    # back to deletion, whose first attempt also fails.
    adapter = FlakyNetworkAdapter(edit_failures=99, delete_failures=1)
    SlowToolAgent.run_seconds = 0.35
    result, key = await _run_turn(monkeypatch, tmp_path, adapter, SlowToolAgent)
    await _drain_post_delivery(adapter, key)
    hb = _heartbeat_ids(adapter)
    assert hb
    for mid in hb:
        assert any(d["message_id"] == mid for d in adapter.deleted), (
            f"Working bubble {mid} survived a failed delete"
        )


class SlowHeartbeatSendAdapter(CleanupCaptureAdapter):
    """The heartbeat send is still in flight when the turn ends; it lands on
    the platform (id minted) only after the run has been torn down."""

    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()
        self.hb_send_started = asyncio.Event()

    async def edit_message(self, chat_id, message_id, content, *, finalize=False,
                           metadata=None) -> SendResult:
        return await super().edit_message(chat_id, message_id, content)

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        if content.startswith("⏳ Working"):
            self.hb_send_started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                # Cancelled locally, but the request already reached the server.
                await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)
                raise
        return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)


@pytest.mark.asyncio
async def test_heartbeat_send_racing_final_response_is_still_cleaned_up(monkeypatch, tmp_path):
    adapter = SlowHeartbeatSendAdapter()
    SlowAgent.run_seconds = 0.4
    result, key = await _run_turn(monkeypatch, tmp_path, adapter, SlowAgent)
    assert adapter.hb_send_started.is_set(), "heartbeat send must be in flight at turn end"
    await _drain_post_delivery(adapter, key)
    adapter.release.set()
    for _ in range(100):
        await asyncio.sleep(0.01)
    hb = _heartbeat_ids(adapter)
    assert hb, "the racing heartbeat still reached the chat"
    for mid in hb:
        assert _gone(adapter, mid), f"racing Working bubble {mid} survived the turn"


@pytest.mark.asyncio
async def test_failed_run_keeps_working_bubbles(monkeypatch, tmp_path):
    adapter = CleanupCaptureAdapter()
    SlowFailingAgent.run_seconds = 0.35
    result, key = await _run_turn(monkeypatch, tmp_path, adapter, SlowFailingAgent)
    assert result.get("failed")
    await _drain_post_delivery(adapter, key)
    assert _heartbeat_ids(adapter)
    assert not adapter.deleted, "failed runs keep their bubbles as breadcrumbs"
    assert not any("tool call" in e["content"] for e in adapter.edits)
