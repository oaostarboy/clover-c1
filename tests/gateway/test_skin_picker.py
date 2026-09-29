"""/skin picker, starter message packs, and the customize-look skill."""

import random
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

import gateway.run as gateway_run
from agent import clover_flavor
from clover_cli import skin_engine
from gateway import clover_acks
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter

STOCK_RESTART = "⚠️ Gateway restarting — Your current task will be interrupted."
TAIL = "Your task is paused; message me after and I'll resume."


@pytest.fixture(autouse=True)
def _clean_skin_state(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOVER_LANGUAGE", "en")
    monkeypatch.setattr(skin_engine, "_active_skin", None)
    monkeypatch.setattr(skin_engine, "_active_skin_name", "clover")
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    clover_acks.reset()
    yield
    clover_acks.reset()


def _event(text="/skin"):
    source = SessionSource(platform=Platform.TELEGRAM, user_id="1", chat_id="2", user_name="u")
    return MessageEvent(text=text, source=source)


class _PickerAdapter:
    def __init__(self):
        self.calls = []

    async def send_skin_picker(self, **kwargs):
        self.calls.append(kwargs)
        return SendResult(success=True, message_id="m1")


def _runner(adapter=None):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._adapter_for_source = lambda source: adapter
    runner._thread_metadata_for_source = lambda source, anchor=None: {}
    runner._reply_anchor_for_event = lambda event: None
    return runner


# --- starter packs -------------------------------------------------------------------

@pytest.mark.parametrize("name", ["butler", "minimal", "hype"])
def test_starter_pack_loads_and_reskins(name):
    skin = skin_engine.set_active_skin(name)
    pack = clover_flavor.pack_for_skin(skin)
    assert pack is not None
    # every kind the schema supports, 3+ lines each
    assert set(pack["lines"]) == set(clover_flavor.PACK_KINDS)
    assert all(len(v) >= 3 for v in pack["lines"].values())

    rng = random.Random(1)
    restarting = clover_acks.notice("restarting", "c", STOCK_RESTART, TAIL, rng=rng)
    back = clover_acks.notice("back_online", "c", "stock online", "", rng=rng)
    stop = clover_acks.stop_ack("⚡ Stopped. You can continue.", "c", rng=rng)
    assert restarting != STOCK_RESTART and restarting.endswith(f". {TAIL}")
    assert back != "stock online"
    assert stop != "⚡ Stopped. You can continue." and stop.endswith("You can continue.")
    assert any(line in restarting for line in pack["lines"]["restarting"])
    assert any(line in back for line in pack["lines"]["back_online"])


def test_starter_pack_marks():
    assert skin_engine.load_skin("butler").messages["mark"] == "🔻"
    assert skin_engine.load_skin("hype").messages["mark"] == "⚡"
    minimal = skin_engine.load_skin("minimal").messages
    assert minimal["mark"] == "" and not minimal.get("faces")


# --- /skin ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skin_lists_skins_and_marks_current_without_buttons():
    out = await _runner(object())._handle_skin_command(_event("/skin"))
    assert "current: clover" in out
    for name in ("clover", "butler", "minimal", "hype"):
        assert name in out
    assert re.search(r"\d+\. clover ●", out)
    assert "butler ●" not in out


@pytest.mark.asyncio
async def test_skin_sends_picker_when_platform_has_buttons():
    adapter = _PickerAdapter()
    out = await _runner(adapter)._handle_skin_command(_event("/skin"))
    assert out is None
    call = adapter.calls[0]
    assert call["current"] == "clover"
    assert {"butler", "minimal", "hype"} <= {s["name"] for s in call["skins"]}
    assert "restarting:" in call["preview"]("butler")


@pytest.mark.asyncio
async def test_apply_persists_and_switches(tmp_path):
    out = await _runner()._handle_skin_command(_event("/skin butler"))
    assert "butler" in out
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["display"]["skin"] == "butler"
    assert skin_engine.get_active_skin_name() == "butler"
    assert clover_flavor.active_pack()["mark"] == "🔻"


@pytest.mark.asyncio
async def test_apply_by_number_and_unknown_is_friendly(tmp_path):
    runner = _runner()
    names = [s["name"] for s in skin_engine.list_skins()]
    await runner._handle_skin_command(_event("/skin 2"))
    assert skin_engine.get_active_skin_name() == names[1]

    out = await runner._handle_skin_command(_event("/skin nonsense"))
    assert "don't know a skin called 'nonsense'" in out and "/skin" in out
    assert skin_engine.get_active_skin_name() == names[1]  # unchanged


def test_preview_has_the_five_lines():
    from clover_cli.skin_cmd import skin_preview

    rows = skin_preview("minimal").splitlines()
    assert rows[0] == "Preview: minimal"
    assert [r.split(":")[0] for r in rows[1:]] == [
        "• restarting", "• back online", "• busy", "• stop", "• learned something",
    ]


# --- Telegram buttons ----------------------------------------------------------------

def _tg():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="t"))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    adapter._is_callback_user_authorized = lambda *a, **k: True
    return adapter


def _query():
    q = AsyncMock()
    q.message = MagicMock()
    q.message.chat_id = 2
    q.from_user = MagicMock()
    return q


@pytest.mark.asyncio
async def test_telegram_tap_previews_then_apply_applies(monkeypatch):
    from plugins.platforms.telegram import adapter as tg_module

    monkeypatch.setattr(tg_module, "InlineKeyboardButton",
                        lambda text, callback_data=None: SimpleNamespace(text=text, callback_data=callback_data))
    monkeypatch.setattr(tg_module, "InlineKeyboardMarkup",
                        lambda rows: SimpleNamespace(inline_keyboard=rows))
    adapter = _tg()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=7))
    on_apply = AsyncMock(return_value="Skin set to hype.")
    res = await adapter.send_skin_picker(
        chat_id="2", skins=[{"name": "clover"}, {"name": "hype"}], current="clover",
        session_key="s", preview=lambda n: f"Preview: {n}", on_apply=on_apply,
    )
    assert res.success and "2" in adapter._skin_picker_state

    q = _query()
    await adapter._handle_skin_picker_callback(q, "sk:1", "2")
    kw = q.edit_message_text.call_args[1]
    assert "Preview: hype" in kw["text"]
    buttons = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row]
    assert buttons == ["sk:a:1", "sk:x"]
    on_apply.assert_not_called()

    q = _query()
    await adapter._handle_skin_picker_callback(q, "sk:a:1", "2")
    on_apply.assert_awaited_once_with("2", "hype")
    assert "2" not in adapter._skin_picker_state


@pytest.mark.asyncio
async def test_telegram_cancel_and_expiry():
    adapter = _tg()
    q = _query()
    await adapter._handle_skin_picker_callback(q, "sk:0", "2")
    assert "expired" in q.answer.call_args[1]["text"]

    adapter._skin_picker_state["2"] = {"names": ["clover"], "preview": str, "on_apply": AsyncMock()}
    q = _query()
    await adapter._handle_skin_picker_callback(q, "sk:x", "2")
    assert "2" not in adapter._skin_picker_state


# --- customize-look skill ------------------------------------------------------------

def test_customize_look_skill_file_is_valid():
    path = Path(__file__).resolve().parents[2] / "skills" / "productivity" / "customize-look" / "SKILL.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    front = yaml.safe_load(text.split("---")[1])
    assert front["name"] == "customize-look"
    assert front["description"].endswith(".") and len(front["description"]) <= 60
    for key in ("messages:", "tool_emojis", "spinner", "branding", "clover skin use"):
        assert key in text
