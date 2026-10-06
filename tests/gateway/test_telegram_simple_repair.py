"""Native Telegram activity: quiet diagnostics, a still block while the answer
streams, and commentary that is prominent without a wall of bold.

Real ``GatewayRunner._run_agent`` / ``GatewayStreamConsumer`` / ``TelegramAdapter``
and renderer; only the Bot API at the network edge is the repository fake.  These
pin what the gateway emits.  They say nothing about how a phone client paints it.
"""

import asyncio
import os
import re
from collections import namedtuple

import pytest

from gateway.stream_consumer import GatewayStreamConsumer
from plugins.platforms.telegram.native_progress import render_row, render_thinking_block
from tests.gateway.test_telegram_native_progress import (
    Clock, make_consumer, native_adapter, row, until,
)
from tests.gateway.test_telegram_native_progress_runner import FINAL, run_turn, unmd
from tests.gateway.test_telegram_polished_transition import document_wire

LONG_COMMAND = "printf start\n  " + "argument " * 30 + "TAILMARK"


def _private_files(home):
    return sorted((home / "workspace" / "native-activity").glob("*.txt"))


def _persistent_text(turn):
    return "\n".join(unmd(m["text"]) for m in turn.api.persistent_messages())


# ── Quiet diagnostics ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiline_command_turn_uploads_nothing_and_keeps_private_diagnostics(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    turn = await run_turn(monkeypatch, tmp_path, [
        ("tool", "terminal", LONG_COMMAND[:40], {"command": LONG_COMMAND}),
        ("done", "terminal", 0.25, False),
    ], native=True, cleanup=True, api_setup=document_wire, event_message_id="9002", session="quiet-multiline")

    assert not turn.api.methods("send_document")
    shown = _persistent_text(turn)
    assert "attached" not in shown and "unavailable" not in shown and "activity-details" not in shown
    files = _private_files(tmp_path)
    assert len(files) == 1 and "TAILMARK" in files[0].read_text(encoding="utf-8")
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert files[0].parent.stat().st_mode & 0o777 == 0o700
    assert str(tmp_path) not in shown

    calls = turn.api.calls
    card_i = [i for i, (m, kw) in enumerate(calls) if m == "send_message" and "tool call" in kw.get("text", "")]
    final_i = [i for i, (m, kw) in enumerate(calls) if turn.api.is_answer_call(m, kw, FINAL)]
    assert len(card_i) == 1 and len(final_i) == 1 and card_i[0] < final_i[0]


