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
    return cb


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

    old_card = card_edits(old.api)[-1]
    native_cards = [
        unmd(kw["text"])
        for kw in new.api.methods("send_message")
        if "tool call" in kw["text"] and FINAL not in kw["text"]
    ]
    assert native_cards and native_cards[-1].split("\n")[0].removesuffix("||") == old_card.split("\n")[0].removesuffix("||")
    assert "pwd" in native_cards[-1] and "sony reviews" in native_cards[-1] and "notes.md" in native_cards[-1]
    assert "3 tool call" in native_cards[-1]
    # nothing but the one collapsed card + the final remains visible from the progress lane
    deleted = {kw["message_id"] for kw in new.api.methods("delete_message")}
    live = [kw for kw in new.api.persistent_messages() if kw["_message_id"] not in deleted]
    assert any(FINAL in kw["text"] for kw in live) and len(live) == 2


@pytest.mark.asyncio
async def test_gateway_thought_prefix_renders_as_natural_commentary_in_real_native_frames(monkeypatch, tmp_path):
    turn = await run_turn(
        monkeypatch, tmp_path,
        [("thought", "The time source is consistent."), NAP, S, NAP],
        native=True, session="sess-real-thought-wrapper",
    )
    frames = [turn.api.rich_text(frame) for frame in turn.api.rich_drafts()]
    assert frames, "the production gateway-to-Telegram native path did not emit a frame"
    frame = frames[-1]
    assert "💭 The time source is consistent." in frame
    assert "💭 <i>The time source is consistent.</i>" not in frame


@pytest.mark.asyncio
async def test_native_activity_blocks_have_clear_spacing_and_one_terminal_identity(monkeypatch, tmp_path):
    turn = await run_turn(
        monkeypatch, tmp_path,
        [("thought", "Checking the source."), NAP, T, NAP],
        native=True, session="sess-native-visual-hierarchy",
    )
    frames = [turn.api.rich_text(frame) for frame in turn.api.rich_drafts()]
    assert frames
    frame = frames[-1]
    assert "pwd" in frame
    assert frame.count("Terminal") == 1
    assert "pwd" in frame
    assert "<br><br>" in frame, "separate activity blocks need a visible gap"


@pytest.mark.asyncio
async def test_native_summary_is_sent_before_final_answer_without_legacy_flash(monkeypatch, tmp_path):
    turn = await run_turn(
        monkeypatch, tmp_path, [T, NAP, S, NAP, R, NAP], native=True,
        cleanup=True, session="sess-summary-before-final",
    )
    calls = turn.api.calls
    summary_i = next(
        (i for i, (method, kw) in enumerate(calls)
         if method == "send_message" and "tool call" in kw.get("text", "")),
        None,
    )
    final_i = next(
        i for i, (method, kw) in enumerate(calls)
        if turn.api.is_answer_call(method, kw, FINAL)
    )
    draft_indices = [
        i for i, (method, _) in enumerate(calls)
        if method == "do_api_request:sendRichMessageDraft"
    ]
    assert draft_indices, "the production rich-draft transport was not exercised"
    assert summary_i is not None
    assert draft_indices[0] < summary_i < final_i
    assert not any(
        method == "send_message" and FINAL not in kw.get("text", "")
        and "tool call" not in kw.get("text", "")
        for method, kw in calls[:summary_i]
    )
    callback = await fire_cleanup(turn)
    assert callable(callback)
    repeated = callback()
    if inspect.isawaitable(repeated):
        await repeated
    await asyncio.sleep(0.1)
    assert sum(
        method == "send_message" and "tool call" in kw.get("text", "")
        for method, kw in turn.api.calls
    ) == 1, "re-running delayed cleanup must not send the summary twice"


@pytest.mark.asyncio
async def test_pre_final_summary_survives_final_delivery_failure(monkeypatch, tmp_path):
    def fail_final(api):
        api.fail["send_message"] = lambda kw: RuntimeError("final transport unavailable") if FINAL in kw.get("text", "") else None
        api.fail["sendRichMessage"] = lambda kw: RuntimeError("final transport unavailable") if FINAL in api.rich_text(kw["api_kwargs"]) else None

    turn = await run_turn(
        monkeypatch, tmp_path, [T, NAP, S, NAP], native=True, cleanup=True,
        api_setup=fail_final, session="sess-summary-final-failure",
    )
    await fire_cleanup(turn)
    summaries = [
        kw for kw in turn.api.methods("send_message")
        if "tool call" in kw.get("text", "")
    ]
    failures = [
        kw for kw in turn.api.persistent_messages()
        if FINAL in kw.get("text", "")
    ]
    assert summaries, "history must persist even when final delivery fails"
    assert len(summaries) == 1, "cleanup must not duplicate the pre-delivery summary"
    assert failures, "the injected final-delivery failure was not exercised"
    summary_i = turn.api.calls.index(("send_message", summaries[0]))
    failure_i = next(i for i, (method, kw) in enumerate(turn.api.calls) if turn.api.is_answer_call(method, kw, FINAL))
    assert summary_i < failure_i, "the persisted history must precede the failed final attempt"


