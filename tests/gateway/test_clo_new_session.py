"""Clover-themed /new and /reset reply (Clo headline, quote-bar info, italic tip)."""
import random
import re
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent import clover_flavor
from clover_cli import skin_engine, tips
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key
from plugins.platforms.telegram.adapter import TelegramAdapter

STOCK_HEADER = "✨ Session reset! Starting fresh."
CTX = SimpleNamespace(
    model="grok-4.7", provider="xai-oauth", base_url="", context_length=500_000,
    context_source="detected",
)


def _set_skin(monkeypatch, name):
    monkeypatch.setattr(skin_engine, "_active_skin", None)
    monkeypatch.setattr(skin_engine, "_active_skin_name", name)


@pytest.fixture(autouse=True)
def clover_active(monkeypatch):
    _set_skin(monkeypatch, "clover")
    monkeypatch.setenv("CLOVER_LANGUAGE", "en")
    monkeypatch.setattr("gateway.run._resolve_gateway_model_context", lambda *a, **k: CTX)
    monkeypatch.setattr(tips, "get_random_tip", lambda *a, **k: "Try /compress when chats get long.")
    clover_flavor.reset()
    clover_flavor.reset_new_session_picks()
    yield
    clover_flavor.reset_new_session_picks()


def _source(chat_id="c1"):
    return SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id=chat_id,
                         user_name="tester", chat_type="dm")


def _event(text="/new", chat_id="c1"):
    return MessageEvent(text=text, source=_source(chat_id), message_id="m1")


