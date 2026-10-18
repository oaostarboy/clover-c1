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


@pytest.mark.parametrize('geometry', ['ordinary', 'native-oversplit', 'open-tail'])
@pytest.mark.parametrize('kind', ['indent', 'inline-fence', 'crlf', 'long-line', 'no-final-newline'])
@pytest.mark.parametrize('mode', ['multipart', 'draft'])
@pytest.mark.parametrize('progressive', [False, True])
@pytest.mark.parametrize('presence', [False, True])
def test_native_multiline_code_has_exact_physical_payload(monkeypatch, tmp_path, kind, mode, progressive, presence, geometry):
    import yaml
    import tests.test_presence_stream_native_wire as native
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from telegram import Bot
    from gateway.platforms.base import utf16_len
    from cron.presence import ThoughtStore
    body = code_body(kind)
    target = 'Deployment \n```python\n' + body + '```'
    stream = native.enable_native_stream
    def enable(mp, h, _text, **kw):
        if geometry == 'open-tail' and progressive:
            kw['first_delta'] = target[:len(target) * 2 // 3]
        return stream(mp, h, target, **kw)
    monkeypatch.setattr(native, 'enable_native_stream', enable)
    if geometry == 'native-oversplit':
        from gateway.stream_consumer import GatewayStreamConsumer
        monkeypatch.setattr(GatewayStreamConsumer, '_raw_message_limit', lambda self: 32768)
    if not presence:
        setup = native.setup_host
        def off(*a, **kw):
            h, requests, bot = setup(*a, **kw)
            path = h.home / 'config.yaml'
            config = yaml.safe_load(path.read_text())
            config.pop('presence', None)
            config.pop('proactive', None)
            path.write_text(yaml.safe_dump(config))
            return h, requests, bot
        monkeypatch.setattr(native, 'setup_host', off)
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
    with native.native_telegram_stream(monkeypatch, tmp_path, mode, progressive=progressive) as observed:
        requested = [text for mid, (text, _) in visible.items() if mid not in initiatives]
        (tmp_path / 'physical-wire.json').write_text(json.dumps(dict(target=target, body=body, visible=visible, operations=operations), ensure_ascii=False, indent=2))
        assert all(utf16_len(params['text']) <= 4096 for method, params in observed.endpoint.calls if method in ('sendMessage', 'editMessageText'))
        assert len(observed.wires) == 1
        assert len(observed.requests) == int(presence)
        assert len(ThoughtStore().publications()) == int(presence)
        assert_physical_code(requested, body)
