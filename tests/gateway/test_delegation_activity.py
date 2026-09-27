"""Behavioral tests for visible, titled subagent activity on gateway surfaces.

Every test here drives the REAL event path end to end:

    child agent event (tool.started / _thinking / tool.completed / ...)
      -> tools.delegate_tool._build_child_progress_callback  (relay + identity)
      -> gateway.run.TurnRunner.progress_callback            (turn routing)
      -> gateway.delegation_activity.DelegationActivityPublisher
      -> adapter.send_or_update_status / adapter.send        (rendered payload)

and asserts on the payload a Telegram-shaped adapter actually receives, not on
a formatting helper in isolation.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


SECRET = "sk-ant-api03-" + "A" * 48


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTelegramAdapter:
    """Records every delivery the way Telegram's edit-in-place transport sees it."""

    def __init__(self, *, fail: int = 0, retry_after: Optional[float] = None,
                 raise_exc: bool = False) -> None:
        self.status_calls: List[Dict[str, Any]] = []
        self.sends: List[Dict[str, Any]] = []
        self.cards: Dict[str, str] = {}
        self.deleted: List[str] = []
        self._status_message_ids: Dict[Any, str] = {}
        self._mid_key: Dict[str, Any] = {}
        self._fail = fail
        self._retry_after = retry_after
        self._raise = raise_exc

    async def send_or_update_status(self, chat_id, status_key, content, *, metadata=None):
        from gateway.platforms.base import SendResult

        self.status_calls.append(
            {"chat_id": chat_id, "key": status_key, "content": content, "metadata": metadata}
        )
        if self._raise:
            raise RuntimeError("network down")
        if self._fail > 0:
            self._fail -= 1
            return SendResult(success=False, error="flood", retry_after=self._retry_after)
        key = (chat_id, status_key)
        self.cards[key] = content
        mid = self._status_message_ids.get(key) or f"m{len(self._status_message_ids) + 1}"
        self._status_message_ids[key] = mid
        self._mid_key[mid] = key
        return SendResult(success=True, message_id=mid)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(str(message_id))
        self.cards.pop(self._mid_key.pop(str(message_id), None), None)
        return True

    def summary(self) -> str:
        assert self.sends, "expected a final summary message"
        return self.sends[-1]["content"]

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        from gateway.platforms.base import SendResult

        self.sends.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return SendResult(success=True, message_id=f"s{len(self.sends)}")

    def card(self, chat_id="chat-A", key=None) -> str:
        if key is None:
            keys = [k for (c, k) in self.cards if c == chat_id]
            assert len(keys) == 1, f"expected exactly one card, got {keys}"
            key = keys[0]
        return self.cards[(chat_id, key)]


def _source(chat_id="chat-A", thread_id=None, platform=Platform.TELEGRAM):
    return SessionSource(platform=platform, chat_id=chat_id, thread_id=thread_id)


def _make_publisher(adapter, *, chat_id="chat-A", metadata=None, clock=None,
                    is_current=None, heartbeat_seconds=60, stall_seconds=600,
                    probe=None):
    from gateway.delegation_activity import DelegationActivityPublisher

    return DelegationActivityPublisher(
        adapter=adapter,
        chat_id=chat_id,
        metadata=metadata,
        loop=asyncio.get_running_loop(),
        is_current=is_current or (lambda: True),
        heartbeat_seconds=heartbeat_seconds,
        stall_seconds=stall_seconds,
        min_interval=0.0,
        liveness_probe=probe,
        clock=clock or FakeClock(),
        auto_heartbeat=False,
    )


def _turn_runner(publisher, source=None):
    from gateway.run import TurnRunner

    class _StubGatewayRunner:
        def _adapter_for_source(self, source):
            return None

    ctx = TurnContext(
        source=source or _source(),
        _run_still_current=lambda: True,
        delegation_activity=publisher,
    )
    return TurnRunner(_StubGatewayRunner(), ctx)


def _child_cb(runner, *, index=0, count=1, subagent_id="sa-1", goal="Audit the gateway auth mixin",
              title="Audit gateway auth", model="claude-opus-5-5", provider="anthropic",
              delegation_id="deleg_aaaa0001"):
    from tools.delegate_tool import _build_child_progress_callback

    parent = SimpleNamespace(
        tool_progress_callback=runner.progress_callback, _delegate_spinner=None
    )
    ref = {"delegation_id": delegation_id, "title": title, "provider": provider}
    cb = _build_child_progress_callback(
        index, goal, parent, count,
        subagent_id=subagent_id, depth=0, model=model, session_ref=ref,
    )
    assert cb is not None
    return cb


# ---------------------------------------------------------------------------
# Concurrency, queueing, titles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_concurrent_titled_children_share_one_card():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    a = _child_cb(runner, index=0, count=2, subagent_id="sa-1", title="Audit gateway auth")
    b = _child_cb(runner, index=1, count=2, subagent_id="sa-2", title="Summarize release notes",
                  model="gpt-5.5", provider="openrouter")

    a("subagent.start", preview="Audit the gateway auth mixin")
    b("subagent.start", preview="Summarize release notes")
    a("tool.started", "read_file", "gateway/authz_mixin.py", {"path": "gateway/authz_mixin.py"})
    b("tool.started", "web_search", "clover release", {"query": "clover release"})
    await pub.drain()

    # One coalesced card per delegation group — not one message per event.
    keys = {c["key"] for c in adapter.status_calls}
    assert keys == {"delegation:deleg_aaaa0001"}
    assert adapter.sends == []
    card = adapter.card()
    assert "Audit gateway auth" in card and "Summarize release notes" in card
    assert "Opus 5.5" in card and "GPT-5.5" in card
    assert "openrouter" not in card  # provider plumbing stays out of the card
    assert "read_file authz_mixin.py" in card and "web_search" in card
    assert "gateway/authz_mixin.py" not in card  # basenames, not full paths
    # Clearly labelled as child activity, not the parent's own thoughts/tools.
    assert "subagent" in card.lower()
    # One line per worker.
    assert len([l for l in card.splitlines() if l.startswith("> ▸")]) == 2
    # No fake percentages.
    assert "%" not in card
    await pub.aclose()


@pytest.mark.asyncio
async def test_queued_child_is_shown_until_it_starts():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    a = _child_cb(runner, index=0, count=2, subagent_id="sa-1")
    b = _child_cb(runner, index=1, count=2, subagent_id="sa-2", title="Draft changelog")

    a("subagent.queued", preview="goal a")
    b("subagent.queued", preview="goal b")
    a("subagent.start", preview="goal a")
    await pub.drain()
    snap = {c["subagent_id"]: c for c in pub.tracker.snapshot("deleg_aaaa0001")}
    assert snap["sa-1"]["state"] == "starting"
    assert snap["sa-2"]["state"] == "queued"
    assert "queued" in adapter.card()

    b("subagent.start", preview="goal b")
    await pub.drain()
    snap = {c["subagent_id"]: c for c in pub.tracker.snapshot("deleg_aaaa0001")}
    assert snap["sa-2"]["state"] == "starting"
    await pub.aclose()


