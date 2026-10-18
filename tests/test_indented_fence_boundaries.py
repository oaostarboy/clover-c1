"""IR-CODE-REG-001: delimiter indentation is syntax; code bytes are data."""
import pytest
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


def escaped(body):
    return body.replace('\\', '\\\\').replace('`', '\\`')


@pytest.mark.parametrize('opening_indent', range(4))
@pytest.mark.parametrize('closing_indent', range(4))
@pytest.mark.parametrize('eol', ['\n', '\r\n'])
@pytest.mark.parametrize('trailing', ['', '  \t'])
def test_indented_closing_line_keeps_exact_body_and_following_prose(opening_indent, closing_indent, eol, trailing):
    from gateway.config import PlatformConfig
    from gateway.platforms.helpers import fenced_code_regions, text_has_unclosed_fence
    from gateway.stream_consumer import ensure_closed_code_fences
    from plugins.platforms.telegram.adapter import TelegramAdapter
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='synthetic'))
    # Include inline literal triples and deeply indented delimiter-shaped data.
    body = ('\t    R0000 = "``` inline and \\`literal`"  \t' + eol +
            '    ```' + eol + '\t```' + eol + '  R0001  \t' + eol + eol)
    prefix = 'Deployment ' + eol + ' ' * opening_indent + '```python' + eol
    close = ' ' * closing_indent + '```'
    text = prefix + body + close + trailing + eol + 'Tail **bold**'
    regions = list(fenced_code_regions(text))
    assert len(regions) == 1
    start, body_start, body_end, end, lang, closed = regions[0]
    assert (start, body_start, body_end, end, lang, closed) == (
        len(prefix) - len('```python' + eol), len(prefix), len(prefix + body),
        len(prefix + body + close), 'python', True)
    assert text[body_start:body_end] == body
    assert not text_has_unclosed_fence(text)
    assert ensure_closed_code_fences(text) == text
    # Whole wire equality proves both the exact code body and prose segmentation.
    assert adapter.format_message(text) == (
        'Deployment ' + eol + ' ' * opening_indent + '```python\n' + escaped(body) +
        '```' + trailing + eol + 'Tail *bold*')


@pytest.mark.parametrize('literal', ['    ```', '       ```', '\t```', ' 	```', r'\```', 'R0001 = "``` inline"'])
@pytest.mark.parametrize('trailing', ['', ' \t'])
def test_terminal_literal_or_deeply_indented_line_does_not_close(literal, trailing):
    from gateway.config import PlatformConfig
    from gateway.platforms.helpers import fenced_code_regions, text_has_unclosed_fence
    from gateway.stream_consumer import ensure_closed_code_fences
    from plugins.platforms.telegram.adapter import TelegramAdapter
    body = '    R0000  \t\r\n' + literal + trailing
    text = '```python\n' + body
    region, = fenced_code_regions(text)
    assert region == (0, len('```python\n'), len(text), len(text), 'python', False)
    assert text_has_unclosed_fence(text)
    closed = ensure_closed_code_fences(text)
    assert closed == text + '\n```'
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='synthetic'))
    assert adapter.format_message(closed) == '```python\n' + escaped(body + '\n') + '```'


def two_blocks(indent, rows=2):
    bodies = {
        'python': ''.join(f'    R{i:04d} = "``` inline \\`x`"  \t\r\n\r\n' for i in range(rows)),
        'sql': '    SELECT 雪🙂e\u0301;  \t\n\n',
    }
    close = ' ' * indent + '```'
    text = ('Deployment \n  ```python\r\n' + bodies['python'] + close + ' \t\r\n'
            'Bridge **bold**\n ```sql\n' + bodies['sql'] + close + '\nTail **bold**')
    wire = ('Deployment \n  ```python\n' + escaped(bodies['python']) + '``` \t\r\n'
            'Bridge *bold*\n ```sql\n' + escaped(bodies['sql']) + '```\nTail *bold*')
    return text, wire, bodies


