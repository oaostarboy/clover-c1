"""Native Telegram activity display (opt-in ``platforms.telegram.extra.native_progress``).

These tests drive the production ``TelegramAdapter`` over an in-memory Bot API
connector (``FakeTelegramApi``).  Nothing about the adapter, stream consumer or
gateway runner is replaced.
"""

import logging
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.gateway._telegram_fake_api import FakeTelegramApi


def make_adapter(**extra):
    """Real TelegramAdapter wired to the in-memory connector."""
    extra.setdefault("rich_messages", True)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra=extra))
    api = FakeTelegramApi()
    adapter._bot = api
    return adapter, api


def row(text, *, kind="tool", tool="web_search", state="running", started_at=0.0,
        duration=None, repeat=1):
    """Duck-typed activity row (the adapter renderer reads attributes only)."""
    return SimpleNamespace(
        text=text, kind=kind, tool=tool, state=state,
        started_at=started_at, duration=duration, repeat=repeat,
    )


# ── Config / option matrix ──────────────────────────────────────────────────


def test_native_progress_is_off_by_default():
    adapter, _ = make_adapter()
    assert adapter._native_progress_enabled is False
    adapter._native_stop_ready = True
    assert adapter.supports_native_progress(chat_type="dm") is False


def test_native_progress_default_is_documented_in_config_defaults():
    from clover_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["telegram"]["extra"]["native_progress"] is False


@pytest.mark.parametrize(
    "native, rich_messages, rich_drafts, expected",
    [
        (False, False, False, False),
        (False, True, False, False),
        (False, True, True, False),
        (True, False, False, False),
        (True, False, True, False),
        (True, True, False, True),
        (True, True, True, True),
    ],
)
def test_option_matrix_never_mutates_existing_rich_flags(native, rich_messages, rich_drafts, expected):
    adapter, _ = make_adapter(
        native_progress=native, rich_messages=rich_messages, rich_drafts=rich_drafts,
    )
    adapter._native_stop_ready = True
    assert adapter.supports_native_progress(chat_type="dm") is expected
    assert adapter._rich_messages_enabled is rich_messages
    assert adapter._rich_drafts_enabled is rich_drafts


def test_native_progress_inert_without_rich_messages_logs_once(caplog):
    adapter, _ = make_adapter(native_progress=True, rich_messages=False)
    adapter._native_stop_ready = True
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            assert adapter.supports_native_progress(chat_type="dm") is False
    notices = [r for r in caplog.records if "native_progress" in r.getMessage()]
    assert len(notices) == 1


def test_native_progress_requires_working_stop_path():
    adapter, _ = make_adapter(native_progress=True)
    # Stop subscription/auth not wired -> no native composer at all.
    assert adapter._native_stop_ready is False
    assert adapter.supports_native_progress(chat_type="dm") is False
    adapter._native_stop_ready = True
    assert adapter.supports_native_progress(chat_type="dm") is True


@pytest.mark.parametrize(
    "chat_type, metadata",
    [
        ("group", None),
        ("supergroup", None),
        ("forum", {"thread_id": "7"}),
        ("dm", {"thread_id": "7"}),
        ("dm", {"direct_messages_topic_id": "9"}),
        ("dm", {"telegram_dm_topic_reply_fallback": True, "thread_id": "5"}),
        ("channel", None),
        (None, None),
    ],
)
def test_native_progress_only_for_plain_private_chats(chat_type, metadata):
    adapter, _ = make_adapter(native_progress=True)
    adapter._native_stop_ready = True
    assert adapter.supports_native_progress(chat_type=chat_type, metadata=metadata) is False


def test_native_progress_requires_rich_capable_bot():
    adapter, _ = make_adapter(native_progress=True)
    adapter._native_stop_ready = True
    adapter._bot = SimpleNamespace(send_message_draft=lambda **kw: None)  # no async do_api_request
    assert adapter.supports_native_progress(chat_type="dm") is False




@pytest.mark.asyncio
async def test_clean_feed_frame_preserves_content_with_specific_header_natural_commentary_and_single_status():
    from agent.display import get_tool_emoji
    from plugins.platforms.telegram.native_progress import NativeIcon

    adapter, api = native_adapter()
    icons = {
        "thinking": NativeIcon("5535457114983497745", "🧠"),
        "running": NativeIcon("5537581341383589905", "🔧"),
    }
    rows = [
        row(f"{get_tool_emoji('web_search', default='⚙️')} Searching the web for local time", tool="web_search", state="running", started_at=1.0),
        row("**I found two likely clocks.**", kind="commentary", tool="", state="info"),
        row("Reading /tmp/clock report\n```text\n*utc* 09:30\n```", tool="read_file",
            state="succeeded", started_at=4.0, duration=0.2),
        row("same call identity A", tool="read_file", state="succeeded", started_at=4.0, duration=0.1),
        row("same call identity B", tool="read_file", state="succeeded", started_at=4.5, duration=0.2),
    ]
    await adapter.send_native_progress_draft("1", 81, rows, "", now=6.0, icons=icons)
    md = _markdowns(api)[-1]

    assert '<tg-emoji emoji-id="5535457114983497745">🧠</tg-emoji>' in md
    assert "Searching the web · 5s" in md
    assert "<i>Commentary</i>" not in md and "<i>I found two likely clocks.</i>" not in md
    assert "**" not in md and "I found two likely clocks." in md
    assert "<i>Running · 5s</i>" in md and "Executing" not in md
    assert "<i>Done · 0s</i>" in md
    assert "<b>Reading</b><br>/tmp/clock report" in md and "<code>*utc* 09:30</code>" in md
    assert "same call identity A" in md and "same call identity B" in md
    assert md.index("Searching the web · 5s") < md.index("I found two likely clocks.") < md.index("<b>Reading</b>")
    assert "for local time" in md
    assert md.count("5537581341383589905") == 1