@pytest.mark.asyncio
async def test_pre_delivery_summary_failure_uses_existing_persistent_history_fallback(monkeypatch, tmp_path):
    def fail_summary(api):
        api.fail["send_message"] = lambda kw: RuntimeError("summary transport unavailable") if "tool call" in kw.get("text", "") else None

    turn = await run_turn(
        monkeypatch, tmp_path, [T, NAP, S, NAP], native=True, cleanup=True,
        api_setup=fail_summary, session="sess-summary-fallback",
    )
    final_i = next(
        i for i, (method, kw) in enumerate(turn.api.calls)
        if turn.api.is_answer_call(method, kw, FINAL)
    )
    assert any(
        method == "send_message" and FINAL not in kw.get("text", "")
        for method, kw in turn.api.calls[:final_i]
    ), "failed summary delivery must retain the old persistent activity fallback"
    await fire_cleanup(turn)
    assert sum(
        method == "send_message" and "tool call" in kw.get("text", "")
        for method, kw in turn.api.calls
    ) == 1




@pytest.mark.asyncio
async def test_successful_native_cleanup_skips_transient_legacy_tool_bubble(monkeypatch, tmp_path):
    turn = await run_turn(
        monkeypatch, tmp_path, [T, NAP, S, NAP, R, NAP], native=True, cleanup=True,
        session="sess-native-cleanup-no-flash",
    )
    assert turn.api.rich_drafts()  # fixture exercised the actual native draft path
    final_index = next(
        i for i, (method, kw) in enumerate(turn.api.calls)
        if turn.api.is_answer_call(method, kw, FINAL)
    )
    summaries = [
        (method, kw.get("text", ""))
        for method, kw in turn.api.calls[:final_index]
        if method == "send_message" and "tool call" in kw.get("text", "")
    ]
    legacy_activity = [
        (method, kw.get("text", ""))
        for method, kw in turn.api.calls[:final_index]
        if method == "send_message" and FINAL not in kw.get("text", "")
        and "tool call" not in kw.get("text", "")
    ]
    assert len(summaries) == 1
    assert legacy_activity == []

    await fire_cleanup(turn)
    visible_messages = [kw["text"] for kw in turn.api.persistent_messages()]
    assert any("tool call" in text for text in visible_messages)
    assert any(FINAL in text for text in visible_messages)


@pytest.mark.asyncio
async def test_native_summary_leaves_detached_worker_cards_unabsorbed(monkeypatch, tmp_path):
    from gateway import delegation_activity

    class DetachedWorkers:
        combined = True

        def __init__(self):
            self.absorb_calls = 0

        def observe(self, *args, **kwargs):
            pass

        def adopt_inbox(self):
            return False

        def absorb_finished(self):
            self.absorb_calls += 1
            return ([object()], ["detached-worker-card"])

        def end_turn(self):
            pass

    publisher = DetachedWorkers()
    monkeypatch.setattr(delegation_activity, "build_turn_publisher", lambda *args: publisher)
    turn = await run_turn(
        monkeypatch, tmp_path, [T, NAP, S, NAP], native=True, cleanup=True,
        session="sess-detached-worker-cards",
    )
    assert publisher.absorb_calls == 0, "the pre-final main summary must not consume a detached worker card"
    await fire_cleanup(turn)
    assert sum("tool call" in kw.get("text", "") for kw in turn.api.methods("send_message")) == 1
    assert not any(
        kw.get("message_id") == "detached-worker-card"
        for method, kw in turn.api.calls
        if method in {"edit_message_text", "delete_message"}
    ), "cleanup must leave detached worker cards separate and persistent"


@pytest.mark.asyncio
async def test_pre_final_summary_deletes_tracked_carrier_without_second_card(monkeypatch, tmp_path):
    from gateway import delegation_activity

    class AdoptedWorkers:
        combined = True
        def __init__(self):
            self.absorbed = 0
        def adopt_inbox(self):
            return True
        def adopted_preview(self):
            return 1, []
        def absorb_finished(self):
            self.absorbed += 1
            return [], []
        def end_turn(self):
            return None

    pub = AdoptedWorkers()
    monkeypatch.setattr(delegation_activity, "build_turn_publisher", lambda *args: pub)
    turn = await run_turn(
        monkeypatch, tmp_path, [T, NAP, S, NAP], native=True, cleanup=True,
        session="sess-summary-tracked-carrier",
    )
    carrier = next(kw for method, kw in turn.api.calls if method == "send_message" and "finished" in kw.get("text", ""))
    summary_count_before = sum(
        method == "send_message" and "tool call" in kw.get("text", "")
        for method, kw in turn.api.calls
    )
    assert summary_count_before == 1
    summary_i = next(
        i for i, (method, kw) in enumerate(turn.api.calls)
        if method == "send_message" and "tool call" in kw.get("text", "")
    )
    final_i = next(
        i for i, (method, kw) in enumerate(turn.api.calls)
        if turn.api.is_answer_call(method, kw, FINAL)
    )
    assert summary_i < final_i
    await fire_cleanup(turn)

    summaries = [
        (method, kw) for method, kw in turn.api.calls
        if "tool call" in kw.get("text", "")
    ]
    assert len(summaries) == 1, "tracked temporary IDs must not create a second collapsed summary"
    assert not card_edits(turn.api), "the adopted carrier must be deleted, not rewritten as another card"
    assert carrier["_message_id"] in {
        kw["message_id"] for kw in turn.api.methods("delete_message")
    }
    assert pub.absorbed == 0


