"""Thoughts-first layout of the native Telegram activity block.

Public thoughts/commentary are the primary text; tool actions are compact
one-line secondary rows; the phase header counts from the immutable turn
start.  Everything here drives the pure render seam
(``plugins.platforms.telegram.native_progress``) with the gateway's real
``ActivityLedger`` rows and gateway-shaped progress lines.
"""

import html
import inspect
import re

from agent.display import get_tool_emoji, get_tool_verb, tool_verb_connector
from gateway.native_progress import ActivityLedger
from plugins.platforms.telegram.native_progress import (
    compose_markdown,
    render_row,
    render_thinking_block,
)


def gateway_line(tool, preview):
    """The progress line the gateway shows for a built-in tool call."""
    emoji = get_tool_emoji(tool, default="⚙️")
    return f"{emoji} {get_tool_verb(tool)}{tool_verb_connector(tool)}{preview}"


def started(ledger, tool, preview, *, now):
    return ledger.add_line(gateway_line(tool, preview), tool=tool, now=now)


def visible(markup):
    """What a reader sees: tags dropped, entities decoded, <br> as newline."""
    return html.unescape(re.sub(r"<[^>]+>", "", markup.replace("<br>", "\n")))


def header_of(block):
    return block.removeprefix("<tg-thinking>").partition("<br>")[0]


def _with_turn_start(fn, *args, turn_started_at, **kwargs):
    """Supply the turn origin through the seam when the renderer accepts it.

    A renderer without the seam is exercised through its only entry point, so
    the assertions below fail on the header it really renders.
    """
    if "turn_started_at" in inspect.signature(fn).parameters:
        kwargs["turn_started_at"] = turn_started_at
    return fn(*args, **kwargs)


# ── Public thought / commentary is the primary text ─────────────────────────


def test_public_thought_is_emphasized_bold_without_italics_or_heading():
    ledger = ActivityLedger()
    ledger.add_line("💭 _Checking the source._", kind="commentary", now=1.0)
    ledger.add_line("The clocks agree.", kind="thought", now=2.0)
    legacy, plain = (render_row(r) for r in ledger.snapshot())

    assert legacy == "<b>💭 Checking the source.</b>"
    assert plain == "<b>💭 The clocks agree.</b>"
    assert "<i>" not in legacy + plain
    assert "thought" not in visible(legacy + plain).lower()


def test_thought_keeps_words_order_and_inline_code_outside_the_bold_run():
    ledger = ActivityLedger()
    ledger.add_line(
        "Use `x ** y` then **ship** it <now> & rest", kind="commentary", now=1.0,
    )
    rendered = render_row(ledger.snapshot()[0])

    assert rendered == (
        "<b>Use </b><code>x ** y</code><b> then ship it &lt;now&gt; &amp; rest</b>"
    )
    assert visible(rendered) == "Use x ** y then ship it <now> & rest"
    # Telegram code entities cannot nest inside other entities.
    assert not re.search(r"<b>[^<]*<code>", rendered)


def test_multiline_thought_emphasizes_each_line_without_a_tag_spanning_a_break():
    ledger = ActivityLedger()
    ledger.add_line(
        "First I check the clock.\nThen </tg-thinking> the zone.",
        kind="thought", now=1.0,
    )
    rendered = render_row(ledger.snapshot()[0])

    assert "\n" not in rendered
    assert rendered.split("<br>") == [
        "<b>💭 First I check the clock.</b>",
        "<b>Then &lt;/tg-thinking&gt; the zone.</b>",
    ]


# ── Tool actions are compact secondary rows ─────────────────────────────────


def test_tool_action_is_one_line_with_a_friendly_label_and_concise_state():
    ledger = ActivityLedger()
    started(ledger, "web_search", "local time", now=0.0)
    ledger.complete_tool("web_search", duration=2.4, is_error=False)
    rendered = render_row(ledger.snapshot()[0], now=5.0)

    assert rendered == "Searching the web for local time · <i>Done · 2s</i>"


