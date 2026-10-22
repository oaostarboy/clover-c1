"""Lossless fenced bodies: compare escaped physical payloads, never strip data.

The only allowed extra body byte is the single newline deliberately added
before a synthetic closing fence at a split. No whitespace/backtick decoder.
"""
import json
import re

import pytest

from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


def escaped_code(text):
    return text.replace('\\', '\\\\').replace('`', '\\`')


def assert_physical_code(wires, body):
    payloads = []
    for text in wires:
        pattern = r'(?<![\\`])```(?:python)?\r?\n([\s\S]*?)(?<!\\)```'
        payloads.extend(span.group(1) for span in re.finditer(pattern, text))
        unnumbered = re.sub(r'[ \n]*\\\(\d+/\d+\\\)$', '', text)
        outside = re.sub(pattern, '', unnumbered)
        assert outside in ('', 'Deployment \n'), repr(outside)
    assert payloads, wires
    # This corpus has one source block. Every earlier closing fence was
    # inserted by the splitter and adds exactly one LF. Keep all source LFs,
    # CRs, spaces, tabs, backslashes, and backticks, including blank lines.
    pieces = []
    for payload in payloads[:-1]:
        assert payload.endswith('\n'), repr(payload[-100:])
        pieces.append(payload[:-1])
    pieces.append(payloads[-1])
    assert ''.join(pieces) == escaped_code(body), (repr(''.join(pieces)[:120]), repr(escaped_code(body)[:120]))


def code_body(kind, rows=160):
    if kind == 'indent':
        return ''.join(f'\t    R{i:04d} = 雪🙂e\u0301  \\x `literal` \t \n\n' for i in range(rows))
    if kind == 'inline-fence':
        return ''.join(f'    R{i:04d} = "``` and `` and `"  \t\n' for i in range(rows))
    if kind == 'crlf':
        return ''.join(f'\tR{i:04d} = 雪🙂 \\`end`\t \r\n\r\n' for i in range(rows))
    if kind == 'no-final-newline':
        return code_body('indent', rows).removesuffix('\n\n')
    if kind == 'long-line':
        return '\t ' * 90 + '    雪🙂 \\`literal` ' * rows + '\n'
    raise AssertionError(kind)


@pytest.mark.parametrize('kind', ['indent', 'inline-fence', 'crlf', 'long-line', 'no-final-newline'])
@pytest.mark.parametrize('limit', [79, 130, 4096])
def test_split_render_preserves_exact_escaped_code_body(kind, limit):
    from gateway.config import PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, utf16_len
    from plugins.platforms.telegram.adapter import TelegramAdapter
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='synthetic'))
    body = code_body(kind)
    source = '```python\n' + body + '```'
    chunks = BasePlatformAdapter.truncate_message(source, limit, len_fn=adapter.streaming_message_len)
    wires = [adapter.format_message(chunk) for chunk in chunks]
    assert all(utf16_len(wire) <= limit for wire in wires)
    assert_physical_code(wires, body)