@pytest.mark.asyncio
async def test_cleanup_does_not_create_a_card_for_log_only_legacy_turn(monkeypatch, tmp_path):
    turn = await run_turn(
        monkeypatch, tmp_path, [T, NAP, S, NAP, R, NAP],
        native=False, cleanup=True, display={"tool_progress": "log"},
        session="sess-log-only-cleanup",
    )
    await fire_cleanup(turn)
    assert bubbles(turn.api) == []
    assert sum(FINAL in kw["text"] for kw in turn.api.methods("send_message")) == 1


@pytest.mark.asyncio
async def test_failed_turn_keeps_the_full_artifact_and_skips_cleanup(monkeypatch, tmp_path):
    script = [T, NAP, S, NAP, ("fail",)]
    old = await run_turn(monkeypatch, tmp_path, script, native=False, cleanup=True)
    new = await run_turn(monkeypatch, tmp_path, script, native=True, cleanup=True)
    assert new.api.rich_drafts() != []                  # the native display really ran
    assert bubbles(new.api) and bubbles(old.api)
    raw = unmd("\n".join(bubbles(new.api)))
    assert "pwd" in raw and "sony reviews" in raw      # the lines the draft showed stay
    assert "[script-" not in raw                        # raw diagnostics are never posted
    from tests.gateway.test_telegram_native_progress_runner import private_diagnostics
    private = "\n".join(private_diagnostics().values())
    assert "terminal [script-1]" in private and "web_search [script-2]" in private
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
    from tests.gateway.test_telegram_native_progress_runner import private_diagnostics
    first = private_diagnostics()
    raw = "\n".join(first.values())
    assert raw.count("unknown duration") == 140
    assert all(f"tool_{i} [script-{i+1}]" in raw for i in range(140))
    shown = unmd("\n".join(bubbles(new.api)))
    assert all(f"item {i}" in shown for i in range(140)) and "[script-" not in shown
    assert bubbles(old.api)  # legacy control is still exercised
    assert "item 139" in frames_text(new.api)

    # lines too big for ONE rich frame: native steps aside and hands everything to the legacy path
    huge = [("tool", f"big_{i}", "z" * 9000, {"i": i}) for i in range(6)] + [NAP, NAP]
    config = {"tool_preview_length": 0, "tool_progress": "verbose"}
    old2 = await run_turn(monkeypatch, tmp_path, huge, native=False, display=config, session="sess-big-old")
    new2 = await run_turn(monkeypatch, tmp_path, huge, native=True, display=config, session="sess-big-new")
    raw2 = "\n".join(text for name, text in private_diagnostics().items() if name not in first)
    assert raw2.count("unknown duration") == 6
    assert unmd("\n".join(bubbles(new2.api))).count("z" * 100) >= 6     # visible lines still handed over
    assert all(f'"i": {i}' in raw2 for i in range(6))
    assert bubbles(old2.api)
    assert new2.adapter._native_progress_disabled is False      # size is not a capability failure


@pytest.mark.asyncio
async def test_final_reply_anchor_and_exactly_once_match_todays_display(monkeypatch, tmp_path):
    script = [S, NAP, R, NAP]
    old = await run_turn(monkeypatch, tmp_path, script, native=False)
    new = await run_turn(monkeypatch, tmp_path, script, native=True)
    assert new.api.rich_drafts() != []                  # the native display really ran
    [old_final] = [kw for kw in old.api.methods("send_message") if FINAL in kw["text"]]
    [new_final] = [kw for kw in new.api.persistent_messages() if FINAL in kw["text"]]
    for key in ("reply_to_message_id", "message_thread_id", "chat_id"):
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
        api.fail["sendRichMessage"] = lambda kw: fail({"text": api.rich_text(kw["api_kwargs"])})

    script = [S, NAP]
    old = await run_turn(monkeypatch, tmp_path, script, native=False, api_setup=flaky, session="sess-flaky-old")
    new = await run_turn(monkeypatch, tmp_path, script, native=True, api_setup=flaky, session="sess-flaky-new")

    def delivered(turn):
        return [kw for kw in turn.api.persistent_messages() if FINAL in kw["text"]]

    # attempts that reached the (in-memory) API are the same in both modes
    assert len(delivered(new)) == len(delivered(old))
    assert new.api.rich_drafts() != []