def test_thought_rows_keep_one_marker_without_blanket_italics():
    from plugins.platforms.telegram.native_progress import render_row

    thought = render_row(row("Inspecting the request.", kind="thought"))
    assert thought == "💭 Inspecting the request."
    assert "<i>" not in thought


def test_legacy_commentary_keeps_marker_and_words_without_blanket_italics():
    from plugins.platforms.telegram.native_progress import render_row

    legacy = render_row(row("💭 _Looking at one more item._", kind="commentary"))
    assert legacy == "💭 Looking at one more item."
    assert "<i>" not in legacy


def test_bold_formatter_preserves_stars_inside_inline_code():
    from plugins.platforms.telegram.native_progress import _inline_markup

    rendered = _inline_markup("Use `x ** y` literally, and **make this bold**.")
    assert "<code>x ** y</code>" in rendered
    assert "<b>make this bold</b>" in rendered


def test_completed_unpaired_tool_row_does_not_claim_an_unknown_duration():
    from plugins.platforms.telegram.native_progress import render_row

    rendered = render_row(row("same-name call was ambiguous", state="completed", duration=8.0))
    assert "<i>Completed</i>" in rendered
    assert "8s" not in rendered


def test_tool_row_renders_action_once_and_only_fenced_code_as_code():
    from agent.display import get_tool_emoji
    from plugins.platforms.telegram.native_progress import render_row

    code_emoji = get_tool_emoji("execute_code", default="⚙️")
    prose = row(f"{code_emoji} Running code from pathlib import Path W=Path('/home/…')", tool="execute_code")
    rendered = render_row(prose)
    assert "<b>Execute code</b>" in rendered
    assert rendered.count("Running") == 1
    assert code_emoji not in rendered
    assert "<code>" not in rendered
    assert "from pathlib import Path W=Path('/home/…')" in rendered
    assert prose.text == f"{code_emoji} Running code from pathlib import Path W=Path('/home/…')"

    terminal_emoji = get_tool_emoji("terminal", default="⚙️")
    fenced = row(f"{terminal_emoji} Running command\n```bash\nls -la\n```", tool="terminal")
    rendered_fence = render_row(fenced)
    assert rendered_fence.count("Running") == 1
    assert "<b>Terminal</b><br><code>ls -la</code>" in rendered_fence
    assert terminal_emoji not in rendered_fence
    assert "<code>ls -la</code>" in rendered_fence
    assert "<code><code>" not in rendered_fence
    assert "<code>command<br>" not in rendered_fence

    nonmatching = row("Running codebase diagnostics 🧪", tool="execute_code")
    assert "Running codebase diagnostics 🧪" in render_row(nonmatching)


@pytest.mark.asyncio
async def test_screenshot_activity_rows_render_with_hierarchy_without_losing_repeated_details():
    adapter, api = native_adapter()
    rows = [
        row("Thinking through the task", kind="thought", tool=None, state="info"),
        row("Running code from pathlib import Path W=Path('/home/…')", tool="execute_code",
            state="running", started_at=0.4),
        row("Updating tasks reading task list", tool="todo", state="succeeded",
            started_at=0.0, duration=0.2),
        row("Running code from pathlib import Path import subprocess…", tool="execute_code",
            state="succeeded", started_at=0.0, duration=0.4),
        row("Running code from pathlib import Path import re…", tool="execute_code",
            state="succeeded", started_at=0.0, duration=0.4),
    ]

    await adapter.send_native_progress_draft("1", 81, rows, "", now=6.4)
    md = _markdowns(api)[-1]
    header = md.partition("<br>")[0]

    assert "<b>Running code · 6s</b>" in header
    assert md.count("<br><br>") == len(rows)
    assert "<b>Execute code</b>" in md
    assert md.count("<b>Execute code</b>") == 3
    assert md.count("Running code") == 1  # only the current action header retains the raw title
    assert "<code>Running code from pathlib" not in md
    assert "<i>Thought</i>" not in md and "Thinking through the task" in md
    assert "<b>Updating tasks</b>" in md
    assert "<i>Done · 0s</i>" in md
    assert md.count("from pathlib import Path") == 3
    assert "<b>Execute code</b><br>from pathlib import Path" in md


@pytest.mark.asyncio
async def test_native_draft_uses_official_rich_draft_payload_with_can_stop():
    adapter, api = make_adapter(native_progress=True)  # rich_drafts left at its default (off)
    adapter._native_stop_ready = True

    result = await adapter.send_native_progress_draft(
        "12345", 4242,
        [row("🔍 Searching the web for sony reviews", started_at=0.0)],
        "partial **answer**",
        now=6.0,
    )

    assert result.success is True and result.message_id is None
    [frame] = api.rich_drafts()
    assert set(frame) == {"chat_id", "draft_id", "rich_message", "can_stop"}
    assert frame["chat_id"] == 12345
    assert frame["draft_id"] == 4242
    assert frame["can_stop"] is True
    assert set(frame["rich_message"]) == {"markdown"}
    md = frame["rich_message"]["markdown"]
    assert md.startswith("<tg-thinking>") and md.count("<tg-thinking>") == 1
    head, _, tail = md.partition("</tg-thinking>")
    assert "Searching the web" in head
    assert tail.strip() == "partial **answer**"
    # Only the ephemeral composer opted into the draft endpoint; flags untouched.
    assert adapter._rich_drafts_enabled is False
    assert adapter._rich_messages_enabled is True
    # No legacy plain draft or persistent send happened.
    assert api.methods("send_message_draft") == []
    assert api.methods("send_message") == []


