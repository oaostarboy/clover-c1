"""
Tests for escape_code_fences_for_display.

B1: Escape triple-backtick markers inside reasoning text before wrapping
    in an outer ``` fence, so inner ``` doesn't break the outer block.
"""

import pytest
from gateway.stream_consumer import (
    escape_code_fences_for_display,
    strip_reasoning_heading_markers,
)


class TestEscapeCodeFencesForDisplay:
    """escape_code_fences_for_display prevents inner ``` from breaking
    the outer code block used to render reasoning."""


    def test_single_fence_escaped(self):
        text = "model used ```python\nx = 1\n``` in its thinking"
        result = escape_code_fences_for_display(text)
        assert "```" not in result
        assert "\\`\\`\\`" in result

    def test_multiple_fences_all_escaped(self):
        text = "```\nblock1\n``` and ```python\nblock2\n```"
        result = escape_code_fences_for_display(text)
        assert result.count("```") == 0
        assert result.count("\\`\\`\\`") == 4


class TestStripReasoningHeadingMarkers:
    """strip_reasoning_heading_markers unwraps whole-line **heading**
    markdown so it doesn't show as literal asterisks inside a code fence."""

    def test_single_heading_line_unwrapped(self):
        text = "**Fetching remote updates**"
        assert strip_reasoning_heading_markers(text) == "Fetching remote updates"

    def test_heading_among_plain_lines(self):
        text = "**Fetching remote updates**\nchecking origin/main\nno conflicts found"
        result = strip_reasoning_heading_markers(text)
        assert "**" not in result
        assert result.splitlines() == [
            "Fetching remote updates",
            "checking origin/main",
            "no conflicts found",
        ]

    def test_multiple_heading_lines_all_unwrapped(self):
        text = "**Step one**\ndid a thing\n**Step two**\ndid another thing"
        result = strip_reasoning_heading_markers(text)
        assert "**" not in result

    def test_preserves_indentation(self):
        text = "  **Indented heading**"
        assert strip_reasoning_heading_markers(text) == "  Indented heading"

    def test_partial_bold_span_inside_sentence_untouched(self):
        # Only a line that IS ENTIRELY a **...** heading is unwrapped —
        # a bold span inside a longer sentence is left alone.
        text = "the file is **very** important here"
        assert strip_reasoning_heading_markers(text) == text

    def test_no_markers_returns_unchanged(self):
        text = "plain reasoning text with no markdown"
        assert strip_reasoning_heading_markers(text) is text

    def test_non_string_passthrough(self):
        assert strip_reasoning_heading_markers(None) is None