def _runner(session_db=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()
    key = build_session_key(_source())
    entry = SessionEntry(session_key=key, session_id="sess-1", created_at=datetime.now(),
                         updated_at=datetime.now(), platform=Platform.TELEGRAM, chat_type="dm")
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store.reset_session.return_value = entry
    runner.session_store._entries = {key: entry}
    runner.session_store._generate_session_key.return_value = key
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = session_db
    runner._agent_cache_lock = None
    runner._is_user_authorized = lambda _s: True
    return runner


async def _reply(text="/new", runner=None, chat_id="c1"):
    return str(await (runner or _runner())._handle_reset_command(_event(text, chat_id)))


def _telegram(msg):
    return TelegramAdapter.format_message(None, msg)


def _telegram_safe(rendered):
    """No bare MarkdownV2 special char left once escapes, quote bars and _italics_ are removed."""
    text = re.sub(r"\\.", "", rendered)
    text = re.sub(r"^> ", "", text, flags=re.M)
    text = re.sub(r"_[^_\n]+_", "", text)
    return not [c for c in "_*[]()~`>#+-=|{}.!" if c in text]


def _headline_parts(headline):
    pack = clover_flavor.CLOVER_PACK
    for mark in (pack["mark"], pack["lucky_mark"]):
        for body in pack["faces"]["new_session"]:
            prefix = mark + body + " "
            if headline.startswith(prefix):
                return mark, body, headline[len(prefix):]
    raise AssertionError(f"not a Clo headline: {headline!r}")


# --- clover skin ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clover_reply_layout():
    reply = await _reply()
    headline, info, tip = reply.split("\n\n")
    mark, _, line = _headline_parts(headline)
    assert mark == "☘️" and line in clover_flavor.CLOVER_PACK["lines"]["new_session"]
    assert info == "> 🤖 Grok 4.7 · xAI\n> 📏 500K context"
    assert tip == "🍀 tip: *Try /compress when chats get long.*"
    for stock in ("Session reset", "◆", "Provider", "xai-oauth", "grok-4.7", "✦"):
        assert stock not in reply
    assert _telegram_safe(_telegram(reply))


@pytest.mark.asyncio
async def test_new_and_reset_share_the_reply():
    assert _headline_parts((await _reply("/reset")).split("\n\n")[0])


@pytest.mark.asyncio
async def test_no_back_to_back_repeat_and_pool_variety():
    seen, last = set(), None
    runner = _runner()
    for _ in range(60):
        headline = (await _reply(runner=runner)).split("\n\n")[0]
        assert headline != last
        last = headline
        seen.add(headline)
    assert len(seen) >= 8


def test_lucky_roll_swaps_the_leaf():
    class Always(random.Random):
        def randrange(self, *a, **k):
            return 0

    headline, _, _ = clover_flavor.render_new_session(chat_key="c", model="m", rng=Always(1))
    assert headline.startswith("🍀") and not headline.startswith("☘️")
    headline, _, _ = clover_flavor.render_new_session(chat_key="c", model="m", rng=random.Random(3))
    assert headline.startswith("☘️") or headline.startswith("🍀")


def test_pool_shape_and_emoji_meanings():
    pack = clover_flavor.CLOVER_PACK
    assert len(pack["lines"]["new_session"]) >= 8 and len(pack["faces"]["new_session"]) >= 5
    # 🧠 is memory in this skin; the info bar must not reuse any tool emoji.
    tool_emojis = set(skin_engine.get_active_skin().tool_emojis.values())
    for key in ("new_model_icon", "new_context_icon", "new_local_icon"):
        assert pack["ui"][key] not in tool_emojis


@pytest.mark.asyncio
async def test_titled_session_keeps_the_title_visible():
    db = AsyncMock()
    reply = await _reply("/new my_proj v2", runner=_runner(db))
    headline = reply.split("\n\n")[0]
    assert headline.endswith("new patch: *my_proj v2*")
    assert headline.split(" new patch")[0] in {
        m + b for m in ("☘️", "🍀") for b in clover_flavor.CLOVER_PACK["faces"]["new_session"]
    }
    assert _telegram_safe(_telegram(reply))


@pytest.mark.asyncio
async def test_title_rejection_warning_survives():
    db = AsyncMock()
    db.set_session_title.side_effect = ValueError("Title 'Dup' is already in use")
    reply = await _reply("/new Dup", runner=_runner(db))
    assert "already in use" in reply and "session started untitled" in reply
    assert "new patch:" not in reply
    assert "> 🤖 Grok 4.7 · xAI" in reply
    # the warning sits right under the headline, before the info bar
    assert reply.index("⚠️") < reply.index("> 🤖")


@pytest.mark.asyncio
async def test_local_endpoint_and_context_guess(monkeypatch):
    local = SimpleNamespace(model="qwen3:8b", provider="custom", base_url="http://localhost:11434/v1",
                            context_length=8192, context_source="default")
    monkeypatch.setattr("gateway.run._resolve_gateway_model_context", lambda *a, **k: local)
    reply = await _reply()
    info = reply.split("\n\n")[1]
    assert info == (
        "> 🤖 qwen3:8b · custom\n"
        "> 📏 8K context *(default guess — set model.context_length to change)*\n"
        "> 🏠 local: localhost:11434"
    )
    assert _telegram_safe(_telegram(reply))


@pytest.mark.asyncio
async def test_detected_and_configured_context_carry_no_hint(monkeypatch):
    cfg = SimpleNamespace(**{**CTX.__dict__, "context_source": "config", "context_length": 1_048_576})
    monkeypatch.setattr("gateway.run._resolve_gateway_model_context", lambda *a, **k: cfg)
    assert "> 📏 1.0M context" in await _reply()
    assert "guess" not in await _reply()


@pytest.mark.asyncio
async def test_telegram_topic_header_is_kept(monkeypatch):
    runner = _runner()
    runner._telegram_topic_new_header = lambda source: "Started a new Clover session in this topic."
    reply = await _reply(runner=runner)
    assert reply.startswith("Started a new Clover session in this topic.")
    assert "> 🤖 Grok 4.7 · xAI" in reply and "🍀 tip:" in reply


@pytest.mark.asyncio
async def test_reply_is_ephemeral():
    from gateway.platforms.base import EphemeralReply

    result = await _runner()._handle_reset_command(_event())
    assert isinstance(result, EphemeralReply)


@pytest.mark.asyncio
async def test_failure_falls_back_to_stock(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(clover_flavor, "render_new_session", boom)
    reply = await _reply()
    assert reply.startswith(STOCK_HEADER) and "◆ Model: `grok-4.7`" in reply


# --- other skins ---------------------------------------------------------------------

STOCK_REPLY = (
    "✨ Session reset! Starting fresh.\n\n"
    "◆ Model: `grok-4.7`\n◆ Provider: xai-oauth\n◆ Context: 500K tokens (detected)\n"
    "✦ Tip: Try /compress when chats get long."
)


@pytest.mark.asyncio
@pytest.mark.parametrize("skin", ["default", "ares", "mono"])
async def test_classic_skins_are_byte_identical(monkeypatch, skin):
    _set_skin(monkeypatch, skin)
    assert await _reply() == STOCK_REPLY


@pytest.mark.asyncio
async def test_classic_titled_and_topic_replies_unchanged(monkeypatch):
    _set_skin(monkeypatch, "default")
    reply = await _reply("/new Plans", runner=_runner(AsyncMock()))
    assert reply.startswith("✨ New session started: Plans\n\n◆ Model:")
    runner = _runner()
    runner._telegram_topic_new_header = lambda source: "TOPIC"
    assert (await _reply(runner=runner)).startswith("TOPIC\n\n◆ Model:")


@pytest.mark.asyncio
async def test_non_english_keeps_stock(monkeypatch):
    monkeypatch.setenv("CLOVER_LANGUAGE", "de")
    reply = await _reply()
    assert "◆ Model: `grok-4.7`" in reply and "☘️" not in reply


CUSTOM = {
    "mark": "🔻", "lucky_mark": "", "done_mark": "🎯",
    "lines": {"new_session": ["a fresh page, sir"]},
}


@pytest.mark.asyncio
async def test_custom_pack_with_new_session_uses_its_own_lines(monkeypatch):
    pack = clover_flavor.normalize_pack(CUSTOM)
    monkeypatch.setattr(clover_flavor, "active_pack", lambda: pack)
    reply = await _reply()
    headline, info, tip = reply.split("\n\n")
    assert headline == "🔻 a fresh page, sir"
    assert info.startswith("> 🤖 Grok 4.7 · xAI")
    assert tip == "🎯 tip: *Try /compress when chats get long.*"
    assert "Clo" not in reply and "☘️" not in reply and "🍀" not in reply


@pytest.mark.asyncio
async def test_custom_pack_ui_overrides(monkeypatch):
    pack = clover_flavor.normalize_pack({
        **CUSTOM, "ui": {"new_model_icon": "🔧", "new_context_icon": "📐", "new_titled": "case: *{title}*",
                         "new_tip": "🎯 hint:"},
    })
    monkeypatch.setattr(clover_flavor, "active_pack", lambda: pack)
    reply = await _reply("/new ledger", runner=_runner(AsyncMock()))
    assert reply.startswith("🔻 case: *ledger*")
    assert "> 🔧 Grok 4.7 · xAI\n> 📐 500K context" in reply and "🎯 hint: *" in reply


@pytest.mark.asyncio
async def test_custom_pack_without_new_session_keeps_stock(monkeypatch):
    pack = clover_flavor.normalize_pack({"mark": "🔻", "lines": {"busy": ["occupied"]}})
    monkeypatch.setattr(clover_flavor, "active_pack", lambda: pack)
    assert await _reply() == STOCK_REPLY


@pytest.mark.parametrize("name", ["butler", "minimal", "hype"])
def test_builtin_starter_looks_define_new_session(name):
    pack = clover_flavor.pack_for_skin(skin_engine.set_active_skin(name))
    out = clover_flavor.render_new_session(chat_key="s", model="grok-4.7", provider="xai", context="500K",
                                           tip="x", pack=pack, rng=random.Random(1))
    assert out is not None and out[1].startswith("> ") and "Clo" not in out[0]


# --- pretty names & Telegram safety --------------------------------------------------

@pytest.mark.parametrize("model,provider,pretty_m,pretty_p", [
    ("grok-4.7", "xai-oauth", "Grok 4.7", "xAI"),
    ("grok-4-fast-reasoning", "xai", "Grok 4 Fast Reasoning", "xAI"),
    ("claude-opus-5-5", "anthropic", "Opus 5.5", "Anthropic"),
    ("anthropic/claude-sonnet-5-5", "openrouter", "Sonnet 5.5", "OpenRouter"),
    ("gpt-6-sol", "openai-codex", "GPT-6 Sol", "OpenAI"),
    ("gemini-2.5-pro", "gemini", "Gemini 2.5 Pro", "Google"),
    ("deepseek-v3.2", "deepseek", "DeepSeek V3.2", "DeepSeek"),
    ("qwen3:8b", "ollama", "qwen3:8b", "custom"),
    ("my_model-v1.gguf", "acme-cloud", "my_model-v1.gguf", "acme-cloud"),
])
def test_pretty_names(model, provider, pretty_m, pretty_p):
    assert clover_flavor.pretty_model_name(model) == pretty_m
    assert clover_flavor.pretty_provider_name(provider) == pretty_p


@pytest.mark.parametrize("model", ["grok-4.7", "my_model-v1.gguf", "qwen3:8b", "a.b-c_d", "gpt-6-sol"])
def test_every_variant_survives_telegram_markdownv2(model):
    for title in ("", "my_proj *v2* (a-b)", "Plan: [x] #1!"):
        for tip in tips.chat_tips():
            headline, info, tip_line = clover_flavor.render_new_session(
                chat_key="t", title=title, model=model, provider="acme-cloud", context="8192",
                context_guess=True, local_endpoint="localhost:11434", tip=tip,
            )
            assert _telegram_safe(_telegram("\n\n".join([headline, info, tip_line])))


# --- tips ----------------------------------------------------------------------------

def test_chat_tips_drop_dashboard_and_cli_only_tips():
    chat = tips.chat_tips()
    assert 40 <= len(chat) < len(tips.TIPS) // 2
    dash = next(t for t in tips.TIPS if "dashboard-themes" in t)
    assert dash in tips.TIPS and dash not in chat
    for tip in chat:
        assert not re.search(r"dashboard|Ctrl\+|Alt\+|config\.yaml|\bclover \w|~/|TUI|status ?bar", tip), tip


def test_chat_slash_tips_name_commands_that_exist_in_chat():
    from clover_cli.commands import resolve_command

    for tip in tips.chat_tips():
        if tip.startswith("/"):
            cmd = resolve_command(tip.split()[0])
            assert cmd is not None and not cmd.cli_only, tip


def test_get_random_tip_platform_filter_and_cli_default(monkeypatch):
    monkeypatch.undo()  # the autouse fixture patches get_random_tip
    from clover_cli.tips import get_random_tip

    chat = set(tips.chat_tips())
    assert all(get_random_tip(platform="telegram") in chat for _ in range(300))
    assert all(get_random_tip(platform="discord") in chat for _ in range(50))
    cli = {get_random_tip() for _ in range(3000)} | {get_random_tip(platform="cli") for _ in range(3000)}
    assert cli - chat, "the CLI pool must keep the terminal/dashboard tips"
    assert set(tips.TIPS) >= cli