@pytest.mark.asyncio
async def test_native_draft_escapes_markup_and_preserves_unicode():
    adapter, api = make_adapter(native_progress=True)
    adapter._native_stop_ready = True
    nasty = '⚙️ run <b>x</b> & "q" \'s\' </tg-thinking><tg-emoji emoji-id="1">x</tg-emoji> 你好 🚀'

    await adapter.send_native_progress_draft("1", 5, [row(nasty)], "", now=1.0)

    md = api.rich_drafts()[0]["rich_message"]["markdown"]
    # User text can never terminate (or open) the real block: one real pair only.
    assert md.startswith("<tg-thinking>")
    assert md.count("<tg-thinking>") == 1 and md.count("</tg-thinking>") == 1
    assert md.endswith("</tg-thinking>")
    assert "&lt;b&gt;x&lt;/b&gt; &amp;" in md
    assert "&lt;/tg-thinking&gt;" in md and "&lt;tg-emoji" in md
    assert md.count("<b>") == 2 and md.count("</b>") == 2
    assert "你好 🚀" in md


@pytest.mark.asyncio
async def test_native_draft_rejects_zero_draft_id_and_oversize_frames_without_latching():
    adapter, api = make_adapter(native_progress=True)
    adapter._native_stop_ready = True

    zero = await adapter.send_native_progress_draft("1", 0, [row("x")], "", now=1.0)
    huge = await adapter.send_native_progress_draft("1", 9, [row("y" * 40000)], "", now=1.0)

    assert zero.success is False and huge.success is False
    assert api.methods("do_api_request:sendRichMessageDraft") == []
    assert adapter._native_progress_disabled is False


@pytest.mark.asyncio
async def test_native_capability_failure_latches_only_native_progress():
    adapter, api = make_adapter(native_progress=True)
    adapter._native_stop_ready = True
    api.fail["sendRichMessageDraft"] = type("EndPointNotFound", (Exception,), {})("no such method")

    result = await adapter.send_native_progress_draft("1", 9, [row("x")], "", now=1.0)

    assert result.success is False
    assert adapter._native_progress_disabled is True
    assert adapter._rich_draft_disabled is False
    assert adapter._rich_send_disabled is False
    assert adapter.supports_native_progress(chat_type="dm") is False


@pytest.mark.asyncio
async def test_native_draft_refuses_when_feature_off_and_old_draft_path_is_unchanged():
    adapter, api = make_adapter()  # native_progress off
    adapter._native_stop_ready = True

    refused = await adapter.send_native_progress_draft("1", 9, [row("x")], "", now=1.0)
    assert refused.success is False
    assert api.calls == []

    # Old path: plain legacy draft, byte-for-byte what it sent before this feature.
    result = await adapter.send_draft("12345", 7, "hello", None)
    assert result.success is True
    [call] = api.methods("send_message_draft")
    assert call["chat_id"] == 12345 and call["draft_id"] == 7
    assert "can_stop" not in call
    assert api.methods("get_sticker_set") == []


# ── AIActions icon lookup (bounded, cached, never blocks a frame) ───────────

import asyncio  # noqa: E402

from telegram.error import BadRequest, RetryAfter  # noqa: E402

# Real current-set IDs, validated by the read-only getStickerSet response and
# rendered artwork. The pack's metadata emoji is 🙂 for both entries.
ID_THINK = "5535457114983497745"
ID_RUN = "5537581341383589905"


def _sticker(emoji, custom_id, *, animated=True, video=False, kind="custom_emoji"):
    # tests/gateway mocks the ``telegram`` package, so build the PTB Sticker
    # shape (verified against real PTB in tests/plugins/platforms/telegram).
    return SimpleNamespace(
        file_id=f"f{custom_id}", file_unique_id=f"u{custom_id}", width=100, height=100,
        is_animated=animated, is_video=video, type=kind, emoji=emoji,
        custom_emoji_id=custom_id,
    )


def _aiactions(stickers=None):
    return SimpleNamespace(
        name="AIActions", title="AI Actions", sticker_type="custom_emoji",
        stickers=stickers if stickers is not None else [
            _sticker("🙂", ID_THINK), _sticker("🙂", ID_RUN),
        ],
    )


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def native_adapter(**kw):
    adapter, api = make_adapter(native_progress=True, **kw)
    adapter._native_stop_ready = True
    adapter._native_clock = Clock()
    return adapter, api


async def _settle_lookup(adapter):
    task = getattr(adapter, "_native_icon_task", None)
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


async def _frame(adapter, draft_id=1):
    return await adapter.send_native_progress_draft(
        "1", draft_id, [row("🔍 Searching", started_at=0.0)], "", now=3.0,
    )


def test_terminal_running_header_keeps_source_title_and_not_dangling_command():
    import asyncio

    from agent.display import get_tool_emoji

    async def render():
        adapter, api = native_adapter()
        api.sticker_sets["AIActions"] = _aiactions()
        terminal = get_tool_emoji("terminal", default="⚙️")
        entry = row(
            f"{terminal} Running command sleep 10; date '+%I:%M:%S %p %Z'",
            tool="terminal", state="running", started_at=996.0,
        )
        await adapter.send_native_progress_draft("1", 73, [entry], "", now=1001.0)
        await _settle_lookup(adapter)
        await adapter.send_native_progress_draft("1", 73, [entry], "", now=1001.0)
        return _markdowns(api)[-1]

    markdown = asyncio.run(render())
    assert "Running command · 5s</b>" in markdown
    assert "<b>Terminal</b>" in markdown
    assert "sleep 10; date '+%I:%M:%S %p %Z'" in markdown
    assert "command sleep" not in markdown


