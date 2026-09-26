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
        self.cards[(chat_id, status_key)] = content
        return SendResult(success=True, message_id="m1")

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
    assert "claude-opus-5-5" in card and "gpt-5.5" in card and "openrouter" in card
    assert "read_file" in card and "web_search" in card
    # Clearly labelled as child activity, not the parent's own thoughts/tools.
    assert "subagent" in card.lower()
    # Stable per-child identity shown consistently.
    assert "#1" in card and "#2" in card
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
    lines = card.splitlines()
    a_idx = next(i for i, l in enumerate(lines) if "Audit gateway auth" in l)
    b_idx = next(i for i, l in enumerate(lines) if "Draft changelog" in l)
    note_idx = next(i for i, l in enumerate(lines) if "Checking token scoping" in l)
    assert a_idx < note_idx < b_idx or b_idx < a_idx < note_idx
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
    assert "failed" in card.lower()
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
    card = adapter.card()
    assert SECRET not in card
    assert "Fixed the scope leak" in card
    assert "authz_mixin.py" in card
    # A child reporting done is NOT parent approval.
    assert "parent review" in card.lower()
    assert "approved" not in card.lower()
    # Failures and cancellations get one concise alert each; success does not.
    alerts = [s["content"] for s in adapter.sends]
    assert len(alerts) == 2
    assert any("Port tests" in m and "failed" in m.lower() for m in alerts)
    assert any("Update docs" in m and "cancel" in m.lower() for m in alerts)
    assert pub.tracker.group_finished("deleg_aaaa0001")
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
    assert "completed" in adapter.card() or "done" in adapter.card().lower()
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
    assert adapter.sends == []
    assert "rm -rf build" not in adapter.card()
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
    assert "cancel" in adapter.card(key="delegation:deleg_live").lower()
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
