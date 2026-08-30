"""The collapsed per-turn summary card.

Ported from the sibling agent fork on 2026-08-30. When ``cleanup_progress`` is
on, a turn's temporary progress bubbles are not all deleted: the FIRST one is
edited into a single expandable Telegram card ("🧠 23 thoughts · 🛠 35 tool
calls · ⏱ 33m53s") and the rest are removed.

The card is Telegram expandable-blockquote markup, and its two syntax rules are
easy to break silently:

* the marker needs a trailing space (``**> Text``). ``**>Text`` gets escaped to
  literal ``\\*\\*\\>Text`` and shows up as visible junk in the chat.
* the ``||`` terminator must land on the LAST line of the block.

So these tests assert the exact rendered string, not just that a call returned
something.
"""

from __future__ import annotations

from agent.turn_summary import format_collapsed_turn_card


class TestCardContent:
    def test_matches_the_shipped_format(self):
        """The shape seen in chat: thoughts · tool calls · elapsed."""
        card = format_collapsed_turn_card(23, 35, 2033.0)

        assert card == "**> 🧠 23 thoughts · 🛠 35 tool calls · ⏱ 33m53s||"

    def test_singular_wording(self):
        card = format_collapsed_turn_card(1, 1, 5.0)

        assert "1 thought ·" in card and "thoughts" not in card
        assert "1 tool call ·" in card and "tool calls" not in card

    def test_seconds_under_a_minute_stay_seconds(self):
        assert "⏱ 47s" in format_collapsed_turn_card(1, 1, 47.4)

    def test_minutes_are_zero_padded(self):
        # 125s -> 2m05s, not 2m5s: the pad keeps the card width stable.
        assert "⏱ 2m05s" in format_collapsed_turn_card(1, 1, 125.0)


class TestCardIsSkippedWhenThereIsNothingToSay:
    """A plain chat reply must never grow a card.

    The gateway treats "" as "fall back to deleting every bubble", so this is
    the switch that keeps conversational turns clean.
    """

    def test_no_work_returns_empty(self):
        assert format_collapsed_turn_card(0, 0, 12.0) == ""

    def test_thoughts_alone_still_render(self):
        card = format_collapsed_turn_card(3, 0, 8.0)
        assert card.startswith("**> 🧠 3 thoughts")
        assert "tool call" not in card

    def test_tools_alone_still_render(self):
        card = format_collapsed_turn_card(0, 2, 8.0)
        assert card.startswith("**> 🛠 2 tool calls")
        assert "thought" not in card


class TestTelegramMarkupRules:
    """Guard the two syntax rules that fail silently when broken."""

    def test_marker_has_the_mandatory_trailing_space(self):
        card = format_collapsed_turn_card(2, 2, 10.0)

        assert card.startswith("**> "), (
            "Telegram escapes '**>Text' into literal '\\*\\*\\>Text'. The space "
            "after the marker is what makes it an expandable blockquote."
        )

    def test_terminator_is_on_the_final_line(self):
        card = format_collapsed_turn_card(
            2, 2, 10.0, detail_lines=["first detail", "second detail"]
        )
        lines = card.split("\n")

        assert lines[-1].endswith("||")
        assert card.count("||") == 1

    def test_detail_lines_are_quoted_and_flattened(self):
        # Embedded newlines would break out of the blockquote and split the
        # card into loose messages, so they are collapsed to spaces.
        card = format_collapsed_turn_card(
            1, 1, 10.0, detail_lines=["line one\nstill line one", "", "   "]
        )
        lines = card.split("\n")

        assert lines[1] == "> line one still line one||"
        assert len(lines) == 2, "blank detail lines must be dropped"


class TestTelegramRendersItRatherThanEscapingIt:
    """End-to-end through the real adapter formatter.

    format_message() escapes anything it does not recognise as markup. If the
    card came back with backslashes, the user would see raw '\\*\\*\\>' text in
    the chat instead of a card, which is exactly the regression this catches.
    """

    def test_format_message_leaves_the_card_intact(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / (
            "plugins/platforms/telegram/adapter.py"
        )
        spec = importlib.util.spec_from_file_location("tg_adapter_card", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        card = format_collapsed_turn_card(23, 35, 2033.0)
        rendered = mod.TelegramAdapter.format_message(mod.TelegramAdapter, card)

        assert rendered == card
        assert "\\*" not in rendered and "\\|" not in rendered