def test_short_multiline_command_folds_onto_one_row_intact():
    ledger = ActivityLedger()
    emoji = get_tool_emoji("terminal", default="⚙️")
    ledger.add_line(
        f"{emoji} Running command\n```bash\ncd /srv && ls\necho a || echo <b>\n```",
        tool="terminal", now=0.0,
    )
    rendered = render_row(ledger.snapshot()[0], now=3.0)

    assert "<br>" not in rendered and "\n" not in rendered
    assert "<code>cd /srv &amp;&amp; ls</code>" in rendered
    assert "<code>echo a || echo &lt;b&gt;</code>" in rendered
    assert rendered.index("cd /srv") < rendered.index("echo a")
    assert visible(rendered).startswith("Terminal ")


def test_clipped_source_preview_is_secondary_code_not_the_primary_label():
    ledger = ActivityLedger()
    started(ledger, "execute_code", "from pathlib import Path W=Path('/home/…')", now=0.0)
    rendered = render_row(ledger.snapshot()[0], now=0.2)

    assert rendered.startswith("Execute code <code>from pathlib import Path")
    assert "<b>" not in rendered
    assert "<br>" not in rendered


def test_failure_is_the_only_emphasized_tool_state():
    ledger = ActivityLedger()
    started(ledger, "web_search", "one", now=0.0)
    ledger.complete_tool("web_search", duration=1.5, is_error=True)
    started(ledger, "read_file", "notes.md", now=2.0)
    ledger.complete_tool("read_file", duration=1.2, is_error=False)
    failed, done = (render_row(r, now=4.0) for r in ledger.snapshot())

    assert failed.endswith(" · <b>Failed · 1s</b>")
    assert "<b>" not in done and done.endswith(" · <i>Done · 1s</i>")


def test_tiny_completed_tool_omits_its_duration():
    ledger = ActivityLedger()
    started(ledger, "read_file", "notes.md", now=0.0)
    ledger.complete_tool("read_file", duration=0.2, is_error=False)
    rendered = render_row(ledger.snapshot()[0], now=4.0)

    assert rendered.endswith(" · <i>Done</i>")
    assert "0s" not in rendered and "1s" not in rendered


def test_running_tool_under_a_second_is_reported_honestly():
    ledger = ActivityLedger()
    started(ledger, "web_search", "local time", now=10.0)
    rendered = render_row(ledger.snapshot()[0], now=10.3)

    assert rendered.endswith(" · <i>&lt;1s</i>")
    assert "0s" not in rendered


def overlapping_reads(*outcomes):
    ledger = ActivityLedger()
    started(ledger, "read_file", "a.md", now=0.0)
    started(ledger, "read_file", "b.md", now=0.1)        # same name, overlapping
    for duration, is_error in zip((3.0, 4.0), outcomes):
        ledger.complete_tool("read_file", duration=duration, is_error=is_error)
    return ledger


def test_unknown_outcome_claims_neither_success_nor_a_duration():
    first, second = (render_row(r, now=9.0) for r in overlapping_reads(False, False).snapshot())

    for rendered in (first, second):
        assert rendered.endswith(" · <i>Completed</i>")
        assert "Done" not in rendered and "3s" not in rendered and "4s" not in rendered
    assert "a.md" in first and "b.md" in second


def test_unpaired_failure_stays_visible_without_inventing_a_success_or_a_duration():
    ledger = overlapping_reads(False, True)
    first, second = (render_row(r, now=9.0) for r in ledger.snapshot())

    # Which of the two calls failed is unknown: the error is never hidden and
    # neither row is given a success or a time it cannot prove.
    for rendered in (first, second):
        assert rendered.endswith(" · <b>Failed</b>")
        assert "Done" not in rendered and "Completed" not in rendered
        assert "3s" not in rendered and "4s" not in rendered
    assert "a.md" in first and "b.md" in second
    for line in ledger.diagnostic_lines():
        assert "aggregate failure; unknown pairing" in line and "unknown duration" in line


# ── Tool detail is a bounded preview ────────────────────────────────────────

DETAIL_BUDGET = 80          # visible characters, omission mark included


def detail_of(rendered, label, state):
    """Visible detail between a row's action label and its trailing state."""
    text = visible(rendered)
    assert text.startswith(f"{label} ") and text.endswith(f" · {state}"), text
    return text[len(label) + 1:len(text) - len(f" · {state}")]


