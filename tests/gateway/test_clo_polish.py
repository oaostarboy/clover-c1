"""Clo-style polish: chat statuses, model switch, first hello, cron header, pickers.

Every surface reads in Clo's voice under the clover skin; any other skin keeps
the stock text byte-for-byte.
"""

import os
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent import clover_flavor
from clover_cli import skin_engine
from gateway import clover_acks
from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter

OTHER_SKINS = ["default", "ares", "mono", "slate"]


def _set_skin(monkeypatch, name):
    monkeypatch.setattr(skin_engine, "_active_skin", None)
    monkeypatch.setattr(skin_engine, "_active_skin_name", name)


@pytest.fixture(autouse=True)
def clover_active(monkeypatch):
    _set_skin(monkeypatch, "clover")
    monkeypatch.setenv("CLOVER_LANGUAGE", "en")
    clover_flavor.reset()
    getattr(clover_flavor, "reset_status_lines", lambda: None)()
    clover_acks.reset()
    yield
    clover_flavor.reset()
    getattr(clover_flavor, "reset_status_lines", lambda: None)()
    clover_acks.reset()


def _faces(kind):
    return clover_flavor.STATUS_POOLS[kind][0]


def _lines(kind):
    return clover_flavor.STATUS_POOLS[kind][1]


# --- 1. retry / busy / rate-limit / error statuses ---------------------------------

@pytest.mark.parametrize("kind", ["busy", "rate_limited", "error"])
def test_status_line_clo_shape_keeps_tail(kind):
    out = clover_flavor.status_line(kind, "chat", "stock", "trying again in 12s (attempt 1/3)",
                                    rng=random.Random(1))
    assert out != "stock"
    face, rest = out.split(" ", 1)
    assert face in _faces(kind)
    assert any(rest.startswith(line + ". ") for line in _lines(kind))
    assert out.endswith("trying again in 12s (attempt 1/3)")


@pytest.mark.parametrize("kind", ["busy", "rate_limited", "error", "model_substitute"])
def test_status_line_never_repeats_back_to_back(kind):
    rng = random.Random(0)
    prev = None
    for _ in range(40):
        out = clover_flavor.status_line(kind, "chat", "s", "t", rng=rng,
                                        requested="a", provider="b")
        assert out != prev
        prev = out


@pytest.mark.parametrize("skin", OTHER_SKINS)
@pytest.mark.parametrize("kind", ["busy", "rate_limited", "error", "model_substitute"])
def test_status_line_other_skins_stock(monkeypatch, skin, kind):
    _set_skin(monkeypatch, skin)
    assert clover_flavor.status_line(kind, "c", "STOCK ⏳ text", "tail") == "STOCK ⏳ text"


def test_status_line_lucky_turn_uses_four_leaf():
    clover_flavor.begin_turn(rng=SimpleNamespace(randrange=lambda n: 0, choice=random.choice,
                                                 random=random.random))
    assert clover_flavor.current_turn().lucky
    out = clover_flavor.status_line("busy", "c", "s", "t")
    assert out.startswith("🍀")


def _agent():
    import run_agent
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                    quiet_mode=True, skip_context_files=True, skip_memory=True)
    a.client = MagicMock()
    a._cached_system_prompt = "You are helpful."
    a._use_prompt_caching = False
    a.save_trajectories = False
    return a


def _rate_limit_error():
    err = Exception("rate limit exceeded, please slow down")
    err.status_code = 429
    return err


def _run_failing_turn(monkeypatch):
    import time as _time
    import run_agent

    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(run_agent, "jittered_backoff", lambda *a, **k: 0.0)
    agent = _agent()
    agent._api_max_retries = 2
    events = []
    agent.status_callback = lambda ev, msg: events.append(msg)
    agent.client.chat.completions.create.side_effect = _rate_limit_error()
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        agent.run_conversation("hello")
    return events


def test_real_agent_rate_limit_statuses_are_clo(monkeypatch):
    events = _run_failing_turn(monkeypatch)
    assert any("still limited after 2 retries" in m and m.startswith("☘️(") for m in events), events
    assert not any("Rate limited after" in m for m in events)
    assert any("trying again in" in m and "(attempt" in m for m in events), events
    assert not any("Waiting" in m for m in events)


@pytest.mark.parametrize("skin", OTHER_SKINS)
def test_real_agent_rate_limit_statuses_stock_on_other_skins(monkeypatch, skin):
    _set_skin(monkeypatch, skin)
    events = _run_failing_turn(monkeypatch)
    assert any(m.startswith("❌ Rate limited after 2 retries — ") for m in events), events
    assert any(m.startswith("⏱️ Rate limited. Waiting ") for m in events), events
    assert not any("☘️" in m for m in events)


# --- 2. model substitute notice ------------------------------------------------------

