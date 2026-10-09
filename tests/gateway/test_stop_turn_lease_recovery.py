"""Behavioral regressions for lease recovery after interrupted gateway turns.

The tests use GatewayRunner's real inbound/Stop/teardown path. Only provider
execution and the synchronous store boundary are controlled; no live transport
or gateway process is involved.
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import AsyncSessionStore, SessionEntry, SessionSource


KEY = "agent:main:telegram:dm:stop-recovery-test"
SESSION_ID = "stop-recovery-test-session"


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="stop-recovery-test",
        chat_type="dm",
        user_id="stop-recovery-test",
    )


def _event(text: str, message_id: str) -> MessageEvent:
    return MessageEvent(text=text, source=_source(), message_id=message_id)


def _runner(tmp_path):
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _cwd: None
    runner._clear_session_env = lambda _token: None
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *args, **kwargs: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    sync_store = MagicMock()
    entry = SessionEntry(
        session_key=KEY,
        session_id=SESSION_ID,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    sync_store.get_or_create_session.return_value = entry
    sync_store.load_transcript.return_value = []
    sync_store.has_platform_message_id.return_value = False
    active_markers = {}
    marker_lock = threading.Lock()

    def mark_turn_active(key):
        token = f"token-{key}-{time.monotonic()}"
        with marker_lock:
            active_markers[key] = token
        return token

    sync_store.mark_turn_active.side_effect = mark_turn_active
    runner.session_store = sync_store
    # Keep the real async facade attached to the same fixture store.
    runner._async_session_store = AsyncSessionStore(sync_store)
    runner._clear_entered = threading.Event()
    runner._clear_release = threading.Event()
    runner._active_markers = active_markers
    runner._block_first_clear = False
    runner._store_clear_calls = []

    def clear_turn_active(key, token):
        runner._store_clear_calls.append((key, token))
        if runner._block_first_clear:
            runner._block_first_clear = False
            runner._clear_entered.set()
            runner._clear_release.wait(10)
        with marker_lock:
            if active_markers.get(key) != token:
                return False
            del active_markers[key]
            return True

    sync_store.clear_turn_active.side_effect = clear_turn_active
    gateway_run._clover_home = tmp_path
    gateway_run._resolve_runtime_agent_kwargs = lambda: {"api_key": "test-only"}
    return runner


async def _drive_first_turn_to_stop(runner):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def synthetic_provider(**kwargs):
        calls.append(kwargs["message"])
        agent = MagicMock()
        agent._gateway_turn_process_task_id = ""
        agent._gateway_turn_process_baseline = None
        agent.get_activity_summary.return_value = {"seconds_since_activity": 0}
        runner._session_state(KEY).turn.agent = agent
        if len(calls) == 1:
            entered.set()
            await release.wait()
            return {"final_response": "", "messages": [], "api_calls": 1, "interrupted": True}
        return {"final_response": "follow-up handled", "messages": [], "api_calls": 1}

    runner._run_agent = synthetic_provider
    first_task = asyncio.create_task(runner._handle_message(_event("first turn", "first")))
    await asyncio.wait_for(entered.wait(), timeout=5)
    holder = runner._turn_leases._leases[SESSION_ID].holder
    assert holder is not None, "real inbound path did not acquire the session lease"
    # This is the real Stop command path used by the busy-session handler.
    stop_result = await runner._busy_stop_command(_event("/stop", "stop"), KEY, _source())
    release.set()
    return first_task, calls, stop_result


@pytest.mark.parametrize("fault", ["hook-hang", "clear-hang", "cancel-during-clear"])
def test_stop_releases_lease_before_nonessential_cleanup_can_pin_followup(tmp_path, fault):
    async def scenario():
        runner = _runner(tmp_path)
        if fault in {"clear-hang", "cancel-during-clear"}:
            runner._block_first_clear = True
        if fault == "hook-hang":
            original = runner._run_post_turn_hooks
            first_hook = True

            async def delayed_hook(*args, **kwargs):
                nonlocal first_hook
                if first_hook:
                    first_hook = False
                    await asyncio.Event().wait()
                await original(*args, **kwargs)

            runner._run_post_turn_hooks = delayed_hook

        first, provider_calls, _ = await _drive_first_turn_to_stop(runner)
        if fault in {"clear-hang", "cancel-during-clear"}:
            reached = await asyncio.get_running_loop().run_in_executor(
                None, runner._clear_entered.wait, 5
            )
            assert reached, "injected durable-marker clear fault was not reached"
        if fault == "cancel-during-clear":
            first.cancel()

        # An independent follow-up observer must complete without waiting for
        # the old hook or marker cleanup. Its normal transcript path stays real.
        follow_up = asyncio.create_task(
            runner._handle_message(_event("next user message", "next"))
        )
        result = await asyncio.wait_for(asyncio.shield(follow_up), timeout=2)
        assert result == "follow-up handled"
        assert len(provider_calls) == 2
        assert runner._release_turn_lease(KEY, 1) is False  # duplicate release is harmless
        assert runner._turn_leases._leases[SESSION_ID].holder is None

        runner._clear_release.set()
        for task in (first, follow_up):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, follow_up, return_exceptions=True)
        if fault == "cancel-during-clear":
            assert first.cancelled(), "Stop cancellation was swallowed during marker cleanup"

    asyncio.run(scenario())


def test_ordinary_completion_releases_its_lease(tmp_path):
    async def scenario():
        runner = _runner(tmp_path)

        async def provider(**kwargs):
            agent = MagicMock()
            agent._gateway_turn_process_task_id = ""
            agent._gateway_turn_process_baseline = None
            agent.get_activity_summary.return_value = {"seconds_since_activity": 0}
            runner._session_state(KEY).turn.agent = agent
            return {"final_response": "completed", "messages": [], "api_calls": 1}

        runner._run_agent = provider
        result = await asyncio.wait_for(
            runner._handle_message(_event("ordinary", "ordinary")), timeout=5
        )
        assert result == "completed"
        assert runner._turn_leases._leases[SESSION_ID].holder is None
        assert runner._session_state(KEY).turn.agent is None

    asyncio.run(scenario())


def test_repeated_stop_is_idempotent_and_does_not_retain_the_lease(tmp_path):
    async def scenario():
        runner = _runner(tmp_path)
        first, _provider_calls, _ = await _drive_first_turn_to_stop(runner)
        await runner._busy_stop_command(_event("/stop", "stop-again"), KEY, _source())
        await asyncio.wait_for(first, timeout=5)
        assert runner._turn_leases._leases[SESSION_ID].holder is None
        assert runner._release_turn_lease(KEY, 1) is False

    asyncio.run(scenario())


def test_late_old_cleanup_preserves_running_successor_and_serializes_third_turn(tmp_path):
    async def scenario():
        runner = _runner(tmp_path)
        runner._block_first_clear = True
        old_entered = asyncio.Event()
        old_release = asyncio.Event()
        successor_entered = asyncio.Event()
        successor_release = asyncio.Event()
        third_entered = asyncio.Event()
        provider_calls = []

        async def provider(**kwargs):
            prompt = kwargs["message"]
            provider_calls.append(prompt)
            agent = MagicMock(name=f"agent-{len(provider_calls)}")
            agent._gateway_turn_process_task_id = ""
            agent._gateway_turn_process_baseline = None
            agent.get_activity_summary.return_value = {"seconds_since_activity": 0}
            runner._session_state(KEY).turn.agent = agent
            if prompt == "old first turn":
                old_entered.set()
                await old_release.wait()
                return {"final_response": "old", "messages": [], "api_calls": 1, "interrupted": True}
            if prompt == "successor turn":
                successor_entered.set()
                await successor_release.wait()
            if prompt == "third turn":
                third_entered.set()
            return {"final_response": prompt, "messages": [], "api_calls": 1}

        runner._run_agent = provider
        old = asyncio.create_task(
            runner._handle_message(_event("old first turn", "old"))
        )
        await asyncio.wait_for(old_entered.wait(), timeout=5)
        await runner._busy_stop_command(_event("/stop", "stop"), KEY, _source())
        old_release.set()
        got_clear = await asyncio.get_running_loop().run_in_executor(
            None, runner._clear_entered.wait, 5
        )
        assert got_clear, "old turn did not reach the blocked durable-marker clear"

        successor = asyncio.create_task(
            runner._handle_message(_event("successor turn", "successor"))
        )
        await asyncio.wait_for(successor_entered.wait(), timeout=5)
        successor_state = runner._session_state(KEY)
        successor_agent = successor_state.turn.agent
        successor_holder = runner._turn_leases._leases[SESSION_ID].holder
        assert successor_holder is not None
        assert successor_state.turn.lease_generation == successor_state.persistent.run_generation
        successor_marker = runner._active_markers[KEY]

        # Resume gen1's late cleanup while gen3 is active. It must not clear the
        # successor's slot, lease, or active-turn marker.
        runner._clear_release.set()
        await asyncio.wait_for(old, timeout=5)
        assert successor_state.turn.agent is successor_agent
        assert runner._turn_leases._leases[SESSION_ID].holder is successor_holder
        assert runner._active_markers[KEY] == successor_marker

        third = asyncio.create_task(
            runner._handle_message(_event("third turn", "third"))
        )
        await asyncio.sleep(0.1)
        assert not third_entered.is_set(), "third provider started concurrently with the successor"
        assert len(provider_calls) == 2

        successor_release.set()
        await asyncio.wait_for(successor, timeout=5)
        await asyncio.wait_for(third, timeout=5)
        assert runner._turn_leases._leases[SESSION_ID].holder is None

    asyncio.run(scenario())