def _markdowns(api):
    return [f["rich_message"]["markdown"] for f in api.rich_drafts()]


def test_real_aiactions_shape_selects_visually_verified_roles_not_metadata_emoji():
    adapter, _ = native_adapter()
    real_set = _aiactions([
        _sticker("🙂", "5535457114983497745"),  # rendered cell 11: outlined brain
        _sticker("🙂", "5537581341383589905"),  # cell 42: wrench/tool with play mark
    ])

    icons = adapter._select_native_icons(real_set)

    assert icons["thinking"].custom_emoji_id == "5535457114983497745"
    assert icons["thinking"].emoji == "🧠"
    assert icons["running"].custom_emoji_id == "5537581341383589905"
    assert icons["running"].emoji == "🔧"
    assert "succeeded" not in icons and "failed" not in icons


def test_icon_lookup_budgets_match_the_plan():
    assert TelegramAdapter.NATIVE_ICON_LOOKUP_TIMEOUT == 5.0
    assert TelegramAdapter.NATIVE_ICON_POSITIVE_TTL == 24 * 3600
    assert TelegramAdapter.NATIVE_ICON_NEGATIVE_TTL == 5 * 60


@pytest.mark.asyncio
async def test_first_frame_never_waits_for_icon_lookup_and_later_frames_use_runtime_ids():
    adapter, api = native_adapter()
    api.sticker_sets["AIActions"] = _aiactions()
    api.delay["get_sticker_set"] = 0.2

    t0 = asyncio.get_running_loop().time()
    first = await _frame(adapter)
    assert first.success is True
    assert asyncio.get_running_loop().time() - t0 < 0.15  # did not wait on the lookup
    assert "<tg-emoji" not in _markdowns(api)[0]          # text is visible immediately
    assert "Searching" in _markdowns(api)[0]

    await _settle_lookup(adapter)
    await _frame(adapter)
    md = _markdowns(api)[-1]
    assert f'<tg-emoji emoji-id="{ID_THINK}">🧠</tg-emoji>' in md
    assert f'<tg-emoji emoji-id="{ID_RUN}">🔧</tg-emoji>' in md
    assert "5368324170671202286" not in md  # docs sample id is never product data
    assert len(api.methods("get_sticker_set")) == 1
    assert api.methods("get_sticker_set")[0]["name"] == "AIActions"


@pytest.mark.asyncio
async def test_lookup_is_single_flight_across_concurrent_frames():
    adapter, api = native_adapter()
    api.sticker_sets["AIActions"] = _aiactions()
    api.delay["get_sticker_set"] = 0.1

    await asyncio.gather(*[_frame(adapter, 10 + i) for i in range(8)])
    await _settle_lookup(adapter)

    assert len(api.methods("get_sticker_set")) == 1


@pytest.mark.asyncio
async def test_lookup_timeout_falls_back_to_text_and_is_negative_cached_for_five_minutes(monkeypatch):
    adapter, api = native_adapter()
    monkeypatch.setattr(TelegramAdapter, "NATIVE_ICON_LOOKUP_TIMEOUT", 0.05)
    api.sticker_sets["AIActions"] = _aiactions()
    api.delay["get_sticker_set"] = 1.0

    await _frame(adapter)
    await _settle_lookup(adapter)
    await _frame(adapter)
    assert all("<tg-emoji" not in md for md in _markdowns(api))
    assert len(api.methods("get_sticker_set")) == 1

    adapter._native_clock.t += 299          # still inside the 5 minute negative TTL
    await _frame(adapter)
    await _settle_lookup(adapter)
    assert len(api.methods("get_sticker_set")) == 1

    api.delay["get_sticker_set"] = 0.0
    adapter._native_clock.t += 2            # past it: exactly one fresh attempt
    await _frame(adapter)
    await _settle_lookup(adapter)
    assert len(api.methods("get_sticker_set")) == 2
    await _frame(adapter)
    assert "<tg-emoji" in _markdowns(api)[-1]


@pytest.mark.asyncio
async def test_positive_cache_lives_24_hours_then_refreshes_once():
    adapter, api = native_adapter()
    api.sticker_sets["AIActions"] = _aiactions()

    await _frame(adapter)
    await _settle_lookup(adapter)
    adapter._native_clock.t += 24 * 3600 - 5
    for _ in range(3):
        await _frame(adapter)
        await _settle_lookup(adapter)
    assert len(api.methods("get_sticker_set")) == 1
    assert "<tg-emoji" in _markdowns(api)[-1]

    adapter._native_clock.t += 10
    await _frame(adapter)           # stale cache is still usable while refreshing
    assert "<tg-emoji" in _markdowns(api)[-1]
    await _settle_lookup(adapter)
    assert len(api.methods("get_sticker_set")) == 2


