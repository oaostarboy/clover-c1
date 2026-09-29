"""Bot-to-bot loop guard: ``_is_user_authorized`` peeks, ``_admit_bot_message`` counts.

Ported from Hermes's tests/gateway/test_bot_loop_guard.py; the BotLoopGuard
mechanics, ``_is_user_authorized``/``_admit_bot_message`` method names and
semantics are identical, so the core scenarios translate directly. The
ingress/busy-path integration tests are rewritten against Clover's own
``_handle_message`` / ``_handle_active_session_busy_message`` (Clover has no
``_hm_admit_event`` split) and add the explicit non-goal check the porting
brief calls for: `clover peer dm` / api_server traffic must never be metered.

The scenarios set ``TELEGRAM_GROUP_ALLOWED_CHATS``: that allowlist admits a
bot before the ``ALLOW_BOTS`` block runs, which is the configuration that
produced the incident.
"""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.bot_loop_guard import BotLoopGuard, BotLoopGuardSettings, load_settings, settings_from_config
from gateway.session import Platform, SessionSource

GROUP_CHAT = "-1001234567890"
OTHER_GROUP = "-1009876543210"
BOT_A = "111111111"
BOT_B = "222222222"
BOT_C = "333333333"
HUMAN = "100200300"


@pytest.fixture(autouse=True)
def _isolate_telegram_env(monkeypatch):
    for var in ("TELEGRAM_ALLOW_BOTS", "TELEGRAM_ALLOWED_USERS", "TELEGRAM_ALLOW_ALL_USERS", "TELEGRAM_GROUP_ALLOWED_USERS",
                "TELEGRAM_GROUP_ALLOWED_CHATS", "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS", "DISCORD_ALLOW_BOTS"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def clock():
    state = {"t": 1_000_000.0}
    return SimpleNamespace(now=lambda: state["t"], advance=lambda secs: state.__setitem__("t", state["t"] + secs))


@pytest.fixture
def settings():
    """Mutable holder so a test can flip settings on a live guard."""
    return {"value": BotLoopGuardSettings(max_events=20, window_seconds=60, cooldown_seconds=60)}


@pytest.fixture
def runner(clock, settings):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: False)
    runner._bot_loop_guard = BotLoopGuard(settings=lambda: settings["value"], clock=clock.now)
    return runner


def _bot(user_id: str, chat_id: str = GROUP_CHAT, chat_type: str = "group") -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type=chat_type, user_id=user_id,
                         user_name=f"Bot{user_id}", is_bot=True)


def _human(chat_id: str = GROUP_CHAT, chat_type: str = "group") -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type=chat_type, user_id=HUMAN,
                         user_name="Alice", is_bot=False)


def _incident_config(monkeypatch):
    """ALLOW_BOTS on, a human allowlist, and the group-chat allowlist that short-circuits authz."""
    monkeypatch.setenv("TELEGRAM_ALLOW_BOTS", "mentions")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", HUMAN)
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", f"{GROUP_CHAT},{OTHER_GROUP}")


def _inbound(runner, source: SessionSource) -> bool:
    """What the cold inbound path does per message: the verdict, then one count for an admitted bot."""
    return runner._is_user_authorized(source) and runner._admit_bot_message(source)


def _ping_pong(runner, turns: int, chat_id: str = GROUP_CHAT) -> list:
    return [_inbound(runner, _bot(BOT_A if t % 2 == 0 else BOT_B, chat_id)) for t in range(turns)]


# --- authz integration -----------------------------------------------------


def test_one_inbound_is_counted_once_however_often_the_verdict_is_asked(monkeypatch, runner):
    """The Telegram adapter, the ingress gate and the busy path all ask the verdict for one message."""
    _incident_config(monkeypatch)
    for _ in range(20):
        bot = _bot(BOT_A)
        assert [runner._is_user_authorized(bot) for _ in range(3)] == [True, True, True]
        assert runner._admit_bot_message(bot) is True
    assert runner._is_user_authorized(_bot(BOT_B)) is True
    assert runner._admit_bot_message(_bot(BOT_B)) is False
    assert runner._is_user_authorized(_bot(BOT_B)) is False


def test_admitted_bot_traffic_is_cut_at_the_budget(monkeypatch, runner):
    _incident_config(monkeypatch)
    verdicts = [_inbound(runner, _bot(BOT_A if i % 2 == 0 else BOT_B)) for i in range(40)]

    assert verdicts[:20] == [True] * 20
    assert not any(verdicts[20:])


def test_humans_are_never_metered_and_stay_authorized_during_cooldown(monkeypatch, runner, clock):
    _incident_config(monkeypatch)
    for _ in range(50):
        assert _inbound(runner, _human(chat_id="123", chat_type="dm")) is True
    assert runner._bot_loop_guard.tracked_conversations == 0

    _ping_pong(runner, 25)
    assert runner._is_user_authorized(_bot(BOT_A)) is False
    assert runner._is_user_authorized(_human()) is True
    clock.advance(5)
    assert runner._is_user_authorized(_human()) is True


