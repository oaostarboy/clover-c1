"""Fence/escape boundary and no-final-newline contract for native code."""
import pytest

from tests.test_telegram_lossless_code import assert_physical_code


@pytest.mark.parametrize('splitter', ['adapter', 'fallback'])
@pytest.mark.parametrize('limit', [31, 32, 33, 47, 79, 130])
@pytest.mark.parametrize('ending', ['newline', 'no-newline'])
def test_fence_boundaries_keep_every_literal_character(limit, ending, splitter):
    from gateway.config import PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, utf16_len
    from plugins.platforms.telegram.adapter import TelegramAdapter
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='synthetic'))
    body = '\t    literal=\\`雪🙂`  \r\n\n' * 25
    if ending == 'no-newline':
        body = body[:-3]
    text = '```python\n' + body + '```'
    if splitter == 'adapter':
        chunks = BasePlatformAdapter.truncate_message(text, limit, len_fn=adapter.streaming_message_len)
    else:
        from gateway.stream_consumer import GatewayStreamConsumer
        chunks = GatewayStreamConsumer._split_text_chunks(text, limit, adapter.streaming_message_len)
    wires = [adapter.format_message(chunk) for chunk in chunks]
    assert all(utf16_len(wire) <= limit for wire in wires)
    assert_physical_code(wires, body)