def test_title_is_derived_from_goal_when_not_given():
    from agent.delegation_activity import derive_task_title

    title = derive_task_title(
        "Investigate why the Telegram adapter drops edits after flood control. "
        "Then write a regression test and fix it."
    )
    assert title.startswith("Investigate why the Telegram adapter")
    assert len(title) <= 60
    assert derive_task_title("x", explicit="  Fix flood control  ") == "Fix flood control"
    assert derive_task_title("", explicit=None) == "Untitled task"


# ---------------------------------------------------------------------------
# Notes: user-facing progress notes only, never reasoning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_note_is_attributed_to_child_and_reasoning_is_never_shown():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    a = _child_cb(runner, index=0, count=2, subagent_id="sa-1", title="Audit gateway auth")
    b = _child_cb(runner, index=1, count=2, subagent_id="sa-2", title="Draft changelog")
    a("subagent.start", preview="g")
    b("subagent.start", preview="g")

    # Deliberate, user-facing interim content from child A.
    a("_thinking", "Checking token scoping in the authz mixin")
    # Private reasoning and spinner chatter from child B must never surface.
    b("reasoning.available", "_thinking", "PRIVATE-CHAIN-OF-THOUGHT about the user", None)
    b("_thinking", "(◕‿◕) pondering...", spinner=True)
    await pub.drain()

    card = adapter.card()
    assert "Checking token scoping in the authz mixin" in card
    assert "PRIVATE-CHAIN-OF-THOUGHT" not in card
    assert "pondering" not in card
    snap = {c["subagent_id"]: c for c in pub.tracker.snapshot("deleg_aaaa0001")}
    assert snap["sa-1"]["note"] == "Checking token scoping in the authz mixin"
    assert snap["sa-2"]["note"] is None
    # The note line sits under child A's title, not child B's.
    # The note is an activity line attributed to child A (title + model) —
    # never to child B.
    note_line = next(l for l in card.splitlines() if "Checking token scoping" in l)
    assert "Audit gateway auth" in note_line and "Opus 5.5" in note_line
    assert "Draft changelog" not in note_line
    await pub.aclose()


def test_progress_note_extraction_drops_inline_reasoning_blocks():
    from agent.delegation_activity import extract_progress_note

    content = "<think>secret plan to read ~/.ssh</think>\nReading the config loader next."
    assert extract_progress_note(content) == "Reading the config loader next."
    assert extract_progress_note("<reasoning>only reasoning</reasoning>") == ""
    # Unterminated reasoning block: withhold everything after the opener.
    assert extract_progress_note("Plan: <think>half open secret") == "Plan:"


# ---------------------------------------------------------------------------
# Tools: sanitized start, outcome, no raw output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_start_is_sanitized_and_result_shows_outcome_not_output():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    cmd = f'curl -H "Authorization: Bearer {SECRET}" https://api.example.com/v1/models'
    a("tool.started", "terminal", cmd, {"command": cmd})
    await pub.drain()
    card = adapter.card()
    assert SECRET not in card
    assert "terminal" in card
    snap = pub.tracker.snapshot("deleg_aaaa0001")[0]
    assert snap["state"] == "tool-running"
    assert snap["current_tool"] == "terminal"

    a("tool.completed", "terminal", None, None, duration=2.5, is_error=True,
      result=f"HTTP 401 key={SECRET} RAW-OUTPUT-BODY")
    await pub.drain()
    card = adapter.card()
    snap = pub.tracker.snapshot("deleg_aaaa0001")[0]
    assert snap["state"] == "running"
    assert snap["tools_failed"] == 1 and snap["tools_ok"] == 0
    assert "RAW-OUTPUT-BODY" not in card and SECRET not in card
    await pub.aclose()


def test_tool_summary_truncates_long_multiline_commands():
    from agent.delegation_activity import summarize_tool_call

    cmd = "python - <<'EOF'\n" + "x = 1\n" * 50 + "EOF"
    out = summarize_tool_call("terminal", None, {"command": cmd})
    assert "\n" not in out
    assert len(out) <= 80


# ---------------------------------------------------------------------------
# Heartbeat: waiting vs stalled from observed state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quiet_heartbeat_distinguishes_waiting_from_stalled():
    clock = FakeClock()
    liveness = {"registered": True, "seconds_since_activity": 5.0, "current_tool": None,
                "activity": "waiting for API response"}
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter, clock=clock, heartbeat_seconds=60, stall_seconds=600,
                          probe=lambda sid: dict(liveness))
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    a("_thinking", "Reading the scheduler")
    await pub.drain()
    calls_before = len(adapter.status_calls)

    # Quiet but not yet a heartbeat interval: no extra edit.
    clock.advance(30)
    await pub.heartbeat_tick()
    assert len(adapter.status_calls) == calls_before

    # Quiet past the heartbeat interval with live model activity -> waiting.
    clock.advance(40)
    await pub.heartbeat_tick()
    snap = pub.tracker.snapshot("deleg_aaaa0001")[0]
    assert snap["state"] == "waiting"
    assert "model" in (snap["reason"] or "")
    assert len(adapter.status_calls) == calls_before + 1
    assert adapter.sends == []  # a heartbeat never posts a new message

    # Observed inactivity past the stall threshold -> blocked + ONE alert.
    clock.advance(700)
    liveness["seconds_since_activity"] = 700.0
    await pub.heartbeat_tick()
    snap = pub.tracker.snapshot("deleg_aaaa0001")[0]
    assert snap["state"] == "blocked"
    assert "no activity" in snap["reason"]
    assert len(adapter.sends) == 1
    assert "Audit gateway auth" in adapter.sends[0]["content"]
    clock.advance(120)
    await pub.heartbeat_tick()
    assert len(adapter.sends) == 1  # not re-alerted every tick

    # Real activity resumes -> back to running, no speculation retained.
    a("tool.started", "read_file", "x.py", {"path": "x.py"})
    await pub.drain()
    assert pub.tracker.snapshot("deleg_aaaa0001")[0]["state"] == "tool-running"
    await pub.aclose()


