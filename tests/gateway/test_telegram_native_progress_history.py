"""Persistent history, limits and delivery for the native display (real turn runner).

Same method as ``test_telegram_native_progress_runner``: one scripted event sequence
through today's display (native off) and the native display, comparing what is
actually left in the chat afterwards.
"""

import asyncio
import inspect

import pytest

from tests.gateway.test_telegram_native_progress_runner import (
    FINAL, NAP, R, S, T, bubbles, frames_text, line_multiset, run_turn, unmd,
)


async def fire_cleanup(turn):
    """Run the post-delivery callback the base adapter would run after the final."""
    cb = turn.adapter.pop_post_delivery_callback(turn.session_key)
    assert callable(cb)
    result = cb()
    if inspect.isawaitable(result):
        await result
    for _ in range(100):
        await asyncio.sleep(0.02)
        if any("tool call" in kw["text"] for kw in turn.api.methods("edit_message_text")):
            break


def card_edits(api):
    return [unmd(kw["text"]) for kw in api.methods("edit_message_text") if "tool call" in kw["text"]]


@pytest.mark.asyncio
async def test_cleanup_progress_collapses_the_native_artifact_into_the_same_card_as_today(monkeypatch, tmp_path):
    script = [T, NAP, S, NAP, R, NAP]
    old = await run_turn(monkeypatch, tmp_path, script, native=False, cleanup=True)
    new = await run_turn(monkeypatch, tmp_path, script, native=True, cleanup=True)
    assert new.api.rich_drafts() != []                  # the native display really ran
    await fire_cleanup(old)
    await fire_cleanup(new)

    assert card_edits(new.api) and card_edits(new.api)[-1] == card_edits(old.api)[-1]
    assert "3 tool call" in card_edits(new.api)[-1]
    # nothing but the one collapsed card + the final remains visible from the progress lane
    deleted = {kw["message_id"] for kw in new.api.methods("delete_message")}
    live = [kw for kw in new.api.methods("send_message") if kw["_message_id"] not in deleted]
    assert any(FINAL in kw["text"] for kw in live) and len(live) == 2


@pytest.mark.asyncio
async def test_failed_turn_keeps_the_full_artifact_and_skips_cleanup(monkeypatch, tmp_path):
    script = [T, NAP, S, NAP, ("fail",)]
    old = await run_turn(monkeypatch, tmp_path, script, native=False, cleanup=True)
    new = await run_turn(monkeypatch, tmp_path, script, native=True, cleanup=True)
    assert new.api.rich_drafts() != []                  # the native display really ran
    assert bubbles(new.api) == bubbles(old.api) != []
    cb = new.adapter.pop_post_delivery_callback(new.session_key)
    if callable(cb):
        r = cb()
        if inspect.isawaitable(r):
            await r
    await asyncio.sleep(0.1)
    assert new.api.methods("delete_message") == []        # breadcrumbs stay on failure, as today


@pytest.mark.asyncio
async def test_more_than_128_events_and_long_lines_lose_nothing(monkeypatch, tmp_path):
    many = [("tool", f"tool_{i}", f"item {i}", {"i": i}) for i in range(140)] + [NAP, NAP]
    old = await run_turn(monkeypatch, tmp_path, many, native=False, session="sess-many-old")
    new = await run_turn(monkeypatch, tmp_path, many, native=True, session="sess-many-new")
    assert line_multiset(bubbles(new.api)) == line_multiset(bubbles(old.api))
    assert sum(line_multiset(bubbles(new.api)).values()) == 140
    assert "item 139" in frames_text(new.api)

    # lines too big for ONE rich frame: native steps aside and hands everything to the legacy path
    huge = [("tool", f"big_{i}", "z" * 9000, {"i": i}) for i in range(6)] + [NAP, NAP]
    config = {"tool_preview_length": 0, "tool_progress": "verbose"}
    old2 = await run_turn(monkeypatch, tmp_path, huge, native=False, display=config, session="sess-big-old")
    new2 = await run_turn(monkeypatch, tmp_path, huge, native=True, display=config, session="sess-big-new")
    assert line_multiset(bubbles(new2.api)) == line_multiset(bubbles(old2.api))
    assert new2.adapter._native_progress_disabled is False      # size is not a capability failure


@pytest.mark.asyncio
async def test_final_reply_anchor_and_exactly_once_match_todays_display(monkeypatch, tmp_path):
    script = [S, NAP, R, NAP]
    old = await run_turn(monkeypatch, tmp_path, script, native=False)
    new = await run_turn(monkeypatch, tmp_path, script, native=True)
    assert new.api.rich_drafts() != []                  # the native display really ran
    [old_final] = [kw for kw in old.api.methods("send_message") if FINAL in kw["text"]]
    [new_final] = [kw for kw in new.api.methods("send_message") if FINAL in kw["text"]]
    for key in ("reply_to_message_id", "message_thread_id", "parse_mode", "chat_id"):
        assert new_final.get(key) == old_final.get(key)
    assert "tg-thinking" not in new_final["text"] and "tg-emoji" not in new_final["text"]


@pytest.mark.asyncio
async def test_transient_final_send_failure_never_duplicates_the_final(monkeypatch, tmp_path):
    def flaky(api):
        state = {"failed": False}

        def fail(kwargs):
            if FINAL in kwargs.get("text", "") and not state["failed"]:
                state["failed"] = True
                return RuntimeError("timed out")
            return None

        api.fail["send_message"] = fail

    script = [S, NAP]
    old = await run_turn(monkeypatch, tmp_path, script, native=False, api_setup=flaky, session="sess-flaky-old")
    new = await run_turn(monkeypatch, tmp_path, script, native=True, api_setup=flaky, session="sess-flaky-new")

    def delivered(turn):
        return [kw for kw in turn.api.methods("send_message") if FINAL in kw["text"]]

    # attempts that reached the (in-memory) API are the same in both modes
    assert len(delivered(new)) == len(delivered(old))
    assert new.api.rich_drafts() != []