def terminal_fence(lines, lang="bash"):
    emoji = get_tool_emoji("terminal", default="⚙️")
    body = "\n".join(lines)
    return f"{emoji} Running command\n```{lang}\n{body}\n```"


def test_long_multiline_command_is_one_bounded_preview_with_state_still_visible():
    ledger = ActivityLedger()
    commands = [f"print({'configuration-check-' + str(i) + '-' + 'x' * 45!r})" for i in range(8)]
    raw = terminal_fence(commands, "python")
    ledger.add_line(raw, tool="terminal", now=1001.0)
    rendered = render_row(ledger.snapshot()[0], now=1003.0)
    detail = detail_of(rendered, "Terminal", "2s")

    assert "<br>" not in rendered and "\n" not in rendered
    assert len(detail) <= DETAIL_BUDGET and detail.endswith("…")
    assert detail.startswith("print('configuration-check-0-")
    assert "configuration-check-7-" not in rendered
    assert rendered.endswith(" · <i>2s</i>")
    # Only the visible preview is bounded; the ledger keeps the raw detail.
    assert ledger.rows[0].text == raw


def test_detail_at_the_budget_is_intact_and_one_character_over_is_marked_omitted():
    ledger = ActivityLedger()
    started(ledger, "terminal", "command " + "a" * DETAIL_BUDGET, now=10.0)
    started(ledger, "terminal", "command " + "a" * (DETAIL_BUDGET + 1), now=10.0)
    at_budget, over = (render_row(r, now=15.0) for r in ledger.snapshot())

    assert detail_of(at_budget, "Terminal", "5s") == "a" * DETAIL_BUDGET
    assert detail_of(over, "Terminal", "5s") == "a" * (DETAIL_BUDGET - 1) + "…"


def test_long_prose_detail_is_bounded_in_reading_order():
    ledger = ActivityLedger()
    query = " ".join(f"term{i:03d}" for i in range(60))
    started(ledger, "web_search", query, now=0.0)
    ledger.complete_tool("web_search", duration=2.0, is_error=False)
    rendered = render_row(ledger.snapshot()[0], now=9.0)
    detail = detail_of(rendered, "Searching the web", "Done · 2s")

    assert len(detail) <= DETAIL_BUDGET and detail.endswith("…")
    assert f"for {query}".startswith(detail[:-1])


def test_bounded_preview_counts_visible_characters_and_keeps_markup_safe():
    ledger = ActivityLedger()
    nasty = "echo '<b>&</b>' \"</tg-thinking>\" " * 12
    started(ledger, "terminal", f"command {nasty}", now=10.0)
    rendered = render_row(ledger.snapshot()[0], now=11.5)
    detail = detail_of(rendered, "Terminal", "1s")

    # Entities are single visible characters: the budget is spent on text.
    assert len(detail) == DETAIL_BUDGET and detail.endswith("…")
    assert nasty.startswith(detail[:-1])
    assert re.findall(r"</?[a-z-]+", rendered) == ["<code", "</code", "<i", "</i"]


def test_bounded_preview_keeps_whole_unicode_characters():
    ledger = ActivityLedger()
    preview = "检查配置 🚀 naïve café " * 20
    started(ledger, "read_file", preview, now=0.0)
    ledger.complete_tool("read_file", duration=0.2, is_error=False)
    rendered = render_row(ledger.snapshot()[0], now=1.0)
    detail = detail_of(rendered, "Reading", "Done")

    assert len(detail) <= DETAIL_BUDGET and detail.endswith("…")
    assert preview.startswith(detail[:-1])
    assert rendered.encode("utf-8").decode("utf-8") == rendered


def test_failed_tool_with_a_long_detail_keeps_its_failure_visible():
    ledger = ActivityLedger()
    started(ledger, "web_search", "q" * 400, now=0.0)
    ledger.complete_tool("web_search", duration=3.2, is_error=True)
    rendered = render_row(ledger.snapshot()[0], now=9.0)

    assert rendered.endswith("… · <b>Failed · 3s</b>")
    assert len(detail_of(rendered, "Searching the web", "Failed · 3s")) == DETAIL_BUDGET