@pytest.mark.asyncio
async def test_long_running_tool_is_waiting_not_stalled():
    clock = FakeClock()
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(
        adapter, clock=clock, heartbeat_seconds=60, stall_seconds=600,
        probe=lambda sid: {"registered": True, "seconds_since_activity": 900.0,
                           "current_tool": "terminal", "activity": "executing tool"},
    )
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    a("tool.started", "terminal", "pytest -q", {"command": "pytest -q"})
    clock.advance(900)
    await pub.heartbeat_tick()
    snap = pub.tracker.snapshot("deleg_aaaa0001")[0]
    assert snap["state"] == "tool-running"
    assert adapter.sends == []
    assert "15m" in adapter.card()
    await pub.aclose()


@pytest.mark.asyncio
async def test_worker_that_vanished_without_completion_is_reported_failed():
    clock = FakeClock()
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter, clock=clock, probe=lambda sid: {"registered": False})
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    clock.advance(61)
    await pub.heartbeat_tick()
    snap = pub.tracker.snapshot("deleg_aaaa0001")[0]
    assert snap["state"] == "failed"
    assert "without a completion report" in snap["reason"]
    await pub.aclose()


# ---------------------------------------------------------------------------
# Completion / error / cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_error_and_cancellation_final_states():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    a = _child_cb(runner, index=0, count=3, subagent_id="sa-1", title="Fix auth")
    b = _child_cb(runner, index=1, count=3, subagent_id="sa-2", title="Port tests")
    c = _child_cb(runner, index=2, count=3, subagent_id="sa-3", title="Update docs")
    for cb in (a, b, c):
        cb("subagent.start", preview="g")

    a("subagent.complete", preview="Fixed", status="completed", duration_seconds=75.0,
      summary=f"Fixed the scope leak in authz_mixin. Commit abc1234. Tests: 12 passed. token={SECRET}",
      files_written=["gateway/authz_mixin.py", "tests/gateway/test_authz.py"], api_calls=6)
    b("subagent.complete", preview="boom", status="failed", duration_seconds=12.0,
      summary="ImportError: no module named foo")
    c("subagent.complete", preview="stopped", status="interrupted", duration_seconds=5.0,
      summary="")
    await pub.drain()

    snap = {s["subagent_id"]: s for s in pub.tracker.snapshot("deleg_aaaa0001")}
    assert snap["sa-1"]["state"] == "completed"
    assert snap["sa-2"]["state"] == "failed"
    assert snap["sa-3"]["state"] == "cancelled"
    assert pub.tracker.group_finished("deleg_aaaa0001")
    # Every result is reported ONCE, in one summary message at the bottom:
    # no per-worker ✅/❌/⏹ pings.
    assert len(adapter.sends) == 1
    final = adapter.summary()
    assert SECRET not in final
    assert final.startswith("❌ 3 subagents ·") and "⏱" in final.splitlines()[0]
    assert "Fixed the scope leak" in final
    assert "Port tests" in final and "failed" in final.lower()
    assert "Update docs" in final and "stopped" in final.lower()
    assert "approved" not in final.lower()
    await pub.aclose()


@pytest.mark.asyncio
async def test_timeout_maps_to_failed_with_reason():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    a("subagent.complete", preview="Timed out after 600s", status="timeout",
      duration_seconds=600.0, summary="")
    await pub.drain()
    snap = pub.tracker.snapshot("deleg_aaaa0001")[0]
    assert snap["state"] == "failed"
    assert "timed out" in snap["reason"].lower()
    await pub.aclose()


# ---------------------------------------------------------------------------
# Redaction everywhere text reaches the transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secrets_are_redacted_from_title_note_tool_and_summary():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    a = _child_cb(runner, title=f"Rotate key {SECRET}", goal=f"Rotate {SECRET}")
    a("subagent.start", preview=f"Rotate {SECRET}")
    a("_thinking", f"Using OPENAI_API_KEY={SECRET} to test")
    a("tool.started", "write_file", f"/tmp/{SECRET}", {"path": "/tmp/x", "content": SECRET})
    a("subagent.complete", status="failed", summary=f"leaked {SECRET}", duration_seconds=1)
    await pub.drain()
    everything = "\n".join(
        [c["content"] for c in adapter.status_calls] + [s["content"] for s in adapter.sends]
    )
    assert SECRET not in everything
    await pub.aclose()


# ---------------------------------------------------------------------------
# Isolation: chat / thread / profile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_events_never_cross_chats_threads_or_profiles():
    adapter_a = FakeTelegramAdapter()  # profile A's telegram adapter
    adapter_b = FakeTelegramAdapter()  # profile B's telegram adapter
    pub_a = _make_publisher(adapter_a, chat_id="chat-A", metadata={"thread_id": "11"})
    pub_b = _make_publisher(adapter_b, chat_id="chat-B", metadata={"thread_id": "22"})
    runner_a = _turn_runner(pub_a, _source("chat-A", "11"))
    runner_b = _turn_runner(pub_b, _source("chat-B", "22"))

    a = _child_cb(runner_a, subagent_id="sa-A", title="Alpha work", delegation_id="deleg_A")
    b = _child_cb(runner_b, subagent_id="sa-B", title="Beta work", delegation_id="deleg_B")
    a("subagent.start", preview="g")
    b("subagent.start", preview="g")
    a("tool.started", "read_file", "alpha.py", {"path": "alpha.py"})
    await pub_a.drain()
    await pub_b.drain()

    assert {c["chat_id"] for c in adapter_a.status_calls} == {"chat-A"}
    assert {c["chat_id"] for c in adapter_b.status_calls} == {"chat-B"}
    assert all(c["metadata"] == {"thread_id": "11"} for c in adapter_a.status_calls)
    assert all(c["metadata"] == {"thread_id": "22"} for c in adapter_b.status_calls)
    assert "Beta work" not in adapter_a.card("chat-A")
    assert "Alpha work" not in adapter_b.card("chat-B")
    assert "alpha.py" not in adapter_b.card("chat-B")
    await pub_a.aclose()
    await pub_b.aclose()


# ---------------------------------------------------------------------------
# Transport failure, flood control, stale/replayed events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_failure_never_reaches_the_child_and_recovers():
    adapter = FakeTelegramAdapter(raise_exc=True)
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    # Must not raise into the child's loop even though delivery explodes.
    a("subagent.start", preview="g")
    a("tool.started", "read_file", "x.py", {"path": "x.py"})
    await pub.drain()
    assert adapter.status_calls  # delivery was attempted
    adapter._raise = False
    a("subagent.complete", status="completed", summary="done", duration_seconds=3)
    await pub.drain()
    snap = pub.tracker.snapshot("deleg_aaaa0001")[0]
    assert snap["state"] == "completed"
    assert "done" in adapter.summary().lower()
    await pub.aclose()