@pytest.mark.parametrize('indent', range(4))
@pytest.mark.parametrize('limit', [43, 79, 131, 4096])
@pytest.mark.parametrize('splitter', ['canonical', 'fallback'])
def test_chunked_multiple_blocks_keep_exact_bodies_and_prose_outside(indent, limit, splitter, tmp_path):
    import json
    import re
    from gateway.config import PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, utf16_len
    from gateway.stream_consumer import GatewayStreamConsumer
    from plugins.platforms.telegram.adapter import TelegramAdapter
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='synthetic'))
    text, _wire, bodies = two_blocks(indent, rows=35)
    if splitter == 'canonical':
        chunks = BasePlatformAdapter.truncate_message(text, limit, adapter.streaming_message_len)
    else:
        chunks = GatewayStreamConsumer._split_text_chunks(text, limit, adapter.streaming_message_len)
    wires = [adapter.format_message(c) for c in chunks]
    (tmp_path / 'physical-wire.json').write_text(json.dumps(dict(chunks=chunks, formatted=wires), ensure_ascii=False, indent=2))
    assert all(utf16_len(w) <= limit for w in wires)
    pattern = re.compile(r'(?<![\\`])```(python|sql)\n([\s\S]*?)(?<![\\`])```')
    spans = [m for w in wires for m in pattern.finditer(w)]
    assert [m.group(1) for m in spans] == sorted(m.group(1) for m in spans)
    for lang, body in bodies.items():
        pieces = [m.group(2) for m in spans if m.group(1) == lang]
        assert pieces and all(piece.endswith('\n') for piece in pieces[:-1])
        # Only synthetic nonterminal wrapper LF may be removed, never source whitespace.
        assert ''.join([piece[:-1] for piece in pieces[:-1]] + pieces[-1:]) == escaped(body), list(zip(chunks[-12:], wires[-12:]))
    outside = [pattern.sub('', w) for w in wires]
    # Words and emphasis can occupy adjacent physical chunks; compare their
    # ordered outside-code tokens without removing code or source whitespace.
    assert [m.group(0) for w in outside for m in re.finditer(r'Bridge|Tail|\*bold\*', w)] == [
        'Bridge', '*bold*', 'Tail', '*bold*']


@pytest.mark.parametrize('indent', range(4))
@pytest.mark.parametrize('mode,cut', [('buffered', 0), ('edit', 0), ('edit', 1), ('draft', 2),
                                     ('edit', 3), ('edit', 6), ('draft', 7)])
def test_native_payload_has_exact_code_and_prose_across_closing_deltas(monkeypatch, tmp_path, indent, mode, cut):
    import json
    import tests.test_presence_stream_native_wire as native
    from cron.presence import ThoughtStore
    from gateway.platforms.base import utf16_len
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from telegram import Bot
    text, expected_wire, bodies = two_blocks(indent)
    # A streamed prefix ends in closing indentation or one/two delimiter ticks.
    close_start = text.index(' ' * indent + '``` \t\r\n', text.index('R0001'))
    first_delta = text[:close_start + indent + cut]
    stream = native.enable_native_stream
    monkeypatch.setattr(native, 'enable_native_stream', lambda mp, h, _text, **kw:
                        stream(mp, h, text, **dict(kw, first_delta=first_delta)))
    visible = {}
    operations = []
    initiatives = set()
    original_presence = TelegramAdapter.send_presence

    async def initiative(self, payload):
        mid = await original_presence(self, payload)
        initiatives.add(str(mid))
        return mid

    monkeypatch.setattr(TelegramAdapter, 'send_presence', initiative)
    for method in ('send_message', 'edit_message_text', 'delete_message'):
        original = getattr(Bot, method)

        def bind(method, original):
            async def observe(self, *args, **kw):
                result = await original(self, *args, **kw)
                mid = str(result.message_id) if method == 'send_message' else str(kw['message_id'])
                operations.append((method, mid, kw.get('text'), str(kw.get('parse_mode'))))
                if method == 'delete_message':
                    visible.pop(mid, None)
                else:
                    visible[mid] = (kw['text'], str(kw.get('parse_mode')))
                return result
            return observe

        monkeypatch.setattr(Bot, method, bind(method, original))
    transport = 'draft' if mode == 'draft' else 'multipart'
    with native.native_telegram_stream(monkeypatch, tmp_path, transport, progressive=mode != 'buffered') as observed:
        requested = [value for mid, value in visible.items() if mid not in initiatives]
        (tmp_path / 'physical-wire.json').write_text(json.dumps(dict(
            target=text, expected_blocks=bodies, expected_wire=expected_wire,
            visible=visible, operations=operations, initiatives=sorted(initiatives),
            first_delta=first_delta, mode=mode), ensure_ascii=False, indent=2))
        # Exact surviving payload and parse mode prove code/prose boundaries without decoding.
        assert requested == [(expected_wire, 'MarkdownV2')]
        assert len(observed.wires) == len(observed.requests) == 1
        assert len(ThoughtStore().publications()) == 1
        assert [p['state'] for p in ThoughtStore().publications()] == ['acknowledged']
        assert all(utf16_len(params['text']) <= 4096 for method, params in observed.endpoint.calls
                   if method in ('sendMessage', 'editMessageText'))