def test_unlabelled_tool_line_is_bounded_too():
    ledger = ActivityLedger()
    ledger.add_line('⚙️ mcp_probe: "' + "z" * 300 + '"', tool="mcp_probe", now=10.0)
    rendered = render_row(ledger.snapshot()[0], now=14.0)
    detail = detail_of(rendered, "Mcp probe", "4s")

    assert len(detail) == DETAIL_BUDGET and detail.endswith("…")
    assert detail.startswith('⚙️ mcp_probe: "zzz')


def test_distinct_long_calls_stay_distinct_bounded_rows():
    ledger = ActivityLedger()
    shared = "s" * 120
    started(ledger, "terminal", f"command {shared} --first", now=0.0)
    ledger.complete_tool("terminal", duration=0.1, is_error=False)
    started(ledger, "terminal", f"command {shared} --second", now=1.0)
    ledger.complete_tool("terminal", duration=2.0, is_error=True)
    block = render_thinking_block(ledger.snapshot(), now=4.0)
    body = block.removeprefix("<tg-thinking>").removesuffix("</tg-thinking>")
    first, second = body.split("<br><br>")[1].split("<br>")

    assert len(detail_of(first, "Terminal", "Done")) == DETAIL_BUDGET
    assert len(detail_of(second, "Terminal", "Failed · 2s")) == DETAIL_BUDGET
    assert [r.text for r in ledger.rows] == [
        gateway_line("terminal", f"command {shared} --first"),
        gateway_line("terminal", f"command {shared} --second"),
    ]


def test_tool_without_detail_shows_label_and_state_with_no_omission_mark():
    ledger = ActivityLedger()
    emoji = get_tool_emoji("terminal", default="⚙️")
    ledger.add_line(f"{emoji} Running command", tool="terminal", now=10.0)

    assert render_row(ledger.snapshot()[0], now=13.0) == "Terminal · <i>3s</i>"


def test_long_public_thought_is_never_clipped():
    ledger = ActivityLedger()
    words = " ".join(f"word{i:03d}" for i in range(80))
    ledger.add_line(f"{words} `code {'c' * 120}`", kind="commentary", now=0.0)
    rendered = render_row(ledger.snapshot()[0])

    assert visible(rendered) == f"{words} code {'c' * 120}"
    assert "…" not in rendered
    assert rendered.startswith(f"<b>{words} </b><code>")


# ── Block layout ────────────────────────────────────────────────────────────


def test_tools_group_compactly_and_thoughts_get_clear_space_in_event_order():
    ledger = ActivityLedger()
    ledger.add_line("Checking both clocks.", kind="thought", now=0.0)
    started(ledger, "read_file", "same preview…", now=1.0)
    ledger.complete_tool("read_file", duration=0.1, is_error=False)
    started(ledger, "read_file", "same preview…", now=2.0)   # distinct call, same clip
    ledger.complete_tool("read_file", duration=0.1, is_error=False)
    ledger.add_line("They agree.", kind="thought", now=3.0)
    block = render_thinking_block(ledger.snapshot(), now=4.0)

    body = block.removeprefix("<tg-thinking>").removesuffix("</tg-thinking>")
    tool = "Reading same preview… · <i>Done</i>"
    assert body.split("<br><br>")[1:] == [
        "<b>💭 Checking both clocks.</b>",
        f"{tool}<br>{tool}",
        "<b>💭 They agree.</b>",
    ]


def test_blank_commentary_adds_no_empty_gap_between_tool_rows():
    ledger = ActivityLedger()
    started(ledger, "read_file", "a.md", now=0.0)
    ledger.add_line("   ", kind="commentary", now=1.0)
    started(ledger, "web_search", "b", now=2.0)
    block = render_thinking_block(ledger.snapshot(), now=2.5)

    assert "<br><br><br>" not in block
    assert block.count("<br><br>") == 1