@pytest.mark.asyncio
async def test_flood_control_retry_after_is_honored_and_latest_state_wins():
    clock = FakeClock()
    adapter = FakeTelegramAdapter(fail=1, retry_after=30.0)
    pub = _make_publisher(adapter, clock=clock)
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    await pub.drain()
    assert len(adapter.status_calls) == 1  # rejected by flood control
    a("tool.started", "read_file", "a.py", {"path": "a.py"})
    a("tool.started", "read_file", "b.py", {"path": "b.py"})
    await pub.flush()
    assert len(adapter.status_calls) == 1  # still inside retry_after window
    clock.advance(31)
    await pub.flush()
    assert len(adapter.status_calls) == 2
    assert "b.py" in adapter.card()  # coalesced: latest state, not a backlog
    await pub.aclose()


@pytest.mark.asyncio
async def test_stale_and_replayed_events_cannot_regress_final_state():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    a("subagent.complete", status="completed", summary="all good", duration_seconds=4)
    # Late/replayed events from a racing thread or a duplicate relay.
    a("tool.started", "terminal", "rm -rf build", {"command": "rm -rf build"})
    a("subagent.start", preview="g")
    a("subagent.complete", status="failed", summary="dup", duration_seconds=9)
    await pub.drain()
    snap = pub.tracker.snapshot("deleg_aaaa0001")[0]
    assert snap["state"] == "completed"
    # Exactly one finding for the real completion; the replayed "failed"
    # completion produced no second message.
    assert len(adapter.sends) == 1 and "all good" in adapter.summary()
    assert "rm -rf build" not in adapter.summary()
    await pub.aclose()


@pytest.mark.asyncio
async def test_stale_run_does_not_open_new_card_but_finalizes_existing():
    current = {"v": True}
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter, is_current=lambda: current["v"])
    runner = _turn_runner(pub)
    a = _child_cb(runner, delegation_id="deleg_live")
    a("subagent.start", preview="g")
    await pub.drain()
    assert adapter.status_calls

    # The turn moved on (e.g. /new). A brand-new group must not open a card...
    current["v"] = False
    late = _child_cb(runner, subagent_id="sa-9", delegation_id="deleg_late")
    late("subagent.start", preview="g")
    await pub.drain()
    assert all(c["key"] != "delegation:deleg_late" for c in adapter.status_calls)
    # ...but the existing card still reaches an accurate final state.
    a("subagent.complete", status="interrupted", summary="", duration_seconds=2)
    await pub.drain()
    assert pub.tracker.snapshot("deleg_live")[0]["state"] == "cancelled"
    assert "stopped" in adapter.summary().lower()
    assert adapter.deleted, "the live card is removed once the summary lands"
    await pub.aclose()


# ---------------------------------------------------------------------------
# Config defaults / opt-out preservation / propagation
# ---------------------------------------------------------------------------


def test_default_and_opt_out_resolution():
    from gateway.delegation_activity import resolve_delegation_activity

    # Fresh install: Telegram shows the card (tool progress is on there).
    on, hb = resolve_delegation_activity({}, "telegram")
    assert on is True and hb == 60
    # Slack keeps tool progress off by default -> auto stays quiet.
    assert resolve_delegation_activity({}, "slack")[0] is False
    # Existing install that opted out of tool progress keeps its silence.
    cfg = {"display": {"platforms": {"telegram": {"tool_progress": "off"}}}}
    assert resolve_delegation_activity(cfg, "telegram")[0] is False
    # ...unless the user explicitly opts in to delegation activity.
    cfg["display"]["platforms"]["telegram"]["delegation_activity"] = "on"
    assert resolve_delegation_activity(cfg, "telegram")[0] is True
    # Explicit off wins over tool progress being on (YAML bare off -> False).
    assert resolve_delegation_activity(
        {"display": {"delegation_activity": False}}, "telegram"
    )[0] is False
    # Heartbeat is configurable; 0 disables it; garbage falls back to default.
    assert resolve_delegation_activity(
        {"display": {"delegation_heartbeat_seconds": 0}}, "telegram"
    )[1] == 0
    assert resolve_delegation_activity(
        {"display": {"delegation_heartbeat_seconds": "nope"}}, "telegram"
    )[1] == 60


def test_default_config_declares_display_keys():
    from clover_cli.config import DEFAULT_CONFIG

    display = DEFAULT_CONFIG["display"]
    assert display["delegation_activity"] in {"auto", "on", "off"}
    assert isinstance(display["delegation_heartbeat_seconds"], int)


def test_canonical_config_writer_round_trips_through_gateway_resolver(
    tmp_path, monkeypatch, capsys
):
    import yaml

    from clover_cli.config import load_config, set_config_value
    from gateway.delegation_activity import resolve_delegation_activity

    home = tmp_path / ".clover"
    home.mkdir()
    monkeypatch.setenv("CLOVER_HOME", str(home))
    # `clover config set` path: the canonical user-facing writer.
    set_config_value("display.platforms.telegram.delegation_activity", "off")
    set_config_value("display.delegation_heartbeat_seconds", "120")
    out = capsys.readouterr()
    assert "Unknown" not in out.out + out.err and "unknown" not in out.out + out.err
    # Gateway reads raw YAML; CLI subcommands read the merged config.
    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert resolve_delegation_activity(raw, "telegram") == (False, 120)
    merged = load_config()
    assert resolve_delegation_activity(merged, "telegram") == (False, 120)
    # Other platforms are untouched by a per-platform opt-out.
    assert resolve_delegation_activity(raw, "discord")[0] is True


def test_gateway_attaches_progress_callback_when_only_delegation_activity_is_on():
    """tool_progress off + explicit delegation_activity on must still observe."""
    from gateway.run import _needs_agent_progress_callback

    ctx = TurnContext(delegation_activity=object())
    assert _needs_agent_progress_callback(ctx) is True
    assert _needs_agent_progress_callback(TurnContext()) is False


# ---------------------------------------------------------------------------
# Surfaces that already consume subagent.* must not change behavior
# ---------------------------------------------------------------------------


def test_tui_gateway_does_not_forward_gateway_only_event_types(monkeypatch):
    from tui_gateway import server

    emitted = []
    monkeypatch.setattr(server, "_emit", lambda et, sid, payload: emitted.append(et))
    monkeypatch.setattr(server, "_mirror_subagent_to_child", lambda *a, **k: None)
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda sid: True, raising=False)
    for et in ("subagent.queued", "subagent.tool_done"):
        server._on_tool_progress("sid-1", et, "read_file", None, None,
                                 subagent_id="sa-1", goal="g")
    assert emitted == []
    server._on_tool_progress("sid-1", "subagent.start", None, "g", None,
                             subagent_id="sa-1", goal="g")
    assert emitted == ["subagent.start"]


