"""Native Telegram activity display through the REAL gateway turn runner.

Every test drives ``GatewayRunner._run_agent`` (progress callback, stream
consumer, queue routing, persistent artifact) with the production
``TelegramAdapter`` over the in-memory Bot API connector.  The same scripted
event sequence is run through today's display (native off) and the native
display so the visible content and the persisted artifact can be compared.
"""

import asyncio
import collections
import html
import importlib
import re
import sys
import time
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig, StreamingConfig
from gateway.session import SessionSource
from gateway.stream_consumer import GatewayStreamConsumer
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway._telegram_fake_api import FakeTelegramApi

FINAL = "ANSWERDONE"  # no MarkdownV2-special characters, so it survives escaping verbatim


class ScriptedAgent:
    """Fake AIAgent that replays a script through the real gateway callbacks."""

    script: list = []
    callback_owner = None   # qualname of the gateway callback that was actually attached

    def __init__(self, **kwargs):
        self.tools = []
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.stream_delta_callback = kwargs.get("stream_delta_callback")
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.reasoning_callback = None
        self.session_id = kwargs.get("session_id")

    def run_conversation(self, message, conversation_history=None, task_id=None):
        # Like the real agent: a callback the gateway did not attach is skipped.
        cb = self.tool_progress_callback or (lambda *a, **k: None)
        ScriptedAgent.callback_owner = getattr(
            getattr(self.tool_progress_callback, "__func__", None), "__qualname__", None,
        )
        for op in type(self).script:
            kind = op[0]
            if kind == "sleep":
                time.sleep(op[1])
            elif kind == "tool":
                _, name, preview, args = op
                cb("tool.started", name, preview, args)
            elif kind == "done":
                _, name, duration, is_error = op
                cb(
                    "tool.completed", name, None, None,
                    duration=duration, is_error=is_error, result="SECRET-RESULT-PAYLOAD",
                )
            elif kind == "thought":
                cb("reasoning.available", "_thinking", op[1], None)
            elif kind == "reasoning":
                if self.reasoning_callback is not None:
                    self.reasoning_callback(op[1])
            elif kind == "commentary":
                if self.interim_assistant_callback is not None:
                    self.interim_assistant_callback(op[1], already_streamed=False)
            elif kind == "delta":
                self.stream_delta_callback(op[1])
            elif kind == "fail":
                return {
                    "final_response": "", "failed": True, "error": "simulated provider failure",
                    "messages": [], "api_calls": 1,
                }
        if self.stream_delta_callback:
            self.stream_delta_callback(FINAL)
        return {
            "final_response": FINAL, "response_previewed": True, "messages": [], "api_calls": 1,
        }


def make_runner(adapter):
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
    runner.session_store = SimpleNamespace(_entries={}, _save=lambda: None)
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False, group_sessions_per_user=False, stt_enabled=False,
        streaming=StreamingConfig(
            enabled=True, transport="draft", edit_interval=0.05, buffer_threshold=5, cursor="",
        ),
    )
    return runner


DEFAULT_DISPLAY = {
    "tool_progress": "all", "tool_preview_length": 0,
    "thinking_progress": True, "live_reasoning": False,
}


async def run_turn(
    monkeypatch, tmp_path, script, *, native, display=None, interim=False, cleanup=False,
    chat_type="dm", adapter_extra=None, api_setup=None, session="sess-np",
):
    monkeypatch.setattr(GatewayStreamConsumer, "NATIVE_MIN_SEND_INTERVAL", 0.0)
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    ScriptedAgent.script = script
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = ScriptedAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    import tools.terminal_tool  # noqa: F401  (registers tool emoji)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_clover_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    cfg_display = dict(DEFAULT_DISPLAY, **(display or {}))
    cfg_display["platforms"] = {"telegram": {
        "interim_assistant_messages": interim, "cleanup_progress": cleanup,
    }}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {"display": cfg_display})

    extra = {"rich_messages": True, "native_progress": native, **(adapter_extra or {})}
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra=extra))
    api = FakeTelegramApi()
    adapter._bot = api
    adapter._native_stop_ready = True
    if api_setup:
        api_setup(api)
    runner = make_runner(adapter)
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="12345", chat_type=chat_type, user_id="12345",
    )
    session_key = f"agent:main:telegram:{chat_type}:12345"
    result = await runner._run_agent(
        message="hello", context_prompt="", history=[], source=source,
        session_id=session, session_key=session_key,
    )
    await asyncio.sleep(0.05)
    return SimpleNamespace(
        adapter=adapter, api=api, result=result, session_key=session_key, runner=runner,
        callback_owner=ScriptedAgent.callback_owner,
    )