def test_phase_header_is_quiet_so_thoughts_carry_the_emphasis():
    ledger = ActivityLedger()
    ledger.add_line("Checking both clocks.", kind="thought", now=0.0)
    started(ledger, "web_search", "local time", now=1.0)
    block = render_thinking_block(ledger.snapshot(), now=6.0)

    assert "<b>" not in header_of(block) and "<i>" not in header_of(block)
    assert re.findall(r"<b>(.*?)</b>", block) == ["💭 Checking both clocks."]


def test_block_is_one_physical_line_with_a_single_thinking_pair():
    ledger = ActivityLedger()
    ledger.add_line("Plan:\n\n</tg-thinking><tg-thinking> || done", kind="thought", now=0.0)
    emoji = get_tool_emoji("terminal", default="⚙️")
    ledger.add_line(f"{emoji} Running command\n```\na\n\nb\n```", tool="terminal", now=1.0)
    block = render_thinking_block(ledger.snapshot(), now=2.0)

    assert "\n" not in block
    assert block.startswith("<tg-thinking>") and block.endswith("</tg-thinking>")
    assert block.count("<tg-thinking>") == 1 and block.count("</tg-thinking>") == 1
    assert block.count("<b>") == block.count("</b>")
    assert block.count("<code>") == block.count("</code>") == 2


# ── Phase header clock ──────────────────────────────────────────────────────


def test_header_elapsed_counts_from_turn_start_not_the_current_tool():
    ledger = ActivityLedger()
    started(ledger, "web_search", "first", now=100.0)
    ledger.complete_tool("web_search", duration=30.0, is_error=False)
    started(ledger, "read_file", "second", now=140.0)
    first = _with_turn_start(
        render_thinking_block, ledger.snapshot(), now=142.5, turn_started_at=100.0,
    )
    ledger.complete_tool("read_file", duration=5.0, is_error=False)
    started(ledger, "terminal", "command pwd", now=150.0)
    second = _with_turn_start(
        render_thinking_block, ledger.snapshot(), now=151.0, turn_started_at=100.0,
    )

    assert header_of(first).endswith(" · 42s")
    assert header_of(second).endswith(" · 51s")


def test_header_elapsed_ignores_idle_resets_when_the_turn_start_is_known():
    ledger = ActivityLedger()
    ledger.add_line("Thinking it over.", kind="thought", now=10.0)
    block = _with_turn_start(
        render_thinking_block, ledger.snapshot(),
        now=135.0, idle_since=130.0, turn_started_at=10.0,
    )

    assert header_of(block) == "Thinking · 2m 05s"


def test_first_frame_reports_under_a_second_and_never_a_negative_or_zero_time():
    ledger = ActivityLedger()
    ledger.add_line("Starting.", kind="thought", now=50.0)
    at_start = _with_turn_start(
        render_thinking_block, ledger.snapshot(), now=50.4, turn_started_at=50.0,
    )
    skewed = _with_turn_start(
        render_thinking_block, ledger.snapshot(), now=50.0, turn_started_at=58.0,
    )

    assert header_of(at_start) == "Thinking · &lt;1s"
    assert header_of(skewed) == "Thinking · &lt;1s"


def test_callers_without_a_turn_start_keep_the_established_header_origin():
    ledger = ActivityLedger()
    started(ledger, "web_search", "first", now=100.0)
    ledger.complete_tool("web_search", duration=30.0, is_error=False)
    started(ledger, "read_file", "second", now=140.0)
    rows = ledger.snapshot()

    assert header_of(render_thinking_block(rows, now=145.0)).endswith(" · 5s")
    assert header_of(render_thinking_block(rows[:1], now=145.0, idle_since=141.0)).endswith(" · 4s")
    assert header_of(render_thinking_block(rows[:1], now=145.0)).endswith(" · 45s")


def test_compose_markdown_threads_the_turn_start_and_leaves_the_answer_untouched():
    ledger = ActivityLedger()
    started(ledger, "web_search", "first", now=140.0)
    answer = "Partial **answer** with `code`\n\n- item"
    markdown = _with_turn_start(
        compose_markdown, ledger.snapshot(), answer, now=142.0, turn_started_at=100.0,
    )
    block, sep, tail = markdown.partition("</tg-thinking>")

    assert header_of(block).endswith(" · 42s")
    assert sep and tail == f"\n\n{answer}"