def test_liveness_probe_reads_live_registry_only():
    from tools import delegate_tool

    assert delegate_tool.get_subagent_liveness("nope-unknown") == {"registered": False}

    class _Agent:
        def get_activity_summary(self):
            return {"seconds_since_activity": 12.0, "current_tool": "terminal",
                    "last_activity_desc": "executing tool"}

    delegate_tool._register_subagent({"subagent_id": "sa-live", "agent": _Agent()})
    try:
        info = delegate_tool.get_subagent_liveness("sa-live")
        assert info["registered"] is True
        assert info["seconds_since_activity"] == 12.0
        assert info["current_tool"] == "terminal"
    finally:
        delegate_tool._unregister_subagent("sa-live")
    assert delegate_tool.get_subagent_liveness("sa-live") == {"registered": False}


# ---------------------------------------------------------------------------
# Activity-first presentation (follow-up acceptance): rendered frames
# ---------------------------------------------------------------------------


def _card_lines(adapter):
    return adapter.card().splitlines()


@pytest.mark.asyncio
async def test_rendered_frames_finished_workers_leave_the_active_feed():
    clock = FakeClock()
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter, clock=clock,
                          probe=lambda sid: {"registered": True, "seconds_since_activity": 2.0})
    runner = _turn_runner(pub)
    a = _child_cb(runner, index=0, count=3, subagent_id="sa-a", title="Audit auth scoping")
    b = _child_cb(runner, index=1, count=3, subagent_id="sa-b", title="Port cron tests",
                  model="gpt-5.5", provider="openrouter")
    c = _child_cb(runner, index=2, count=3, subagent_id="sa-c", title="Draft release notes")
    for cb in (a, b, c):
        cb("subagent.queued", preview="g")
    a("subagent.start", preview="g")
    b("subagent.start", preview="g")

    # Frame 1: one line per worker, showing what it is doing right now.
    for path in ("a.py", "b.py", "c.py", "d.py"):
        a("tool.started", "read_file", path, {"path": path})
        clock.advance(1)
        a("tool.completed", "read_file", None, None, duration=0.1, is_error=False)
    a("_thinking", "Scoping check looks wrong in authz_mixin")
    b("tool.started", "terminal", "pytest", {"command": "pytest tests/cron -q"})
    await pub.drain()
    frame1 = _card_lines(adapter)
    assert frame1[0].startswith("🔀 3 subagents")
    line_a = next(l for l in frame1 if "Audit auth scoping" in l)
    assert "Opus 5.5" in line_a and "Scoping check looks wrong" in line_a
    line_b = next(l for l in frame1 if "Port cron tests" in l)
    assert "GPT-5.5" in line_b and "terminal pytest tests/cron -q" in line_b
    assert "openrouter" not in line_b
    # Finished tool calls don't pile up as a scrolling log.
    assert not any("read_file" in l for l in frame1)
    assert "Recent:" not in frame1

    # Frame 2: b finishes -> leaves the live card, counted in the header;
    # no separate per-worker message.
    clock.advance(3)
    b("tool.completed", "terminal", None, None, duration=3.0, is_error=False)
    b("subagent.complete", status="completed", duration_seconds=10,
      summary="Ported 14 cron tests; all pass.")
    c("subagent.start", preview="g")
    await pub.drain()
    frame2 = _card_lines(adapter)
    assert not any("Port cron tests" in l for l in frame2)
    assert "1 done" in frame2[0]
    assert adapter.sends == []

    # Frame 3 (heartbeat): says what is pending; never posts.
    clock.advance(70)
    await pub.heartbeat_tick()
    frame3 = _card_lines(adapter)
    line_a = next(l for l in frame3 if "Audit auth scoping" in l)
    assert "waiting for model response" in line_a
    assert adapter.sends == []

    # Final: ONE summary message posted at the bottom; live card removed.
    a("subagent.complete", status="completed", duration_seconds=80, summary="Found the leak.")
    c("subagent.complete", status="failed", duration_seconds=70, summary="network error")
    await pub.drain()
    assert len(adapter.sends) == 1
    final = adapter.summary()
    assert final.startswith("❌ 3 subagents ·") and "⏱" in final.splitlines()[0]
    for title in ("Audit auth scoping", "Port cron tests", "Draft release notes"):
        assert title in final
    assert "Found the leak." in final and "Ported 14 cron tests" in final
    assert "network error" in final
    assert "▸" not in final
    assert adapter.deleted and not adapter.cards
    await pub.aclose()


@pytest.mark.asyncio
async def test_active_card_stays_compact_with_many_workers():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    cbs = [_child_cb(runner, index=i, count=12, subagent_id=f"sa-{i}", title=f"Worker {i}")
           for i in range(12)]
    for i, cb in enumerate(cbs):
        cb("subagent.start", preview="g")
        cb("tool.started", "web_search", f"q{i}", {"query": f"q{i}"})
        cb("tool.completed", "web_search", None, None, duration=0.2, is_error=False)
    for cb in cbs[:6]:
        cb("subagent.complete", status="completed", duration_seconds=5, summary="ok")
    await pub.drain()
    lines = _card_lines(adapter)
    # header + <=6 worker lines + overflow
    assert len(lines) <= 1 + 6 + 1
    assert not any(f"Worker {i} " in l or l.endswith(f"Worker {i}") for i in range(6) for l in lines[1:])
    await pub.aclose()


# ---------------------------------------------------------------------------
# Edit-only adapters (Discord, Matrix, Mattermost, ...): no send_or_update_status
# ---------------------------------------------------------------------------


