"""Thoughts-first renderer + native lifecycle, combined, on the fake Bot API wire.

Every turn here drives the REAL ``GatewayRunner._run_agent`` and the REAL
``TelegramAdapter`` over the in-memory Bot API connector, so the rendered
activity block is the one the renderer produced from the ledger and clock the
lifecycle supplied.  Each test records the frames it asserted on as junit
properties (``wire_*``) so the exact bytes can be read back from the XML.
"""

import html
import json
import re

import pytest

from tests.gateway.test_telegram_native_progress_runner import FINAL, private_diagnostics, run_turn

T = ("tool", "terminal", "pwd", {"command": "pwd"})
S = ("tool", "web_search", "sony reviews", {"query": "sony reviews"})
NAP = ("sleep", 0.5)
ELAPSED = re.compile(r"(<1s|\d+s|\d+m(?: \d+s)?)$")


def visible(markup):
    return html.unescape(re.sub(r"<[^>]+>", "", markup.replace("<br>", "\n")))


def blocks(api):
    """Visible activity block of every native draft frame, in wire order."""
    return [visible(api.rich_text(f).split("</tg-thinking>")[0]) for f in api.rich_drafts()]


def answers(api):
    """Visible answer text carried beside the activity block, per frame."""
    return [visible(api.rich_text(f).partition("</tg-thinking>")[2]).strip() for f in api.rich_drafts()]


def seconds(header):
    found = ELAPSED.search(header.strip())
    assert found, header
    text = found.group(1)
    if text == "<1s":
        return 0
    minutes = re.match(r"(\d+)m(?: (\d+)s)?", text)
    if minutes:
        return int(minutes.group(1)) * 60 + int(minutes.group(2) or 0)
    return int(text[:-1])


def persisted(api):
    return [kw["text"] for kw in api.persistent_messages()]


def diagnostics():
    """Full per-call diagnostics: retained privately, never posted to the chat."""
    return "\n".join(private_diagnostics().values())


