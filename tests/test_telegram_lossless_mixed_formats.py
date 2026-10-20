"""Actual native fixtures with prose formatting adjacent to literal code."""
import json

import pytest
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from tests.test_telegram_lossless_code import assert_physical_code, code_body


@pytest.mark.parametrize('mode', ['multipart', 'draft'])
@pytest.mark.parametrize('progressive', [False, True])
def test_native_mixed_formats_do_not_reinterpret_code(monkeypatch, tmp_path, mode, progressive):
    import tests.test_presence_stream_native_wire as native
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from telegram import Bot
    from cron.presence import ThoughtStore
    from gateway.platforms.base import utf16_len

    prefix = 'Deployment **bold** and *italic* with `note`\n\n'
    formatted_prefix = 'Deployment *bold* and _italic_ with `note`\n\n'
    body = code_body('inline-fence') + '\t    **literal** _literal_ `literal` \\end\t \n'
    target = prefix + '```python\n' + body + '```'
    stream = native.enable_native_stream
    monkeypatch.setattr(native, 'enable_native_stream', lambda mp, h, _text, **kw: stream(mp, h, target, **kw))
    visible = {}
    initiatives = set()
    presence_send = TelegramAdapter.send_presence
    async def initiative(self, payload):
        mid = await presence_send(self, payload)
        initiatives.add(str(mid))
        return mid
    monkeypatch.setattr(TelegramAdapter, 'send_presence', initiative)
    for method in ('send_message', 'edit_message_text', 'delete_message'):
        original = getattr(Bot, method)
        def bind(method, original):
            async def observe(self, *args, **kwargs):
                result = await original(self, *args, **kwargs)
                mid = str(result.message_id) if method == 'send_message' else str(kwargs['message_id'])
                if method == 'delete_message':
                    visible.pop(mid, None)
                else:
                    visible[mid] = kwargs['text']
                return result
            return observe
        monkeypatch.setattr(Bot, method, bind(method, original))
    with native.native_telegram_stream(monkeypatch, tmp_path, mode, progressive=progressive) as observed:
        requested = [text for mid, text in visible.items() if mid not in initiatives]
        (tmp_path / 'physical-wire.json').write_text(json.dumps(dict(target=target, body=body, visible=visible), ensure_ascii=False, indent=2))
        assert sum(text.startswith(formatted_prefix) for text in requested) == 1
        requested = [text.removeprefix(formatted_prefix) for text in requested]
        assert_physical_code(requested, body)
        assert all(utf16_len(params['text']) <= 4096 for method, params in observed.endpoint.calls if method in ('sendMessage', 'editMessageText'))
        assert len(observed.wires) == 1 and len(observed.requests) == 1
        assert len(ThoughtStore().publications()) == 1