def test_model_substitute_clo_notice():
    out = clover_flavor.status_line(
        "model_substitute", "c", "stock", "so I used *claude-sonnet-5-5* instead.",
        rng=random.Random(2), joiner=", ", requested="gpt-9", provider="openai",
    )
    assert out.startswith(tuple(_faces("model_substitute")))
    assert ("couldn't find *gpt-9*" in out) or ("no *gpt-9* on openai" in out)
    assert out.endswith(", so I used *claude-sonnet-5-5* instead.")


def test_oneshot_stderr_text_unchanged():
    import inspect
    from clover_cli import oneshot

    src = inspect.getsource(oneshot)
    assert "doesn't exist \"\n            f\"on {result.get('requested_provider')}, so I used" in src


# --- 3. first hello ------------------------------------------------------------------

def test_first_hello_once_per_dm_chat(tmp_path):
    rng = random.Random(3)
    first = clover_acks.with_first_hello("the answer", "telegram", "dm", "42", tmp_path, rng)
    assert first.endswith("\n\nthe answer")
    assert first.split("\n\n")[0] in clover_acks.HELLO_LINES
    assert "I'm Clo" in first
    assert clover_acks.with_first_hello("again", "telegram", "dm", "42", tmp_path, rng) == "again"
    other = clover_acks.with_first_hello("hi", "telegram", "dm", "43", tmp_path, rng)
    assert "I'm Clo" in other


@pytest.mark.parametrize("platform,chat_type", [
    ("telegram", "group"), ("telegram", "channel"), ("api_server", "dm"),
    ("webhook", "dm"), ("local", "dm"),
])
def test_first_hello_never_in_groups_or_machine_surfaces(tmp_path, platform, chat_type):
    assert clover_acks.with_first_hello("x", platform, chat_type, "1", tmp_path) == "x"
    assert not (tmp_path / "clo_hello_seen.json").exists()


@pytest.mark.parametrize("skin", OTHER_SKINS)
def test_first_hello_stock_on_other_skins(monkeypatch, tmp_path, skin):
    _set_skin(monkeypatch, skin)
    assert clover_acks.with_first_hello("x", "telegram", "dm", "1", tmp_path) == "x"


# --- 4. cron header ------------------------------------------------------------------

def _deliver(job_name, failed=False):
    from cron.scheduler import _deliver_result
    from gateway.config import Platform

    pconfig = MagicMock()
    pconfig.enabled = True
    cfg = MagicMock()
    cfg.platforms = {Platform.TELEGRAM: pconfig}
    with patch("gateway.config.load_gateway_config", return_value=cfg), \
         patch("tools.send_message_tool._send_to_platform",
               new=AsyncMock(return_value={"success": True})) as send:
        _deliver_result(
            {"id": "j1", "name": job_name, "deliver": "origin",
             "origin": {"platform": "telegram", "chat_id": "123"}},
            "body text", failed=failed,
        )
    return send.call_args.kwargs.get("content") or send.call_args[0][-1]


def test_cron_header_clover(monkeypatch):
    monkeypatch.setattr(random, "randrange", lambda n: 7)
    sent = _deliver("daily-report")
    assert sent.startswith("☘️ daily-report\n(job_id: j1)")
    assert "Cronjob Response" not in sent
    assert "body text" in sent


def test_cron_header_lucky(monkeypatch):
    monkeypatch.setattr(random, "randrange", lambda n: 0)
    assert _deliver("daily-report").startswith("🍀 daily-report\n")


def test_cron_header_failed():
    sent = _deliver("daily-report", failed=True)
    assert sent.startswith("🥀 daily-report didn't finish\n")
    assert "body text" in sent


@pytest.mark.parametrize("skin", OTHER_SKINS)
def test_cron_header_stock_on_other_skins(monkeypatch, skin):
    _set_skin(monkeypatch, skin)
    sent = _deliver("daily-report")
    assert sent.startswith("Cronjob Response: daily-report\n(job_id: j1)\n-------------")
    assert sent.startswith(_deliver("daily-report", failed=True)[:0] + "Cronjob Response")


def test_cron_header_wrap_response_false_untouched(monkeypatch):
    from cron.scheduler import _deliver_result
    from gateway.config import Platform

    pconfig = MagicMock()
    pconfig.enabled = True
    cfg = MagicMock()
    cfg.platforms = {Platform.TELEGRAM: pconfig}
    with patch("gateway.config.load_gateway_config", return_value=cfg), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("tools.send_message_tool._send_to_platform",
               new=AsyncMock(return_value={"success": True})) as send:
        _deliver_result({"id": "j1", "name": "n", "deliver": "origin",
                         "origin": {"platform": "telegram", "chat_id": "1"}}, "bare")
    sent = send.call_args.kwargs.get("content") or send.call_args[0][-1]
    assert sent == "bare"


# --- 5. /model picker ----------------------------------------------------------------

