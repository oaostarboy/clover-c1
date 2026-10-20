"""Semantic assertions independent of regenerated native snapshots."""
import re

import pytest

from plugins.platforms.telegram.adapter import TelegramAdapter


def render(text):
    return TelegramAdapter.format_message(None, text)


def test_multiple_blocks_then_triple_inline_has_no_phantom_backticks():
    # A backtick run is one delimiter, never an inner single-tick pair plus
    # four visible ticks. This is inline code, not a third fenced block.
    source = '```\na\n```\nmid\n```\nb\n```\nend ```inline``` tail'
    assert render(source) == '```\na\n```\nmid\n```\nb\n```\nend `inline` tail'


@pytest.mark.parametrize('ticks', ['`', '``', '```', '````'])
def test_inline_delimiter_run_maps_to_one_native_code_span(ticks):
    assert render('Before ' + ticks + 'x(y)' + ticks + ' **after**') == 'Before `x(y)` *after*'


def test_unmatched_triple_inline_keeps_all_literal_ticks():
    assert render('Before ```unclosed **after**') == r'Before \`\`\`unclosed *after*'


def test_unclosed_block_preserves_exact_body_in_native_wrapper():
    body = "\t  print('never closed') \\x `tick`  \r\n\nlast  "
    wire = render('```python\n' + body)
    assert wire == '```python\n' + body.replace('\\', '\\\\').replace('`', '\\`') + '```'
    # Decode only the two legal MarkdownV2 code escapes; never strip whitespace.
    payload = wire[len('```python\n'):-3]
    assert re.sub(r'\\([\\`])', r'\1', payload) == body