def test_budget_is_scoped_by_chat_and_platform(monkeypatch, runner):
    _incident_config(monkeypatch)
    _ping_pong(runner, 25)
    assert runner._is_user_authorized(_bot(BOT_A)) is False

    assert _inbound(runner, _bot(BOT_A, chat_id=OTHER_GROUP)) is True
    assert _inbound(runner, _bot(BOT_B, chat_id=OTHER_GROUP)) is True
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "all")
    discord_bot = SessionSource(platform=Platform.DISCORD, chat_id=GROUP_CHAT, chat_type="group", user_id=BOT_A, is_bot=True)
    assert _inbound(runner, discord_bot) is True


def test_cooldown_expiry_readmits_with_a_fresh_budget(monkeypatch, runner, clock):
    _incident_config(monkeypatch)
    _ping_pong(runner, 21)
    assert runner._is_user_authorized(_bot(BOT_A)) is False

    clock.advance(61)
    assert all(_ping_pong(runner, 20))
    assert _inbound(runner, _bot(BOT_A)) is False


def test_disabled_via_config_admits_everything(monkeypatch, runner, settings):
    _incident_config(monkeypatch)
    settings["value"] = settings_from_config({"gateway": {"bot_loop_guard": {"enabled": False}}})

    assert all(_ping_pong(runner, 40))


def test_rejected_bot_messages_do_not_consume_budget(monkeypatch, runner):
    """Only admitted bot traffic counts, so an unauthorized bot cannot silence an authorized one."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", HUMAN)

    for _ in range(30):
        assert _inbound(runner, _bot(BOT_A, chat_id="123", chat_type="dm")) is False

    assert runner._bot_loop_guard.tracked_conversations == 0


# --- non-goal: api_server / clover peer dm traffic is never metered --------


def test_api_server_and_peer_dm_traffic_is_never_metered(monkeypatch, runner, settings):
    """`clover peer dm` and api_server sessions are never bot-authored (`is_bot`
    defaults False and neither surface sets it), so they must sail through the
    guard untouched regardless of how small the budget is."""
    settings["value"] = BotLoopGuardSettings(max_events=1, window_seconds=60, cooldown_seconds=60)

    api_source = SessionSource(
        platform=Platform.API_SERVER, chat_id="peer-dm-1", chat_type="dm", user_id="peer-client",
    )
    for _ in range(50):
        assert runner._admit_bot_message(api_source) is True
    assert runner._bot_loop_guard.tracked_conversations == 0


@pytest.mark.asyncio
async def test_handle_message_drops_a_tripped_bot_conversation_before_session_setup(monkeypatch):
    """Integration check against Clover's real cold inbound path (_handle_message):
    a bot conversation already cooling down is dropped by the wired-in guard
    before the message ever reaches session creation / agent dispatch."""
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.run import GatewayRunner

    _incident_config(monkeypatch)
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    guard = BotLoopGuard(settings=lambda: BotLoopGuardSettings(max_events=1, window_seconds=60, cooldown_seconds=60))
    runner._bot_loop_guard = guard
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: False)
    runner._profile_name_for_source = lambda source: None

    calls = []

    def _get_or_create_session(*a, **kw):
        calls.append(True)
        raise RuntimeError("stop before the rest of the agent pipeline")

    runner.session_store = SimpleNamespace(get_or_create_session=_get_or_create_session)

    def _bot_event(uid: str) -> MessageEvent:
        return MessageEvent(text="ping", message_type=MessageType.TEXT, message_id=uid, source=_bot(uid))

    # First bot message is admitted (consumes the max_events=1 budget) and
    # reaches session creation, where the injected stub bails out.
    with pytest.raises(RuntimeError):
        await runner._handle_message(_bot_event(BOT_A))
    assert calls == [True]

    # A second message from the SAME conversation is over budget: the new
    # gate must drop it before it ever touches session_store.
    second = await runner._handle_message(_bot_event(BOT_B))
    assert second is None
    assert calls == [True]  # unchanged — session_store was never called again
    assert guard.blocked(runner._bot_loop_guard_conversation(_bot(BOT_A))) is True


# --- BotLoopGuard unit ------------------------------------------------------


def test_concurrent_admits_respect_the_budget(clock):
    guard = BotLoopGuard(settings=lambda: BotLoopGuardSettings(max_events=20, window_seconds=60, cooldown_seconds=60), clock=clock.now)

    with ThreadPoolExecutor(max_workers=8) as pool:
        allowed = list(pool.map(lambda _: guard.admit("c")[0], range(80)))

    assert sum(allowed) == 20


# --- settings ---------------------------------------------------------------


def test_load_settings_reads_config_yaml(monkeypatch):
    import clover_cli.config as clover_config

    monkeypatch.setattr(clover_config, "load_config_readonly", lambda: {"gateway": {"bot_loop_guard": {"max_events": 3}}})
    assert load_settings().max_events == 3

    def boom():
        raise RuntimeError("no config")

    monkeypatch.setattr(clover_config, "load_config_readonly", boom)
    assert load_settings() == BotLoopGuardSettings()