@pytest.mark.asyncio
async def test_retry_after_suppresses_lookups_without_a_retry_loop():
    adapter, api = native_adapter()
    api.fail["get_sticker_set"] = RetryAfter(900)

    for _ in range(4):
        await _frame(adapter)
        await _settle_lookup(adapter)
    assert len(api.methods("get_sticker_set")) == 1

    adapter._native_clock.t += 899
    await _frame(adapter)
    await _settle_lookup(adapter)
    assert len(api.methods("get_sticker_set")) == 1

    api.fail.clear()
    api.sticker_sets["AIActions"] = _aiactions()
    adapter._native_clock.t += 2
    await _frame(adapter)
    await _settle_lookup(adapter)
    assert len(api.methods("get_sticker_set")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stickers",
    [
        [],                                                                  # empty set
        [_sticker("⚙️", ID_RUN, animated=False, video=False)],               # static only
        [_sticker("⚙️", ID_RUN, kind="regular")],                            # not custom emoji
        [_sticker("🐟", "7700000000000002")],                                # unknown verified id
        [SimpleNamespace(is_animated=True, is_video=False, type="custom_emoji", emoji="⚙️", custom_emoji_id=None)],
    ],
)
async def test_unusable_sets_stay_text_only_and_are_negative_cached(stickers):
    adapter, api = native_adapter()
    api.sticker_sets["AIActions"] = _aiactions(stickers)

    await _frame(adapter)
    await _settle_lookup(adapter)
    await _frame(adapter)
    await _frame(adapter)

    assert all("<tg-emoji" not in md for md in _markdowns(api))
    assert all(f["can_stop"] is True for f in api.rich_drafts())
    assert len(api.methods("get_sticker_set")) == 1


@pytest.mark.asyncio
async def test_video_sticker_counts_as_animation_capable():
    adapter, api = native_adapter()
    api.sticker_sets["AIActions"] = _aiactions([_sticker("⚙️", ID_RUN, animated=False, video=True)])
    await _frame(adapter)
    await _settle_lookup(adapter)
    await _frame(adapter)
    assert f'emoji-id="{ID_RUN}"' in _markdowns(api)[-1]


@pytest.mark.asyncio
async def test_entitlement_rejection_degrades_icons_only():
    adapter, api = native_adapter()
    api.sticker_sets["AIActions"] = _aiactions()
    await _frame(adapter)
    await _settle_lookup(adapter)

    api.fail["sendRichMessageDraft"] = lambda kw: (
        BadRequest("Bad Request: can't parse entities: custom emoji can't be used")
        if "<tg-emoji" in kw["api_kwargs"]["rich_message"]["markdown"] else None
    )
    result = await _frame(adapter)

    assert result.success is True               # the frame still landed, immediately
    last = _markdowns(api)[-1]
    assert "<tg-emoji" not in last and "🔧" in last   # semantic text fallback, no custom tag
    assert adapter._native_progress_disabled is False   # native display + Stop untouched
    await _frame(adapter)
    assert all("<tg-emoji" not in md for md in _markdowns(api)[-2:])
    assert adapter._native_icons_disabled is True


@pytest.mark.asyncio
async def test_lookup_failures_are_redacted_and_off_mode_never_looks_up(caplog):
    adapter, api = native_adapter()
    token = "8123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    api.fail["get_sticker_set"] = RuntimeError(f"https://api.telegram.org/bot{token}/getStickerSet failed")
    with caplog.at_level(logging.DEBUG):
        await _frame(adapter)
        await _settle_lookup(adapter)
    assert token not in caplog.text
    assert adapter._native_progress_disabled is False

    off, off_api = make_adapter()          # native_progress off
    off._native_stop_ready = True
    await off.send_draft("1", 5, "hi", None)
    assert off.supports_native_progress(chat_type="dm") is False
    assert off_api.methods("get_sticker_set") == []
    assert getattr(off, "_native_icon_task", None) is None


# ═══ P1: one GatewayStreamConsumer owns status + partial answer ═════════════

import time  # noqa: E402

from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig, format_thought  # noqa: E402


async def until(predicate, timeout=3.0, step=0.01):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return bool(predicate())


class History:
    """Records what the consumer hands to the existing persistent-artifact path."""

    def __init__(self, api=None):
        self.calls = []
        self.api = api

    async def __call__(self, lines, reason):
        self.calls.append(
            (list(lines), reason, len(self.api.calls) if self.api is not None else None)
        )


def make_consumer(adapter, *, chat_type="dm", metadata=None, history="default", native_kwargs=None):
    cfg = StreamConsumerConfig(
        transport="draft", chat_type=chat_type, edit_interval=0.05, buffer_threshold=5, cursor="",
    )
    from gateway.native_progress import NativeProgressScope

    kwargs = dict(
        metadata=metadata, initial_reply_to_id="99",
        native_scope=NativeProgressScope(session_key="sess", run_generation=1, source=None),
    )
    # Native mode is only offered when the existing persistent-artifact path and a
    # Stop scope are wired (the gateway always passes both); history=None omits it.
    if history == "default":
        history = History(adapter._bot)
    if history is not None:
        kwargs["on_native_history"] = history
    kwargs.update(native_kwargs or {})
    return GatewayStreamConsumer(adapter, "12345", cfg, **kwargs)


@pytest.mark.asyncio
async def test_native_composer_is_ineligible_without_a_persistent_history_path():
    adapter, api = native_adapter()
    consumer = make_consumer(adapter, history=None)
    assert consumer.accepts_tool_progress is False
    assert consumer.native_activity_active is False


def fast(monkeypatch, *, spacing=0.0, drain=None, refresh=None, keepalive=None, max_keepalives=None):
    monkeypatch.setattr(GatewayStreamConsumer, "NATIVE_MIN_SEND_INTERVAL", spacing)
    if drain is not None:
        monkeypatch.setattr(GatewayStreamConsumer, "NATIVE_FINAL_DRAIN", drain)
    if refresh is not None:
        monkeypatch.setattr(GatewayStreamConsumer, "NATIVE_REFRESH_INTERVAL", refresh)
    if keepalive is not None:
        monkeypatch.setattr(GatewayStreamConsumer, "NATIVE_KEEPALIVE_INTERVAL", keepalive)
    if max_keepalives is not None:
        monkeypatch.setattr(GatewayStreamConsumer, "NATIVE_MAX_KEEPALIVES", max_keepalives)