def bubbles(api):
    """Final text of every persistent non-final message, in send order."""
    texts, order = {}, []
    for method, kw in api.calls:
        if method == "send_message":
            texts[kw["_message_id"]] = kw["text"]
            order.append(kw["_message_id"])
        elif method == "edit_message_text":
            texts[kw["message_id"]] = kw["text"]
    return [texts[i] for i in order if FINAL not in texts[i]]


def line_multiset(texts):
    return collections.Counter(line for t in texts for line in t.split("\n") if line.strip())


def visible(markdown):
    """Plain visible text of a rendered thinking block."""
    block = markdown.split("</tg-thinking>")[0]
    block = block.replace("<br>", "\n")
    return html.unescape(re.sub(r"<[^>]+>", "", block))


def frames_text(api):
    return "\n".join(visible(f["rich_message"]["markdown"]) for f in api.rich_drafts())


def unmd(text):
    return re.sub(r"\\(.)", r"\1", text)


T = ("tool", "terminal", "pwd", {})
S = ("tool", "web_search", "sony reviews", {"query": "sony reviews"})
R = ("tool", "read_file", "notes.md", {"path": "notes.md"})
NAP = ("sleep", 0.5)

CASES = {
    "all": ([T, NAP, S, NAP, R, NAP], {}, False),
    "new": ([S, NAP, S, NAP, R, NAP], {"tool_progress": "new"}, False),
    "verbose": ([S, NAP, R, NAP], {"tool_progress": "verbose"}, False),
    "preview40": (
        [("tool", "web_search", "q" * 120, {"query": "q" * 120}), NAP, R, NAP],
        {"tool_preview_length": 40}, False,
    ),
    "dedup": ([S, NAP, S, NAP, S, NAP], {}, False),
    "thoughts": ([("thought", "Checking the sources"), NAP, S, NAP], {}, False),
    "live_reasoning": (
        [("reasoning", "Inspecting the request.\n"), NAP, S, NAP], {"live_reasoning": True}, False,
    ),
    "commentary": (
        [("commentary", "Looking at one more item."), NAP, S, NAP], {}, True,
    ),
    "tools_hidden": ([S, NAP, R, NAP], {"tool_progress": "off", "thinking_progress": False}, False),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_native_display_shows_the_same_visible_lines_as_today_and_persists_them(
    monkeypatch, tmp_path, case,
):
    script, display, interim = CASES[case]
    old = await run_turn(monkeypatch, tmp_path, script, native=False, display=display, interim=interim)
    new = await run_turn(monkeypatch, tmp_path, script, native=True, display=display, interim=interim)

    old_lines = line_multiset(bubbles(old.api))
    new_lines = line_multiset(bubbles(new.api))
    # actual persisted artifact after the turn == what today's display leaves behind
    assert new_lines == old_lines
    if case != "tools_hidden":
        assert sum(old_lines.values()) >= 1          # the oracle is not vacuous (dedup folds 3 calls into 1 line)
    if case in {"all", "new", "verbose", "preview40", "dedup", "thoughts", "live_reasoning"}:
        assert bubbles(new.api) == bubbles(old.api)  # identical text, order and formatting

    # during the turn: no separate progress bubble — native draft + one artifact + one final
    # NEW streams through sendRichMessageDraft (the legacy plain sendMessageDraft would carry
    # no thinking block); OLD never emits a rich draft.
    if case != "tools_hidden":
        assert new.api.rich_drafts() != []
        # the REAL gateway progress callback drove it (not a stand-in)
        assert new.callback_owner == "TurnRunner.progress_callback"
    assert new.api.methods("send_message_draft") == []
    assert old.api.rich_drafts() == [] and old.api.methods("get_sticker_set") == []
    final_sends = [kw for kw in new.api.methods("send_message") if FINAL in kw["text"]]
    assert len(final_sends) == 1 and "tg-thinking" not in final_sends[0]["text"]

    # every shown line was visible inside the native block while the turn ran
    shown = frames_text(new.api)
    from agent.display import get_tool_emoji, get_tool_verb

    # Native rows preserve the legacy tool detail while presenting the action once.
    for text in bubbles(old.api):
        for line in text.split("\n"):
            core = unmd(line).replace("```", "").strip()
            legacy_thought = core.startswith("💭 ")
            if legacy_thought:
                # Preserve the thought marker and public words, but remove only
                # the generated outer italic wrapper.
                core = core.removeprefix("💭 ").strip("_*")
            else:
                core = core.strip("_*")
            if not core:
                continue
            if legacy_thought:
                draft_markup = "".join(
                    frame.get("rich_message", {}).get("markdown", "")
                    for frame in new.api.rich_drafts()
                )
                assert f"💭 {core}" in shown, (case, core)
                assert "💭 <i>" not in draft_markup
                continue
            matched_action = False
            for tool in ("terminal", "web_search", "read_file"):
                verb = get_tool_verb(tool)
                emoji = get_tool_emoji(tool, default="⚙️")
                prefix = f"{emoji} {verb}"
                if core == prefix or core.startswith(prefix + " "):
                    detail = core[len(prefix):].lstrip()
                    assert verb in shown, (case, verb)
                    assert detail in shown, (case, detail)
                    matched_action = True
                    break
            if not matched_action:
                assert core in shown, (case, core)
    # secrets/results/args beyond today's display never leak
    assert "SECRET-RESULT-PAYLOAD" not in shown
    assert all(f["can_stop"] is True for f in new.api.rich_drafts())
    assert new.adapter._rich_drafts_enabled is False and new.adapter._rich_messages_enabled is True


@pytest.mark.asyncio
async def test_hidden_progress_stays_hidden_and_commentary_has_no_separate_bubble(monkeypatch, tmp_path):
    hidden = await run_turn(
        monkeypatch, tmp_path, [S, NAP], native=True,
        display={"tool_progress": "off", "thinking_progress": False},
    )
    assert bubbles(hidden.api) == []
    assert "web_search" not in frames_text(hidden.api) and "sony" not in frames_text(hidden.api)

    shown = await run_turn(
        monkeypatch, tmp_path, [("commentary", "Looking at one more item."), NAP],
        native=True, interim=True,
    )
    assert "Looking at one more item." in frames_text(shown.api)
    persisted = bubbles(shown.api)
    assert len(persisted) == 1 and "Looking at one more item" in persisted[0]


@pytest.mark.asyncio
async def test_native_capability_loss_flushes_everything_through_todays_path(monkeypatch, tmp_path):
    failure = type("EndPointNotFound", (Exception,), {})("Endpoint 'sendRichMessageDraft' not found in Bot API")

    def setup(api):
        api.fail["sendRichMessageDraft"] = failure

    old = await run_turn(monkeypatch, tmp_path, CASES["all"][0], native=False)
    new = await run_turn(
        monkeypatch, tmp_path, CASES["all"][0], native=True, api_setup=setup,
    )
    # the in-memory failure is classified by the PRODUCTION adapter as a capability error
    assert new.adapter._is_rich_capability_error(failure) is True
    assert new.api.rich_drafts() != []           # native was attempted, then fell back
    assert line_multiset(bubbles(new.api)) == line_multiset(bubbles(old.api))
    assert new.adapter._native_progress_disabled is True
    assert len([kw for kw in new.api.methods("send_message") if FINAL in kw["text"]]) == 1


@pytest.mark.asyncio
async def test_unsupported_routes_run_exactly_todays_display(monkeypatch, tmp_path):
    new = await run_turn(
        monkeypatch, tmp_path, CASES["all"][0], native=True, adapter_extra={"rich_messages": False},
    )
    old = await run_turn(
        monkeypatch, tmp_path, CASES["all"][0], native=False, adapter_extra={"rich_messages": False},
    )
    assert bubbles(new.api) == bubbles(old.api)
    assert new.api.rich_drafts() == [] and new.api.methods("get_sticker_set") == []