class FakeEditOnlyAdapter:
    """Discord-shaped transport: send() + edit_message(), no status keying."""

    MAX_MESSAGE_LENGTH = 2000

    def __init__(self, *, edit_failures=None) -> None:
        # Each queued entry is consumed by one edit_message call:
        # a float retry_after, "gone" (message deleted), or "error".
        self.edit_failures = list(edit_failures or [])
        self.sends: List[Dict[str, Any]] = []
        self.edits: List[Dict[str, Any]] = []
        self.messages: Dict[str, str] = {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        from gateway.platforms.base import SendResult

        if len(content) > self.MAX_MESSAGE_LENGTH:
            return SendResult(success=False, error="message too long")
        mid = f"s{len(self.sends) + 1}"
        self.sends.append({"chat_id": chat_id, "content": content})
        self.messages[mid] = content
        return SendResult(success=True, message_id=mid)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        from gateway.platforms.base import SendResult

        self.edits.append({"message_id": message_id, "content": content})
        if len(content) > self.MAX_MESSAGE_LENGTH:
            return SendResult(success=False, error="message too long")
        if self.edit_failures:
            kind = self.edit_failures.pop(0)
            if kind == "gone":
                return SendResult(success=False, error="Unknown Message (404)")
            if kind == "error":
                return SendResult(success=False, error="connection reset")
            return SendResult(success=False, error="rate limited", retry_after=float(kind))
        self.messages[message_id] = content
        return SendResult(success=True, message_id=message_id)


@pytest.mark.asyncio
async def test_edit_only_transient_failure_backs_off_then_keeps_editing_same_card():
    clock = FakeClock()
    adapter = FakeEditOnlyAdapter(edit_failures=[5.0])
    pub = _make_publisher(adapter, clock=clock)
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    await pub.drain()
    assert len(adapter.sends) == 1
    a("tool.started", "read_file", "a.py", {"path": "a.py"})
    await pub.flush()  # edit rejected with retry_after=5
    assert len(adapter.edits) == 1
    a("tool.started", "read_file", "b.py", {"path": "b.py"})
    await pub.flush()
    assert len(adapter.edits) == 1, "retry_after must be honoured on edit-only adapters"
    clock.advance(6)
    await pub.flush()
    assert len(adapter.edits) == 2
    assert "b.py" in adapter.messages["s1"], "card must not freeze after one failed edit"
    a("subagent.complete", status="completed", summary="done", duration_seconds=3)
    await pub.drain()
    # Final summary is a NEW message at the bottom; the old live card is
    # retired (edit-only adapters can't delete, so it points below).
    assert len(adapter.sends) == 2
    assert adapter.sends[-1]["content"].startswith("✅ Opus 5.5")
    assert "summary below" in adapter.messages["s1"]
    await pub.aclose()


@pytest.mark.asyncio
async def test_edit_only_plain_error_is_retried_not_marked_delivered():
    clock = FakeClock()
    adapter = FakeEditOnlyAdapter(edit_failures=["error"])
    pub = _make_publisher(adapter, clock=clock)
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    await pub.drain()
    a("tool.started", "read_file", "a.py", {"path": "a.py"})
    await pub.flush()
    assert "a.py" not in adapter.messages["s1"]
    clock.advance(5)
    await pub.flush()
    assert "a.py" in adapter.messages["s1"]
    assert len(adapter.sends) == 1
    await pub.aclose()


@pytest.mark.asyncio
async def test_edit_only_deleted_card_is_reposted_once():
    clock = FakeClock()
    adapter = FakeEditOnlyAdapter(edit_failures=["gone"])
    pub = _make_publisher(adapter, clock=clock)
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    await pub.drain()
    a("tool.started", "read_file", "a.py", {"path": "a.py"})
    await pub.drain()
    clock.advance(5)
    await pub.flush()
    assert len(adapter.sends) == 2, "a deleted card is replaced by one new card"
    assert "a.py" in adapter.messages["s2"]
    a("tool.started", "read_file", "b.py", {"path": "b.py"})
    await pub.drain()
    assert "b.py" in adapter.messages["s2"] and len(adapter.sends) == 2
    await pub.aclose()


@pytest.mark.asyncio
async def test_card_fits_the_adapter_message_limit():
    adapter = FakeEditOnlyAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    cbs = [_child_cb(runner, index=i, count=12, subagent_id=f"sa-{i}",
                     title=f"Worker {i} " + "long title " * 8) for i in range(12)]
    for i, cb in enumerate(cbs):
        cb("subagent.start", preview="g")
        cb("tool.started", "terminal", "x" * 300, {"command": "echo " + "x" * 300})
    for cb in cbs:
        cb("subagent.complete", status="completed", duration_seconds=5, summary="ok " * 80)
    await pub.drain()
    assert adapter.sends, "card must be delivered"
    assert all(len(m) <= adapter.MAX_MESSAGE_LENGTH for m in adapter.messages.values())
    await pub.aclose()


@pytest.mark.asyncio
async def test_groups_without_delegation_id_get_a_per_publisher_key():
    adapter = FakeTelegramAdapter()
    pub1 = _make_publisher(adapter)
    pub2 = _make_publisher(adapter)
    for pub in (pub1, pub2):
        runner = _turn_runner(pub)
        a = _child_cb(runner, delegation_id=None)
        a("subagent.start", preview="g")
        await pub.drain()
    keys = {k for (_c, k) in adapter.cards}
    assert len(keys) == 2, f"two turns must not share one status card: {keys}"
    await pub1.aclose()
    await pub2.aclose()


# ---------------------------------------------------------------------------
# Parent and subagent finishing at the same time
# ---------------------------------------------------------------------------


def _busy_publisher(adapter, *, clock=None, defer=True, max_final_defer=90.0):
    from gateway.delegation_activity import DelegationActivityPublisher

    state = {"busy": True, "released": []}

    def defer_final(release):
        if not defer:
            return False
        state["released"].append(release)
        return True

    pub = DelegationActivityPublisher(
        adapter=adapter,
        chat_id="chat-A",
        loop=asyncio.get_running_loop(),
        min_interval=0.0,
        clock=clock or FakeClock(),
        auto_heartbeat=False,
        is_parent_busy=lambda: state["busy"],
        defer_final=defer_final,
        max_final_defer=max_final_defer,
    )
    return pub, state


@pytest.mark.asyncio
async def test_summary_waits_for_the_parent_reply_then_posts_below_it():
    """While the parent turn is still working, a finished subagent's summary
    is held (the live card stays), so it never lands between the parent's
    tool bubbles and its answer. After the parent's reply is delivered, the
    summary is posted as the newest message and the live card is removed."""
    adapter = FakeTelegramAdapter()
    pub, state = _busy_publisher(adapter)
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    a("tool.started", "read_file", "x.py", {"path": "x.py"})
    await pub.drain()
    a("subagent.complete", status="completed", summary="Root cause found.", duration_seconds=9)
    await pub.drain()
    assert adapter.sends == [], "summary must wait while the parent is still working"
    assert len(state["released"]) == 1, "summary registered to fire after the parent's reply"
    assert adapter.cards, "live card stays up while waiting"

    # Parent's reply is delivered -> post-delivery callback fires.
    await adapter.send("chat-A", "PARENT REPLY")
    state["busy"] = False
    state["released"][0]()
    await pub.drain()
    contents = [s["content"] for s in adapter.sends]
    assert contents[0] == "PARENT REPLY"
    assert contents[-1].startswith("✅ Opus 5.5") and "Root cause found." in contents[-1]
    assert len(contents) == 2, "exactly one summary, after the parent's reply"
    assert adapter.deleted and not adapter.cards, "live card removed"
    await pub.aclose()


@pytest.mark.asyncio
async def test_summary_posts_immediately_when_parent_is_idle():
    adapter = FakeTelegramAdapter()
    pub, state = _busy_publisher(adapter)
    state["busy"] = False
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    await pub.drain()
    a("subagent.complete", status="completed", summary="Done.", duration_seconds=3)
    await pub.drain()
    assert len(adapter.sends) == 1 and "Done." in adapter.summary()
    assert state["released"] == []
    await pub.aclose()


@pytest.mark.asyncio
async def test_summary_falls_back_if_the_parent_reply_never_releases_it(monkeypatch):
    """A parent that crashes or never delivers must not strand the summary:
    the hard cap releases it, and a late hook after that doesn't double-post."""
    import gateway.delegation_activity as da

    monkeypatch.setattr(da, "_SUMMARY_POLL_SECONDS", 0.01)
    clock = FakeClock()
    adapter = FakeTelegramAdapter()
    pub, state = _busy_publisher(adapter, clock=clock, max_final_defer=60)
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    await pub.drain()
    a("subagent.complete", status="completed", summary="Still delivered.", duration_seconds=3)
    await pub.drain()
    await asyncio.sleep(0.05)
    await pub.drain()
    assert adapter.sends == [], "busy and under the cap: keep holding"
    clock.advance(61)
    await asyncio.sleep(0.05)
    await pub.drain()
    assert len(adapter.sends) == 1 and "Still delivered." in adapter.summary()
    state["released"][0]()
    await pub.drain()
    assert len(adapter.sends) == 1
    await pub.aclose()


@pytest.mark.asyncio
async def test_lost_hook_still_posts_once_parent_goes_idle(monkeypatch):
    """The post-delivery hook can be overwritten by a newer run or never
    fire; polling must release the summary once the parent is idle, and
    never while it's still working."""
    import gateway.delegation_activity as da

    monkeypatch.setattr(da, "_SUMMARY_POLL_SECONDS", 0.01)
    adapter = FakeTelegramAdapter()
    pub, state = _busy_publisher(adapter)  # hook registered, never called
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    await pub.drain()
    a("subagent.complete", status="completed", summary="Polled out.", duration_seconds=3)
    await pub.drain()
    for _ in range(5):
        await asyncio.sleep(0.02)
        await pub.drain()
    assert adapter.sends == [], "never posted while the parent is still working"
    state["busy"] = False
    for _ in range(5):
        await asyncio.sleep(0.02)
        await pub.drain()
    assert len(adapter.sends) == 1 and "Polled out." in adapter.summary()
    await pub.aclose()


@pytest.mark.asyncio
async def test_held_summary_shows_result_on_live_card_while_parent_works():
    adapter = FakeTelegramAdapter()
    pub, state = _busy_publisher(adapter)
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    await pub.drain()
    a("subagent.complete", status="completed", summary="Visible early.", duration_seconds=3)
    await pub.drain()
    assert adapter.sends == []
    assert "Visible early." in adapter.card()
    await pub.aclose()


@pytest.mark.asyncio
async def test_ambiguous_summary_send_error_is_not_retried():
    """A send that raises may still have been accepted (timeout): retrying
    could post the summary twice."""

    class _FlakySend(FakeTelegramAdapter):
        calls = 0

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            type(self).calls += 1
            raise TimeoutError("read timeout")

    adapter = _FlakySend()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    await pub.drain()
    a("subagent.complete", status="completed", summary="x", duration_seconds=1)
    await pub.drain()
    await pub.flush()
    await pub.flush()
    assert _FlakySend.calls == 1
    await pub.aclose()


@pytest.mark.asyncio
async def test_single_subagent_card_is_compact_and_readable():
    clock = FakeClock()
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter, clock=clock)
    runner = _turn_runner(pub)
    a = _child_cb(runner, title="Find why the summary card is skipped")
    a("subagent.start", preview="g")
    a("_thinking", "Two injection sites, checking run.py")
    a("tool.started", "Read", "/home/x/repo/gateway/platforms/base.py",
      {"path": "/home/x/repo/gateway/platforms/base.py"})
    clock.advance(72)
    await pub.drain()
    lines = _card_lines(adapter)
    assert lines == [
        "🔀 Opus 5.5 · 🛠 1 tool call · ⏱ 1m12s",
        "> Find why the summary card is skipped",
        "> 🔧 Read base.py",
        "> 💬 Two injection sites, checking run.py",
    ]
    await pub.aclose()


