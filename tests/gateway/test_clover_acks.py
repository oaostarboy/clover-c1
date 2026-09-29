"""Clo's busy / stop acks under the clover skin; other skins stay byte-for-byte."""

import random
import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.SUPERGROUP, _ct.GROUP, _ct.PRIVATE = "supergroup", "group", "private"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from agent import clover_flavor  # noqa: E402
from clover_cli import skin_engine  # noqa: E402
from gateway import clover_acks  # noqa: E402
from gateway.platforms.base import (  # noqa: E402
    MessageEvent,
    MessageType,
    SessionSource,
    build_session_key,
)

POOLS = clover_acks.ACK_POOLS
BUSY_KINDS = ("steer", "redirect", "interrupt", "queued", "stop")
FUNCTIONAL = {
    "steer": "Your message arrives after the next tool call.",
    "redirect": "I'll adjust using your correction.",
    "interrupt": "I'll respond to your message shortly.",
    "queued": "I'll respond once the current task finishes.",
    "stop": "You can continue this session.",
}
SUBAGENT_SENTENCE = (
    "Your message is queued for when it finishes (use /stop to cancel everything)."
)


@pytest.fixture(autouse=True)
def clover_active(monkeypatch):
    monkeypatch.setattr(skin_engine, "_active_skin", None)
    monkeypatch.setattr(skin_engine, "_active_skin_name", "clover")
    monkeypatch.setenv("CLOVER_LANGUAGE", "en")
    clover_flavor.reset()
    clover_acks.reset()
    yield
    clover_flavor.reset()
    clover_acks.reset()


def _split(text, kind):
    """(face, line) if *text* starts with a pool face + contains a pool line."""
    faces, lines = POOLS[kind]
    face = next(f for f in faces if text.startswith(f + " "))
    line = next(x for x in lines if text[len(face) + 1:].startswith(x))
    return face, line


@pytest.mark.parametrize("kind", BUSY_KINDS)
def test_every_ack_uses_pool_face_line_and_functional_sentence(kind):
    rng = random.Random(1)
    for _ in range(40):
        text = clover_acks.build_ack(kind, "chat", FUNCTIONAL[kind], " (2 min elapsed)", rng=rng)
        face, line = _split(text, kind)
        assert face.startswith("☘️")
        assert text == f"{face} {line} (2 min elapsed). {FUNCTIONAL[kind]}"


@pytest.mark.parametrize("kind", BUSY_KINDS)
def test_never_repeats_the_same_pair_back_to_back(kind):
    rng = random.Random(7)
    picks = [
        _split(clover_acks.build_ack(kind, "chat-a", FUNCTIONAL[kind], rng=rng), kind)
        for _ in range(20)
    ]
    assert all(a != b for a, b in zip(picks, picks[1:]))
    assert len(set(picks)) > 1


def test_no_repeat_even_with_a_stuck_rng():
    class Stuck:
        def choice(self, seq):
            return seq[0]

    picks = [
        _split(clover_acks.build_ack("steer", "c", "x", rng=Stuck()), "steer")
        for _ in range(6)
    ]
    assert all(a != b for a, b in zip(picks, picks[1:]))


def test_last_pick_is_per_chat():
    class Stuck:
        def choice(self, seq):
            return seq[0]

    a = clover_acks.build_ack("steer", "chat-1", "x", rng=Stuck())
    b = clover_acks.build_ack("steer", "chat-2", "x", rng=Stuck())
    assert a == b  # a different chat has its own memory


def test_lucky_turn_swaps_leaf_prefix():
    class Lucky:
        def randrange(self, n):
            return 0

        def choice(self, seq):
            return seq[0]

    clover_flavor.begin_turn(rng=Lucky())
    text = clover_acks.build_ack("interrupt", "c", FUNCTIONAL["interrupt"], rng=Lucky())
    assert text.startswith("🍀(°ロ°)! ")
    assert "☘️" not in text
    clover_flavor.begin_turn(rng=random.Random(3), monotonic=lambda: 0)
    clover_flavor.current_turn().lucky = False
    assert clover_acks.build_ack("interrupt", "d", "x", rng=Lucky()).startswith("☘️")


