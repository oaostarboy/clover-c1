"""Native edit preflight uses the same formatted UTF-16 budget as sends."""
import asyncio
import json
from collections import Counter
import re

import pytest
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from tests.gateway.test_presence_integration import setup_host
from tests.test_telegram_physical_delivery import literal


@pytest.mark.parametrize('style', ['unicode', 'list', 'code'])
@pytest.mark.parametrize('rows', [130, 270])
def test_native_final_edit_expansion_is_bounded_and_complete(monkeypatch, tmp_path, style, rows):
    from telegram.ext import Application
    from gateway.platforms.base import utf16_len
    from tests.test_owner_telegram_wire import TOKEN, TelegramEndpoint
    h, requests, _ = setup_host(monkeypatch, tmp_path)
    attempts = []
    visible = {'77': 'Old preview'}
    class Endpoint(TelegramEndpoint):
        async def do_request(self, url, method, request_data=None, **kwargs):
            params = request_data.parameters if request_data else {}
            api = url.rsplit('/', 1)[-1]
            if api in {'sendMessage', 'editMessageText'}:
                attempts.append((api, params))
                assert utf16_len(params['text']) <= 4096
            status, raw = await super().do_request(url, method, request_data, **kwargs)
            response = json.loads(raw)
            if api == 'sendMessage':
                response['result']['message_id'] = len(self.calls) + 100
                visible[str(response['result']['message_id'])] = params['text']
            elif api == 'editMessageText':
                visible[str(params['message_id'])] = params['text']
            return status, json.dumps(response).encode()
    endpoint = Endpoint()
    app = Application.builder().token(TOKEN).request(endpoint).build()
    h.adapter._bot = app.bot
    body = ''.join(('1. ' if style == 'list' else '') + f'row_{i:04d} evidence_{i:04d}. 雪🙂é\n' for i in range(rows))
    text = 'Deployment\n' + ('```python\n' + body + '```' if style == 'code' else body)
    async def run():
        async with app:
            return await type(h.adapter).edit_message(h.adapter, '12345', '77', text, finalize=True)
    try:
        result = asyncio.run(run())
        assert result.success is True
        from gateway.stream_consumer import GatewayStreamConsumer
        assert set(GatewayStreamConsumer._message_ids_from_result(result)) == set(visible), 'native split receipt omits a surviving physical ID'
        assert all(utf16_len(p['text']) <= 4096 for _, p in attempts), 'oversize MarkdownV2 attempt escaped native preflight'
        actual = ''.join(literal(s) for s in visible.values())
        assert actual == literal(text)
        assert Counter(re.findall(r'row_(\d{4})', actual)) == Counter(f'{i:04d}' for i in range(rows))
        assert not requests
    finally:
        h.close()