def final_sends(api):
    """Persistent (non-draft) messages a user would see in history."""
    return [kw for kw in api.methods("send_message")] + [
        kw for m, kw in api.calls if m == "do_api_request:sendRichMessage"
    ]


def thinking_of(md):
    head, _, tail = md.partition("</tg-thinking>")
    return head, tail.strip()


def test_consumer_budgets_match_the_plan():
    c = GatewayStreamConsumer
    assert c.NATIVE_MIN_SEND_INTERVAL == 1.0
    assert c.NATIVE_REFRESH_INTERVAL == 5.0
    assert c.NATIVE_KEEPALIVE_INTERVAL <= 20.0
    assert c.NATIVE_MAX_KEEPALIVES == 15
    assert c.NATIVE_MAX_AGE == 300.0
    assert c.NATIVE_FINAL_DRAIN == 2.0


@pytest.mark.asyncio
async def test_one_composer_shows_rows_and_partial_answer_on_one_draft_and_final_is_sent_once(monkeypatch):
    fast(monkeypatch)
    adapter, api = native_adapter()
    consumer = make_consumer(adapter)
    assert consumer.accepts_tool_progress is True
    task = asyncio.create_task(consumer.run())

    consumer.on_tool_progress("🔍 Searching the web for sony reviews", tool="web_search")
    assert await until(lambda: api.rich_drafts())
    consumer.on_delta("Here is the ")
    assert await until(lambda: any("Here is the" in thinking_of(m)[1] for m in _markdowns(api)))
    consumer.on_tool_complete("web_search", duration=2.0, is_error=False)
    consumer.on_delta("answer.")
    assert await until(lambda: any("Done" in m for m in _markdowns(api)))
    consumer.finish("Here is the answer.")
    await asyncio.wait_for(task, 3)

    drafts = api.rich_drafts()
    assert len({d["draft_id"] for d in drafts}) == 1 and drafts[0]["draft_id"] > 0
    assert all(d["can_stop"] is True and d["chat_id"] == 12345 for d in drafts)
    mds = _markdowns(api)
    assert any("Running · 0s" in m and "Searching the web for sony reviews" in m for m in mds)
    assert any("Done · 2s" in m for m in mds)
    # No second progress bubble, no legacy draft/edit: the only persistent send is the final.
    assert api.methods("send_message_draft") == [] and api.methods("edit_message_text") == []
    sent = final_sends(api)
    assert len(sent) == 1
    assert "Here is the answer" in str(sent[0])  # MarkdownV2 escapes the period
    assert "tg-thinking" not in str(sent[0])


@pytest.mark.asyncio
async def test_off_mode_consumer_is_byte_identical_to_the_legacy_draft_path(monkeypatch):
    fast(monkeypatch)
    adapter, api = make_adapter()  # native_progress off
    adapter._native_stop_ready = True
    consumer = make_consumer(adapter)
    assert consumer.accepts_tool_progress is False
    task = asyncio.create_task(consumer.run())
    consumer.on_tool_progress("🔍 Searching")        # ignored exactly as today
    consumer.on_delta("hello wor")
    assert await until(lambda: api.methods("send_message_draft"))
    consumer.on_delta("ld")
    consumer.finish("hello world")
    await asyncio.wait_for(task, 3)

    assert api.rich_drafts() == []
    assert api.methods("get_sticker_set") == []
    assert all("can_stop" not in kw for kw in api.methods("send_message_draft"))
    assert len(final_sends(api)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chat_type, metadata, extra",
    [
        ("group", None, {}),
        ("forum", {"thread_id": "7"}, {}),
        ("dm", {"thread_id": "7"}, {}),
        ("dm", None, {"rich_messages": False}),
    ],
)
async def test_unsupported_routes_keep_todays_display(chat_type, metadata, extra):
    adapter, api = make_adapter(native_progress=True, **extra)
    adapter._native_stop_ready = True
    consumer = make_consumer(adapter, chat_type=chat_type, metadata=metadata)
    assert consumer.accepts_tool_progress is False
    consumer.on_tool_progress("🔍 Searching", tool="web_search")
    assert api.rich_drafts() == []


@pytest.mark.asyncio
async def test_segment_break_allocates_new_draft_and_repopulates_the_activity(monkeypatch):
    fast(monkeypatch)
    adapter, api = native_adapter()
    consumer = make_consumer(adapter)
    task = asyncio.create_task(consumer.run())

    consumer.on_tool_progress("🔍 first lookup", tool="web_search")
    consumer.on_delta("segment one text")
    assert await until(lambda: any("segment one text" in thinking_of(m)[1] for m in _markdowns(api)))
    first_id = api.rich_drafts()[-1]["draft_id"]
    consumer.on_segment_break()
    consumer.on_tool_progress("💻 second step", tool="terminal")
    consumer.on_delta("segment two text")
    assert await until(lambda: any("segment two text" in thinking_of(m)[1] for m in _markdowns(api)))
    consumer.finish("segment two text")
    await asyncio.wait_for(task, 3)

    second = [d for d in api.rich_drafts() if "segment two text" in thinking_of(d["rich_message"]["markdown"])[1]]
    assert second and second[0]["draft_id"] != first_id and second[0]["draft_id"] > 0
    head = thinking_of(second[0]["rich_message"]["markdown"])[0]
    assert "first lookup" in head and "second step" in head   # whole-turn activity survives