@pytest.mark.asyncio
async def test_without_a_delivery_hook_summary_polls_until_parent_is_idle(monkeypatch):
    adapter = FakeTelegramAdapter()
    pub, state = _busy_publisher(adapter, defer=False)
    import gateway.delegation_activity as da

    delays = []
    real_call_later = pub._loop.call_later

    def fast_call_later(delay, cb, *args):
        delays.append(delay)
        return real_call_later(0.01, cb, *args)

    monkeypatch.setattr(pub._loop, "call_later", fast_call_later)
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    await pub.drain()
    a("subagent.complete", status="completed", summary="Polled.", duration_seconds=3)
    await pub.drain()
    await asyncio.sleep(0.05)
    await pub.drain()
    assert adapter.sends == [], "still busy: keep holding"
    state["busy"] = False
    await asyncio.sleep(0.05)
    await pub.drain()
    assert len(adapter.sends) == 1 and "Polled." in adapter.summary()
    assert da  # module imported for monkeypatch scope
    await pub.aclose()


@pytest.mark.asyncio
async def test_defer_final_only_registers_with_the_run_generation(monkeypatch):
    """Registering the summary release without the run generation would
    overwrite the turn's generation-tagged slot and drop its summary card."""
    import gateway.delegation_activity as da

    monkeypatch.setattr(da, "resolve_delegation_activity", lambda cfg, key: (True, 60))
    registered = []

    class _Adapter(FakeTelegramAdapter):
        def __init__(self):
            super().__init__()
            self._active_sessions = {}

        def register_post_delivery_callback(self, key, cb, *, generation=None):
            registered.append((key, generation))

    adapter = _Adapter()

    class _Runner:
        def _adapter_for_source(self, source):
            return adapter

        def _thread_metadata_for_source(self, source):
            return None

        def _session_key_for_source(self, source):
            return "sk"

    pub = da.build_turn_publisher(_Runner(), _source(), {}, "telegram", lambda: True)
    event = asyncio.Event()
    adapter._active_sessions["sk"] = event
    assert pub._parent_busy() is True
    assert pub._defer_final(lambda: None) is False, "no generation bound: must not register"
    assert registered == []
    event._clover_run_generation = 7
    assert pub._defer_final(lambda: None) is True
    assert registered == [("sk", 7)]
    del adapter._active_sessions["sk"]
    assert pub._parent_busy() is False
    await pub.aclose()