@pytest.mark.asyncio
async def test_short_turn_keeps_private_diagnostics_too(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    turn = await run_turn(monkeypatch, tmp_path, [
        ("tool", "terminal", "pwd", {"command": "pwd"}),
        ("done", "terminal", 0.25, False),
    ], native=True, cleanup=True, api_setup=document_wire, session="quiet-short")

    files = _private_files(tmp_path)
    assert len(files) == 1 and "pwd" in files[0].read_text(encoding="utf-8")
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert not turn.api.methods("send_document")


@pytest.mark.asyncio
async def test_history_without_a_summary_card_shows_only_what_the_draft_showed(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    turn = await run_turn(monkeypatch, tmp_path, [
        ("tool", "terminal", LONG_COMMAND[:40], {"command": LONG_COMMAND}),
        ("done", "terminal", 0.25, False),
    ], native=True, cleanup=False, api_setup=document_wire, session="quiet-history")

    shown = _persistent_text(turn)
    assert "printf start" in shown, "the visible activity line is still kept"
    assert "TAILMARK" not in shown, "raw diagnostics are private, never a public fallback"
    assert not turn.api.methods("send_document")
    files = _private_files(tmp_path)
    assert len(files) == 1 and "TAILMARK" in files[0].read_text(encoding="utf-8")
    assert sum(FINAL in m["text"] for m in turn.api.persistent_messages()) == 1


@pytest.mark.asyncio
async def test_diagnostics_write_failure_never_blocks_the_summary_or_the_answer(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    import gateway.native_progress as native_progress

    def refuse(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(native_progress, "retain_private_diagnostics", refuse)
    turn = await run_turn(monkeypatch, tmp_path, [
        ("tool", "terminal", "pwd", {"command": "pwd"}),
        ("done", "terminal", 0.25, False),
    ], native=True, cleanup=True, api_setup=document_wire, session="quiet-write-failure")

    messages = turn.api.persistent_messages()
    assert sum("tool call" in m["text"] for m in messages) == 1
    assert sum(FINAL in m["text"] for m in messages) == 1
    assert not turn.api.methods("send_document")


def test_private_diagnostics_are_bounded_to_the_newest_files(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    from gateway.native_progress import retain_private_diagnostics

    directory = tmp_path / "workspace" / "native-activity"
    directory.mkdir(parents=True, mode=0o755)
    os.chmod(directory, 0o755)
    for i in range(6):
        old = directory / f"old{i}.txt"
        old.write_text("old")
        os.utime(old, (1000 + i, 1000 + i))

    path = retain_private_diagnostics(["first line", "second line"], keep=4)

    assert path is not None and path.read_text(encoding="utf-8") == "first line\n\nsecond line\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    kept = {p.name for p in directory.glob("*.txt")}
    assert kept == {path.name, "old5.txt", "old4.txt", "old3.txt"}
    assert retain_private_diagnostics([]) is None and retain_private_diagnostics(["  "]) is None


# ── A still activity block while the answer streams ─────────────────────────

Frame = namedtuple("Frame", "block answer")


def _last_frame(api):
    text = api.rich_text(api.rich_drafts()[-1])
    block, _, answer = text.partition("</tg-thinking>")
    return Frame(block, answer.strip())


def _header(block):
    return block.split("<br>")[0]


@pytest.mark.asyncio
async def test_answer_growth_changes_only_the_answer_and_boundaries_release_the_clock():
    adapter, api = native_adapter()
    adapter._native_icons_disabled = True
    adapter._native_icons_nowait = lambda: {}
    clock = Clock()
    consumer = make_consumer(adapter)
    consumer._np_clock = clock
    GatewayStreamConsumer._draft_id_counter = 9100
    task = asyncio.create_task(consumer.run())

    async def settle():
        await asyncio.sleep(0.13)
        await consumer._np_pump()
        await asyncio.sleep(0.01)
        await consumer._np_pump()

    try:
        assert await until(lambda: api.rich_drafts() and consumer._np_task is None)
        clock.t += 1.1
        consumer.on_tool_progress("💭 Checking the source.")
        consumer.on_commentary("Checking the public source.")
        consumer.on_tool_progress("Reading source.md", tool="read_file")
        await settle()
        working = _last_frame(api)
        clock.t += 6.0
        await settle()
        assert _last_frame(api).block != working.block, "control: timers are live while only work is shown"

        growth = []
        for text in ("Alpha", " beta", " gamma"):
            clock.t += 1.1
            consumer.on_delta(text)
            await settle()
            growth.append(_last_frame(api))
        clock.t += 15.0                     # idle refresh keeps the draft alive
        await settle()
        growth.append(_last_frame(api))
        assert [f.answer for f in growth] == ["Alpha", "Alpha beta", "Alpha beta gamma", "Alpha beta gamma"]
        assert len({f.block for f in growth}) == 1, "the activity block must not change while only the answer grows"

        clock.t += 1.1                      # real new activity still shows; the clock stays put
        consumer.on_commentary("One more public note.")
        await settle()
        noted = _last_frame(api)
        assert "One more public note." in noted.block and noted.answer == "Alpha beta gamma"
        assert _header(noted.block) == _header(growth[0].block)

        clock.t += 1.1                      # tool boundary: back to work, live clock again
        consumer.on_segment_break(interim=True)
        await settle()
        clock.t += 6.0
        await settle()
        resumed = _last_frame(api)
        assert resumed.answer == "" and _header(resumed.block) != _header(growth[0].block)

        frames = api.rich_drafts()
        assert len({f["draft_id"] for f in frames}) == 1
        assert all(f["can_stop"] is True for f in frames)
    finally:
        consumer.finish("Alpha beta gamma")
        try:
            await asyncio.wait_for(task, 3)
        except Exception:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


# ── Prominent commentary without a wall of bold ─────────────────────────────


def _bold_runs(markup):
    return re.findall(r"<b>(.*?)</b>", markup)


def test_only_the_newest_public_update_is_bold_and_tool_rows_are_untouched():
    tool = row("Searching sony reviews", state="succeeded", duration=0.2)
    rows = [
        row("First update.", kind="commentary"),
        tool,
        row("Weighing **two** options <carefully>.", kind="thought"),
        row("Third update.", kind="commentary"),
    ]
    block = render_thinking_block(rows, now=10.0, turn_started_at=0.0)

    bold = _bold_runs(block)
    assert len(bold) == 1 and "Third update." in bold[0]
    assert "First update." in block and "Weighing" in block and "options &lt;carefully&gt;." in block
    assert "**" not in block, "author emphasis markers never leak as literal stars"
    assert render_row(tool, now=10.0) in block, "tool rows render exactly as before"
    # The same paragraphs, in the same order, with the same spacing.
    assert block.index("First update.") < block.index("Weighing") < block.rindex("Third update.")
    assert block.count("<br><br>") == 4


def test_a_single_public_update_stays_bold_and_render_row_default_is_unchanged():
    only = row("Checking the configuration.", kind="thought")
    block = render_thinking_block([only], now=3.0, turn_started_at=0.0)
    assert _bold_runs(block) == ["💭 Checking the configuration."]
    assert render_row(only) == "<b>💭 Checking the configuration.</b>"
    assert render_row(only, emphasize=False) == "💭 Checking the configuration."