def test_stop_pool_has_no_sleepy_faces():
    for face in POOLS["stop"][0]:
        assert "zzZ" not in face and "－_－" not in face


@pytest.mark.parametrize(
    "stock,rest",
    [
        ("⚡ Stopped. You can continue this session.", "You can continue this session."),
        ("⚡ Stopped. The agent hadn't started yet — you can continue this session.",
         "The agent hadn't started yet — you can continue this session."),
        ("⚡ Force-stopped. The agent was still starting — session unlocked.",
         "The agent was still starting — session unlocked."),
    ],
)
def test_stop_acks(stock, rest):
    text = clover_acks.stop_ack(stock, "chat", rng=random.Random(2))
    face, line = _split(text, "stop")
    assert text == f"{face} {line}. {rest}"


@pytest.mark.parametrize("name", ["default", "ares"])
def test_other_skins_keep_stock_text_byte_for_byte(name):
    skin_engine.set_active_skin(name)
    stock = "⚡ Stopped. You can continue this session."
    assert clover_acks.active() is False
    assert clover_acks.stop_ack(stock, "chat") == stock


def test_non_english_session_keeps_stock_text(monkeypatch):
    monkeypatch.setenv("CLOVER_LANGUAGE", "de")
    stock = "⚡ Gestoppt. Du kannst diese Sitzung fortsetzen."
    assert clover_acks.stop_ack(stock, "chat") == stock


# --- through the real busy-message handler ------------------------------------------


def _runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner._busy_text_mode = "interrupt"
    runner._queued_events = {}
    runner.adapters = {}
    runner.config = MagicMock()
    runner.config.group_sessions_per_user = True
    runner.config.thread_sessions_per_user = False
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda _source: True
    return runner


async def _busy_ack(monkeypatch, mode):
    import gateway.run as gr

    monkeypatch.delenv("CLOVER_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)
    monkeypatch.setattr(gr, "_load_gateway_config", lambda: {})
    runner = _runner()
    runner._busy_input_mode = mode
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter._text_debounce = {}
    adapter._busy_text_debounce_seconds = 0.6
    source = SessionSource(
        platform=MagicMock(value="telegram"), chat_id="123", chat_type="private", user_id="u1"
    )
    event = MessageEvent(text="hey", message_type=MessageType.TEXT, source=source, message_id="m1")
    sk = build_session_key(source)
    agent = MagicMock()
    agent.steer = MagicMock(return_value=True)
    agent.get_activity_summary.return_value = {
        "api_call_count": 3, "max_iterations": 60, "current_tool": "terminal",
        "seconds_since_activity": 1.0,
    }
    runner._running_agents[sk] = agent
    runner._running_agents_ts[sk] = time.time() - 600
    runner.adapters[source.platform] = adapter
    await runner._handle_active_session_busy_message(event, sk)
    call = adapter._send_with_retry.call_args
    return call.kwargs.get("content") or call[1].get("content", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,kind",
    [("steer", "steer"), ("interrupt", "interrupt"), ("queue", "queued")],
)
async def test_busy_handler_emits_clover_acks(monkeypatch, mode, kind):
    content = await _busy_ack(monkeypatch, mode)
    face, line = _split(content, kind)
    head = content.split("\n\n", 1)[0]  # drop the one-time onboarding tip
    assert head.startswith(f"{face} {line}")
    assert head.endswith(FUNCTIONAL[kind])


@pytest.mark.asyncio
async def test_busy_handler_default_skin_unchanged(monkeypatch):
    skin_engine.set_active_skin("default")
    content = await _busy_ack(monkeypatch, "interrupt")
    head = content.split("\n\n", 1)[0]
    assert head.startswith("⚡ Interrupting current task")
    assert head.endswith("I'll respond to your message shortly.")
    assert "☘️" not in content