def test_plain_summary_prefers_the_workers_plain_paragraph():
    from agent.delegation_activity import plain_summary

    answer = (
        "## Findings\n1. **Polling never fires** (`gateway/x.py:474-477`).\n\n"
        "Plain summary: I found three problems. Each one has a small fix."
    )
    assert plain_summary(answer) == "I found three problems. Each one has a small fix."


def test_plain_summary_fallback_strips_code_paths_and_broken_sentences():
    from agent.delegation_activity import plain_summary

    answer = (
        "Found three issues.\n\n1. **The polling fallback never fires** "
        "(`gateway/delegation_activity.py:474-477`). `_recheck_summary` (:454) "
        "clears `summary_waiting`, so the deadline resets."
    )
    out = plain_summary(answer)
    assert out == "Found three issues. The polling fallback never fires."
    for bad in ("`", "**", "gateway/", ":474", "clears ,"):
        assert bad not in out


def test_plain_summary_is_redacted_and_bounded():
    from agent.delegation_activity import plain_summary

    out = plain_summary("Plain summary: " + ("Done. " * 200) + SECRET)
    assert SECRET not in out and len(out) <= 320


@pytest.mark.asyncio
async def test_subagent_output_is_quoted_apart_from_the_main_chat():
    """Everything a subagent produced sits in a blockquote under a one-line
    header, so it reads as separate from the main agent's own messages."""
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    a("tool.started", "read_file", "x.py", {"path": "x.py"})
    await pub.drain()
    live = adapter.card().splitlines()
    assert live[0].startswith("🔀 Opus 5.5") and all(l.startswith("> ") for l in live[1:])
    a("subagent.complete", status="completed", duration_seconds=3,
      summary="**Done** (`a/b.py:3`).\n\nPlain summary: I checked the file. It is fine.")
    await pub.drain()
    final = adapter.summary().splitlines()
    assert final[0].startswith("✅ Opus 5.5")
    assert all(l.startswith("> ") for l in final[1:])
    assert final[-1] == "> I checked the file. It is fine."
    await pub.aclose()


def test_child_prompt_asks_for_a_plain_summary():
    from tools.delegate_tool import _build_child_system_prompt

    prompt = _build_child_system_prompt("do x")
    assert "Plain summary:" in prompt and "Simplified Technical English" in prompt



@pytest.mark.parametrize("answer, expected", [
    ("Fixed it.\n\nPlain summary: I fixed the login bug. It works now.\n\n```\nrm -rf build\n```\nThanks!",
     "I fixed the login bug. It works now."),
    ("Plain summary: Real one.\n\n> Plain summary: fake from a file", "Real one."),
    ("Ran this:\n```\nrm -rf build\n```\nIt worked.", "Ran this: It worked."),
    ("Open 24/7 and/or on 2026/09/27. See https://ex.com/docs/page for details.",
     "Open 24/7 and/or on 2026/09/27. See https://ex.com/docs/page for details."),
    ("Meet at 10:30. Ratio is 3:1. Bug in gateway/run.py:31011 fixed.",
     "Meet at 10:30. Ratio is 3:1. Bug in run.py fixed."),
    ("- Fixed login\n- Added tests", "Fixed login. Added tests."),
    ("npm install fixed it. Then tests passed.", "npm install fixed it. Then tests passed."),
])
def test_plain_summary_review_cases(answer, expected):
    """Cases from the Opus review: stop at the plain paragraph, ignore quoted
    markers and fenced code, keep URLs/dates/times/ratios, split bullets."""
    from agent.delegation_activity import plain_summary

    assert plain_summary(answer) == expected


@pytest.mark.asyncio
async def test_header_matches_the_main_agent_turn_card():
    """Same shape as '🧠 N thoughts · 🛠 N tool calls · ⏱ Ns' so the two read
    as one system: model · tool calls · time."""
    clock = FakeClock()
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter, clock=clock)
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    a("tool.started", "read_file", "a.py", {"path": "a.py"})
    a("tool.completed", "read_file", None, None, duration=0.1, is_error=False)
    a("tool.started", "read_file", "b.py", {"path": "b.py"})
    clock.advance(22)
    await pub.drain()
    assert adapter.card().splitlines()[0] == "🔀 Opus 5.5 · 🛠 2 tool calls · ⏱ 22s"
    a("tool.completed", "read_file", None, None, duration=0.1, is_error=False)
    a("subagent.complete", status="completed", summary="Plain summary: Done.", duration_seconds=25)
    await pub.drain()
    assert adapter.summary().splitlines()[0] == "✅ Opus 5.5 · 🛠 2 tool calls · ⏱ 25s"
    await pub.aclose()


@pytest.mark.parametrize("model, expected", [
    ("claude-opus-5-5", "Opus 5.5"),
    ("claude-sonnet-4-5-20250929", "Sonnet 4.5"),
    ("anthropic/claude-opus-4.5", "Opus 4.5"),
    ("gpt-6-sol", "GPT-6 Sol"),
    ("", ""),
])
def test_pretty_model_names(model, expected):
    from agent.delegation_activity import pretty_model

    assert pretty_model(model) == expected


@pytest.mark.asyncio
async def test_done_header_counts_tools_still_running_at_the_end():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    a = _child_cb(runner)
    a("subagent.start", preview="g")
    a("tool.started", "read_file", "a.py", {"path": "a.py"})
    a("tool.completed", "read_file", None, None, duration=0.1, is_error=False)
    a("tool.started", "terminal", "sleep", {"command": "sleep 99"})
    await pub.drain()
    assert "🛠 2 tool calls" in adapter.card()
    a("subagent.complete", status="interrupted", duration_seconds=5)
    await pub.drain()
    assert "🛠 2 tool calls (1 failed)" in adapter.summary().splitlines()[0]
    await pub.aclose()


@pytest.mark.asyncio
async def test_group_header_shows_failure_not_running_icon():
    adapter = FakeTelegramAdapter()
    pub = _make_publisher(adapter)
    runner = _turn_runner(pub)
    a = _child_cb(runner, index=0, count=2, subagent_id="s1", title="One")
    b = _child_cb(runner, index=1, count=2, subagent_id="s2", title="Two")
    a("subagent.start", preview="g")
    b("subagent.start", preview="g")
    a("subagent.complete", status="completed", summary="ok", duration_seconds=1)
    b("subagent.complete", status="failed", summary="boom", duration_seconds=1)
    await pub.drain()
    assert adapter.summary().startswith("❌ 2 subagents")
    await pub.aclose()