def _adapter():
    a = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    a._bot = AsyncMock()
    a._app = MagicMock()
    return a


PROVIDERS = [{"slug": "prov_one", "name": "Provider One", "total_models": 2, "is_current": True,
              "models": ["m1", "m2"]}]


async def _send_picker(adapter):
    sent = {}

    async def _send(**kw):
        sent.update(kw)
        return SimpleNamespace(message_id=101)

    adapter._bot.send_message = AsyncMock(side_effect=_send)
    await adapter.send_model_picker(
        chat_id="12345", providers=PROVIDERS, current_model="model_1",
        current_provider="prov_one", session_key="s", on_model_selected=AsyncMock(),
    )
    return sent


def _button_texts(rows):
    return [t for row in rows for t in row]


@pytest.fixture(autouse=True)
def plain_buttons(monkeypatch):
    """Buttons become their label, keyboards become their rows."""
    monkeypatch.setattr("plugins.platforms.telegram.adapter.InlineKeyboardButton",
                        lambda text, callback_data=None, **kw: text)
    monkeypatch.setattr("plugins.platforms.telegram.adapter.InlineKeyboardMarkup", lambda rows: rows)


@pytest.mark.asyncio
async def test_model_picker_clover():
    sent = await _send_picker(_adapter())
    assert "🍀 *Pick a model*" in sent["text"] or "🍀 \\*Pick a model\\*" in sent["text"] or "Pick a model" in sent["text"]
    assert "Model Configuration" not in sent["text"]
    assert "Using: `model_1`" in sent["text"]
    assert "Current model" not in sent["text"]
    assert "Choose a provider:" in sent["text"]
    assert "Provider:" in sent["text"]
    assert any(t.startswith("🍀 ") and "Provider One" in t for t in _button_texts(sent["reply_markup"])), _button_texts(sent["reply_markup"])
    assert not any("✓" in t for t in _button_texts(sent["reply_markup"]))


@pytest.mark.asyncio
@pytest.mark.parametrize("skin", OTHER_SKINS)
async def test_model_picker_stock_other_skins(monkeypatch, skin):
    _set_skin(monkeypatch, skin)
    sent = await _send_picker(_adapter())
    assert "Model Configuration" in sent["text"]
    assert "Current model: `model_1`" in sent["text"]
    assert "Select a provider:" in sent["text"]
    assert any(t.startswith("✓ ") for t in _button_texts(sent["reply_markup"]))


def _query(data):
    q = AsyncMock()
    q.data = data
    q.message = MagicMock()
    q.message.chat_id = 12345
    q.message.text = "Pick"
    q.from_user = MagicMock()
    q.from_user.id = "777"
    q.from_user.first_name = "Tester"
    return q


def _state(adapter, on_selected=None):
    adapter._model_picker_state["12345"] = {
        "providers": PROVIDERS, "current_model": "model_1", "current_provider": "prov_one",
        "session_key": "s", "on_model_selected": on_selected or AsyncMock(return_value="switched ok"),
        "msg_id": 42,
    }


@pytest.mark.asyncio
async def test_model_picker_model_step_clover():
    a = _adapter()
    _state(a)
    q = _query("mp:prov_one")
    await a._handle_model_picker_callback(q, "mp:prov_one", "12345")
    text = q.edit_message_text.call_args[1]["text"]
    assert "Pick a model" in text and "Choose a model:" in text
    assert "Select a model" not in text and "Model Configuration" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("skin", OTHER_SKINS)
async def test_model_picker_model_step_stock(monkeypatch, skin):
    _set_skin(monkeypatch, skin)
    a = _adapter()
    _state(a)
    q = _query("mp:prov_one")
    await a._handle_model_picker_callback(q, "mp:prov_one", "12345")
    text = q.edit_message_text.call_args[1]["text"]
    assert "Model Configuration" in text and "Select a model:" in text


@pytest.mark.asyncio
async def test_model_picker_expired_and_switched_answers_clover():
    a = _adapter()
    q = _query("mb")
    await a._handle_model_picker_callback(q, "mb", "12345")
    assert q.answer.call_args[1]["text"] == "☘️ this menu expired, send /model again"

    _state(a)
    q = _query("mm:0")
    a._model_picker_state["12345"].update(
        selected_provider="prov_one", selected_provider_name="Provider One", model_list=["m1", "m2"], model_page=0)
    with patch("clover_cli.model_selection_guards.combined_selection_warning", return_value=None):
        await a._handle_model_picker_callback(q, "mm:0", "12345")
    assert q.answer.call_args[1]["text"] == "🍀 switched!"