def record(request, name, value):
    request.node.user_properties.append(
        (name, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    )


def record_wire(request, turn):
    api = turn.api
    record(request, "wire_methods", [m for m, _ in api.calls])
    record(request, "wire_draft_ids", sorted({str(f.get("draft_id")) for f in api.rich_drafts()}))
    record(request, "wire_draft_frames_html", [api.rich_text(f) for f in api.rich_drafts()])
    record(request, "wire_persistent_messages", persisted(api))


@pytest.mark.asyncio
async def test_header_elapsed_keeps_the_turn_origin_across_tool_handoffs(monkeypatch, tmp_path, request):
    turn = await run_turn(
        monkeypatch, tmp_path,
        [T, ("sleep", 1.2), ("done", "terminal", 1.2, False), S, ("sleep", 1.2), ("done", "web_search", 1.2, False),
         T, NAP],
        native=True,
    )
    record_wire(request, turn)
    shown = blocks(turn.api)
    assert shown
    elapsed = [seconds(block.split("\n")[0]) for block in shown]
    record(request, "wire_header_elapsed_seconds", elapsed)

    assert elapsed == sorted(elapsed), "the header clock ran backwards"
    # Frames drawn after the third call started are >2.4s into the turn; a clock
    # that restarted with the newest tool would read under a second there.
    after_handoff = [e for e, block in zip(elapsed, shown) if block.count("Terminal") == 2]
    assert after_handoff and min(after_handoff) >= 2
    assert "0s" not in "\n".join(b.split("\n")[0] for b in shown).replace("10s", "")


@pytest.mark.asyncio
async def test_identical_calls_are_distinct_rows_without_a_legacy_repeat_counter(monkeypatch, tmp_path, request):
    turn = await run_turn(monkeypatch, tmp_path, [S, NAP, S, NAP, S, NAP], native=True)
    record_wire(request, turn)
    rows = blocks(turn.api)[-1].split("\n\n", 1)[1].split("\n")

    assert len(rows) == 3 and all("sony reviews" in row for row in rows)
    assert "(×" not in "\n".join(blocks(turn.api))
    history = html.unescape("\n".join(persisted(turn.api))).replace("\\", "")
    assert [i for i in (1, 2, 3) if f"web_search [script-{i}]" in diagnostics()] == [1, 2, 3]
    assert "(×" not in history and "[script-" not in history


@pytest.mark.asyncio
async def test_long_command_is_a_bounded_preview_live_and_whole_in_the_diagnostics(monkeypatch, tmp_path, request):
    command = " && ".join(f"python -c \"print('configuration-check-{i}')\"" for i in range(8))
    turn = await run_turn(
        monkeypatch, tmp_path,
        [("tool", "terminal", command, {"command": command}), NAP, ("done", "terminal", 2.25, False), NAP],
        native=True,
    )
    record_wire(request, turn)
    rows = [line for line in blocks(turn.api)[-1].split("\n")[1:] if "configuration-check" in line]
    assert len(rows) == 1
    detail = rows[0].removeprefix("Terminal ").split(" · ")[0]
    record(request, "wire_row_visible", rows[0])
    assert len(command) > 300 and 0 < len(detail) <= 80
    assert rows[0].endswith(" · Done · 2s")
    assert "configuration-check-7" not in "\n".join(blocks(turn.api))

    history = diagnostics()
    for i in range(8):
        assert f"configuration-check-{i}" in history
    assert "terminal [script-1]" in history and "2.25s" in history
    assert "[script-" not in html.unescape("\n".join(persisted(turn.api)))


@pytest.mark.asyncio
async def test_credentials_and_gateway_private_artifacts_never_reach_the_wire(monkeypatch, tmp_path, request):
    from clover_constants import get_clover_home

    private = get_clover_home() / "workspace" / "native-activity" / "0123abcd.txt"
    command = (
        "curl -H 'Authorization: Bearer sk-live-abcdef0123456789abcdef' https://example.test"
        f" > /srv/reports/out.txt; cat {private}"
    )
    args = {"command": command, "api_key": "sk-live-abcdef0123456789abcdef"}
    turn = await run_turn(
        monkeypatch, tmp_path,
        [("tool", "terminal", command, args), NAP, ("done", "terminal", 0.4, True), NAP],
        native=True,
    )
    record_wire(request, turn)
    everything = json.dumps([kw for _, kw in turn.api.calls], default=str)

    assert "sk-live-abcdef0123456789abcdef" not in everything
    assert "0123abcd" not in everything and "native-activity" not in everything
    assert "SECRET-RESULT-PAYLOAD" not in everything
    assert "Failed" in blocks(turn.api)[-1]
    history = diagnostics()
    assert "sk-live-abcdef0123456789abcdef" not in history and "SECRET-RESULT-PAYLOAD" not in history
    assert "terminal [script-1] · failed" in history
    # the operand the command itself named is the permitted raw detail
    assert "> /srv/reports/out.txt; cat [gateway-private]" in history


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["stream_tool", "stream_no_tool", "nonstream_tool", "status_only", "delegation"])
async def test_one_draft_then_exactly_one_final_answer(monkeypatch, tmp_path, request, shape):
    script = {
        "stream_tool": [S, NAP, ("done", "web_search", 0.5, False), ("delta", "Partial "), NAP, ("delta", "answer "), NAP],
        "stream_no_tool": [("delta", "Partial "), NAP, ("delta", "answer "), NAP],
        "nonstream_tool": [S, NAP, ("done", "web_search", 0.5, False), NAP],
        "status_only": [NAP, NAP],
        "delegation": [
            ("commentary", "Handing the research to a worker."),
            ("tool", "delegate_task", "research", {"goal": "research"}), NAP,
            ("done", "delegate_task", 0.5, False), NAP,
        ],
    }[shape]
    turn = await run_turn(
        monkeypatch, tmp_path, script, native=True, interim=shape == "delegation",
        send_final_delta=shape != "nonstream_tool",
    )
    record_wire(request, turn)
    api = turn.api

    finals = [text for text in persisted(api) if FINAL in text]
    assert len(finals) == 1 and "tg-thinking" not in finals[0]
    assert api.methods("send_message_draft") == [], "the legacy plain draft slot was used"
    assert len({str(f.get("draft_id")) for f in api.rich_drafts()}) <= 1
    assert all(f["can_stop"] is True for f in api.rich_drafts())
    assert "<br><br><br>" not in "".join(api.rich_text(f) for f in api.rich_drafts())

    grown = [a for a in answers(api) if a]
    record(request, "wire_answer_growth", grown)
    if shape.startswith("stream"):
        assert any(a.startswith("Partial") for a in grown), "provider deltas never reached the draft"
        assert all(later.startswith(earlier) or earlier.startswith(later) for earlier, later in zip(grown, grown[1:]))
    if shape == "nonstream_tool":
        assert not grown, "an answer was animated without provider deltas"
    if shape == "delegation":
        assert "Handing the research to a worker." in "\n".join(blocks(api))
        assert not any("Handing the research" in a for a in grown), "commentary was shown as the answer"
    if shape != "stream_no_tool":
        # the summary/history card is delivered before the answer, never after
        order = persisted(api)
        assert order.index(finals[0]) == len(order) - 1