@pytest.mark.asyncio
async def test_frames_are_locally_paced(monkeypatch):
    fast(monkeypatch, spacing=0.25)
    adapter, api = native_adapter()
    consumer = make_consumer(adapter)
    task = asyncio.create_task(consumer.run())
    for i in range(12):
        consumer.on_tool_progress(f"⚙️ step {i}", tool=f"tool{i}")
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.4)
    consumer.finish("")
    await asyncio.wait_for(task, 3)

    times = [t for (m, _), t in zip(api.calls, api.call_times) if m == "do_api_request:sendRichMessageDraft"]
    assert len(times) >= 2
    assert all(b - a >= 0.24 for a, b in zip(times, times[1:]))
    # Coalesced: 12 events do not become 12 sends.
    assert len(times) <= 5


@pytest.mark.asyncio
async def test_unchanged_dirty_snapshot_does_not_resend_the_same_content_frame(monkeypatch):
    fast(monkeypatch, refresh=100.0, keepalive=100.0)
    adapter, api = native_adapter()
    consumer = make_consumer(adapter)
    task = asyncio.create_task(consumer.run())
    consumer.on_tool_progress("🔍 stable lookup", tool="web_search")
    assert await until(lambda: api.rich_drafts())
    assert await until(lambda: consumer._np_task is None)

    consumer._np_dirty = True  # A dirty notification with no ledger/answer change.
    await consumer._np_pump()
    await asyncio.sleep(0.1)
    assert len(api.rich_drafts()) == 1, "identical content must not trigger avoidable layout churn"

    consumer.finish("")
    await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_stalled_draft_send_never_delays_final_beyond_the_drain_bound(monkeypatch):
    fast(monkeypatch, drain=0.3)
    adapter, api = native_adapter()
    api.gate["sendRichMessageDraft"] = asyncio.Event()      # network send that never answers
    history = History(api)
    consumer = make_consumer(adapter, history=history)
    task = asyncio.create_task(consumer.run())

    consumer.on_tool_progress("🔍 stalled lookup", tool="web_search")
    assert await until(lambda: api.rich_drafts())            # send is now in flight (and stuck)
    consumer.on_delta("final answer text")
    started = asyncio.get_running_loop().time()
    consumer.finish("final answer text")
    await asyncio.wait_for(task, 3)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 1.5                                     # drain bound, not the stalled network
    assert len(final_sends(api)) == 1 and "final answer text" in str(final_sends(api)[0])
    frames_at_final = len(api.rich_drafts())
    # The stuck frame finally lands after the final: it must be ignored, no revival.
    api.gate["sendRichMessageDraft"].set()
    await asyncio.sleep(0.3)
    assert len(api.rich_drafts()) == frames_at_final
    assert consumer.native_activity_active is False
    assert len(final_sends(api)) == 1
    assert history.calls and history.calls[0][0] == ["🔍 stalled lookup"]


@pytest.mark.asyncio
async def test_idle_keepalive_is_bounded_then_flushes_to_the_legacy_artifact(monkeypatch):
    fast(monkeypatch, keepalive=0.1, refresh=100.0, max_keepalives=3)
    adapter, api = native_adapter()
    history = History(api)
    consumer = make_consumer(adapter, history=history)
    task = asyncio.create_task(consumer.run())

    consumer.on_tool_progress("🔍 long running", tool="web_search")
    assert await until(lambda: history.calls, timeout=4)
    frames = api.rich_drafts()
    assert 2 <= len(frames) <= 5                              # first frame + <= 3 keepalives
    assert all(f["can_stop"] is True for f in frames)
    assert history.calls[0][0] == ["🔍 long running"] and history.calls[0][1].startswith("fallback")
    assert consumer.accepts_tool_progress is False            # later lines use the existing queue
    settled = len(api.rich_drafts())
    await asyncio.sleep(0.4)
    assert len(api.rich_drafts()) == settled                  # nothing keeps writing
    consumer.finish("")
    await asyncio.wait_for(task, 3)
    assert len(history.calls) == 1                            # flushed exactly once