@pytest.mark.asyncio
@pytest.mark.parametrize("skin", OTHER_SKINS)
async def test_model_picker_expired_and_switched_answers_stock(monkeypatch, skin):
    _set_skin(monkeypatch, skin)
    a = _adapter()
    q = _query("mb")
    await a._handle_model_picker_callback(q, "mb", "12345")
    assert q.answer.call_args[1]["text"] == "Picker expired — use /model again."
    _state(a)
    a._model_picker_state["12345"].update(
        selected_provider="prov_one", selected_provider_name="Provider One", model_list=["m1", "m2"], model_page=0)
    q = _query("mm:0")
    with patch("clover_cli.model_selection_guards.combined_selection_warning", return_value=None):
        await a._handle_model_picker_callback(q, "mm:0", "12345")
    assert q.answer.call_args[1]["text"] == "Model switched!"


# --- 6. clarify + exec-approval buttons ----------------------------------------------

async def _send_clarify(adapter):
    sent = {}

    async def _send(**kw):
        sent.update(kw)
        return SimpleNamespace(message_id=100)

    adapter._bot.send_message = AsyncMock(side_effect=_send)
    await adapter.send_clarify(chat_id="12345", question="Which color?", choices=["red", "green"],
                               clarify_id="cidX", session_key="sk")
    return sent


@pytest.mark.asyncio
async def test_clarify_clover():
    sent = await _send_clarify(_adapter())
    assert sent["text"].startswith("🌼 Which color?")
    texts = _button_texts(sent["reply_markup"])
    assert "✏️ something else" in texts
    assert "Other (type answer)" not in " ".join(texts)


@pytest.mark.asyncio
@pytest.mark.parametrize("skin", OTHER_SKINS)
async def test_clarify_stock(monkeypatch, skin):
    _set_skin(monkeypatch, skin)
    sent = await _send_clarify(_adapter())
    assert sent["text"].startswith("❓ Which color?")
    assert "✏️ Other (type answer)" in _button_texts(sent["reply_markup"])


def _clarify_callback(adapter, data):
    from tools import clarify_gateway as cm

    with cm._lock:
        cm._entries.clear(); cm._session_index.clear(); cm._notify_cbs.clear()
    cm.register("cidA", "sk", "Pick", ["red", "green"])
    adapter._clarify_state["cidA"] = "sk"
    q = _query(data)
    upd = MagicMock()
    upd.callback_query = q
    return q, upd


@pytest.mark.asyncio
async def test_clarify_callbacks_clover():
    a = _adapter()
    q, upd = _clarify_callback(a, "cl:cidA:other")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}):
        await a._handle_callback_query(upd, MagicMock())
    assert q.answer.call_args[1]["text"] == "✏️ type it in the chat"
    assert "waiting for Tester to type…" in q.edit_message_text.call_args[1]["text"]
    assert "Awaiting typed" not in q.edit_message_text.call_args[1]["text"]

    a = _adapter()
    q, upd = _clarify_callback(a, "cl:cidA:1")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}):
        await a._handle_callback_query(upd, MagicMock())
    assert q.answer.call_args[1]["text"] == "🍀 green"
    assert q.edit_message_text.call_args[1]["text"] == "🌼 Pick\n\n🍀 <b>Tester:</b> green"


@pytest.mark.asyncio
@pytest.mark.parametrize("skin", OTHER_SKINS)
async def test_clarify_callbacks_stock(monkeypatch, skin):
    _set_skin(monkeypatch, skin)
    a = _adapter()
    q, upd = _clarify_callback(a, "cl:cidA:other")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}):
        await a._handle_callback_query(upd, MagicMock())
    assert q.answer.call_args[1]["text"] == "✏️ Type your answer in the chat."
    assert "Awaiting typed response from Tester…" in q.edit_message_text.call_args[1]["text"]

    a = _adapter()
    q, upd = _clarify_callback(a, "cl:cidA:1")
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}):
        await a._handle_callback_query(upd, MagicMock())
    assert q.answer.call_args[1]["text"] == "✓ green"
    assert q.edit_message_text.call_args[1]["text"] == "❓ Pick\n\n<b>Tester:</b> green"


async def _approval_buttons(adapter, monkeypatch):
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    await adapter.send_exec_approval(chat_id="1", command="ls", session_key="k", description="d")
    return _button_texts(adapter._bot.send_message.call_args[1]["reply_markup"])


@pytest.mark.asyncio
async def test_exec_approval_buttons_clover(monkeypatch):
    buttons = await _approval_buttons(_adapter(), monkeypatch)
    assert buttons == ["🍀 Allow Once", "🍀 Session", "🍀 Always", "🥀 Deny"]


@pytest.mark.asyncio
@pytest.mark.parametrize("skin", OTHER_SKINS)
async def test_exec_approval_buttons_stock(monkeypatch, skin):
    _set_skin(monkeypatch, skin)
    buttons = await _approval_buttons(_adapter(), monkeypatch)
    assert buttons == ["✅ Allow Once", "✅ Session", "✅ Always", "❌ Deny"]