@pytest.mark.asyncio
async def test_wall_clock_cap_flushes_even_when_content_keeps_changing(monkeypatch):
    fast(monkeypatch)
    adapter, api = native_adapter()
    history = History(api)
    consumer = make_consumer(adapter, history=history)
    clock = Clock()
    consumer._np_clock = clock
    task = asyncio.create_task(consumer.run())

    consumer.on_tool_progress("🔍 early", tool="web_search")
    assert await until(lambda: api.rich_drafts())
    clock.t += 301
    consumer.on_tool_progress("🔍 later", tool="web_search2")
    assert await until(lambda: history.calls)
    assert history.calls[0][0] == ["🔍 early", "🔍 later"]
    consumer.finish("")
    await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_running_tool_refreshes_elapsed_time(monkeypatch):
    fast(monkeypatch)
    adapter, api = native_adapter()
    consumer = make_consumer(adapter)
    clock = Clock()
    consumer._np_clock = clock
    task = asyncio.create_task(consumer.run())

    consumer.on_tool_progress("🔍 slow search", tool="web_search")
    assert await until(lambda: api.rich_drafts())
    before = len(api.rich_drafts())
    clock.t += 12
    assert await until(lambda: len(api.rich_drafts()) > before)
    head = thinking_of(_markdowns(api)[-1])[0]
    assert "🔍 slow search · 12s" in head and "Running · 12s" in head
    consumer.finish("")
    await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_tool_outcomes_are_only_claimed_when_justified(monkeypatch):
    fast(monkeypatch)
    adapter, api = native_adapter()
    consumer = make_consumer(adapter)
    task = asyncio.create_task(consumer.run())

    consumer.on_tool_progress("🔍 one", tool="web_search")
    consumer.on_tool_complete("web_search", duration=1.5, is_error=True)
    consumer.on_tool_progress("📄 two A", tool="read_file")
    consumer.on_tool_progress("📄 two B", tool="read_file")      # same tool, concurrent
    consumer.on_tool_complete("read_file", duration=0.1, is_error=False)
    assert await until(lambda: any("Failed" in m for m in _markdowns(api)))
    mid = thinking_of(_markdowns(api)[-1])[0]
    assert "<b>Searching the web</b><br>🔍 one<br><i>Failed · 1s</i>" in mid
    assert "<b>Read file</b><br>📄 two A<br><i>Running · 0s</i>" in mid
    assert "<b>Read file</b><br>📄 two B<br><i>Running · 0s</i>" in mid  # ambiguous: no guess
    consumer.on_tool_complete("read_file", duration=0.2, is_error=False)
    assert await until(lambda: "<i>Completed</i>" in thinking_of(_markdowns(api)[-1])[0])
    end = thinking_of(_markdowns(api)[-1])[0]
    assert end.count("<i>Completed</i>") == 2 and "Done" not in end
    consumer.finish("")
    await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_visible_lines_are_handed_to_the_persistent_artifact_before_the_final(monkeypatch):
    fast(monkeypatch)
    adapter, api = native_adapter()
    history = History(api)
    consumer = make_consumer(adapter, history=history)
    task = asyncio.create_task(consumer.run())

    consumer.on_tool_progress("🔍 Searching", tool="web_search")
    consumer.on_commentary("checking the second source")
    consumer.on_tool_progress("💻 terminal", tool="terminal")
    assert await until(lambda: len(api.rich_drafts()) >= 1)
    consumer.on_delta("the answer")
    consumer.finish("the answer")
    await asyncio.wait_for(task, 3)

    [(lines, reason, calls_before)] = history.calls
    assert lines == ["🔍 Searching", format_thought("checking the second source"), "💻 terminal"]
    assert reason == "done"
    final_index = next(i for i, (m, kw) in enumerate(api.calls) if m == "send_message")
    assert calls_before <= final_index                       # artifact first, final after
    # commentary rode the composer, not a separate bubble
    assert not any("checking the second source" in str(kw) for kw in api.methods("send_message"))


@pytest.mark.asyncio
async def test_dedup_replaces_the_last_row_like_the_legacy_bubble(monkeypatch):
    fast(monkeypatch)
    adapter, api = native_adapter()
    history = History(api)
    consumer = make_consumer(adapter, history=history)
    task = asyncio.create_task(consumer.run())
    consumer.on_tool_progress("⚙️ run", tool="execute_code")
    consumer.on_tool_progress("⚙️ run (×2)", tool="execute_code", replace_last=True)
    consumer.on_tool_progress("⚙️ run (×3)", tool="execute_code", replace_last=True)
    consumer.finish("")
    await asyncio.wait_for(task, 3)
    assert history.calls[0][0] == ["⚙️ run (×3)"]


@pytest.mark.asyncio
async def test_capability_failure_falls_back_once_and_keeps_the_answer_flowing(monkeypatch):
    fast(monkeypatch)
    adapter, api = native_adapter()
    api.fail["sendRichMessageDraft"] = type("EndPointNotFound", (Exception,), {})("no such method")
    history = History(api)
    consumer = make_consumer(adapter, history=history)
    task = asyncio.create_task(consumer.run())

    consumer.on_tool_progress("🔍 Searching", tool="web_search")
    assert await until(lambda: history.calls)
    assert history.calls[0][0] == ["🔍 Searching"] and history.calls[0][1].startswith("fallback")
    assert adapter._native_progress_disabled is True
    assert consumer.accepts_tool_progress is False
    consumer.on_delta("legacy preview text")
    assert await until(lambda: api.methods("send_message_draft"))
    consumer.finish("legacy preview text")
    await asyncio.wait_for(task, 3)
    assert len(final_sends(api)) == 1
    assert len(history.calls) == 1


@pytest.mark.asyncio
async def test_oversize_frame_moves_everything_to_the_legacy_artifact(monkeypatch):
    fast(monkeypatch)
    adapter, api = native_adapter()
    history = History(api)
    consumer = make_consumer(adapter, history=history)
    task = asyncio.create_task(consumer.run())
    huge = "📄 " + "y" * 40000
    consumer.on_tool_progress(huge, tool="read_file")
    assert await until(lambda: history.calls)
    assert history.calls[0][0] == [huge]                     # nothing dropped
    assert adapter._native_progress_disabled is False         # size is not a capability failure
    consumer.finish("")
    await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_retry_after_pauses_native_frames_without_falling_back(monkeypatch):
    fast(monkeypatch)
    adapter, api = native_adapter()
    attempts = []

    def flood(kwargs):
        attempts.append(1)
        return RetryAfter(1) if len(attempts) == 1 else None

    api.fail["sendRichMessageDraft"] = flood
    history = History(api)
    consumer = make_consumer(adapter, history=history)
    monkeypatch.setattr(RetryAfter, "retry_after", 0.3, raising=False)
    task = asyncio.create_task(consumer.run())
    consumer.on_tool_progress("🔍 flooded", tool="web_search")
    assert await until(lambda: len(attempts) >= 2, timeout=4)
    assert history.calls == []                                # not a fallback
    assert consumer.accepts_tool_progress is True
    consumer.finish("")
    await asyncio.wait_for(task, 3)
