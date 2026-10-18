"""Native PTB and OpenAI SDK wire proof. Both HTTP endpoints are synthetic."""
import asyncio
import json
import threading
import pytest
from tests.gateway.test_presence_stream_receipts import enable_native_stream
from tests.gateway.test_presence_integration import setup_host
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from cron.presence import ThoughtStore

@pytest.mark.parametrize("mode", ["edit", "draft", "fresh", "multipart"])
def test_native_telegram_sdk_final_and_initiative_have_distinct_transport_receipts(monkeypatch, tmp_path, mode):
    from telegram.ext import Application
    from tests.test_owner_telegram_wire import TOKEN, TelegramEndpoint
    h, requests, _bot = setup_host(monkeypatch, tmp_path)
    try:
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=100)
        gate = threading.Event() if mode != 'multipart' else None
        text = 'The deployment verification is complete' if mode != 'multipart' else ('Deployment verification complete '*350)
        wires, client = enable_native_stream(monkeypatch, h, text, preview_gate=gate)
        class Endpoint(TelegramEndpoint):
            async def do_request(self, url, method, request_data=None, **kwargs):
                status, raw = await super().do_request(url, method, request_data, **kwargs)
                body = json.loads(raw)
                if url.endswith('sendMessage'):
                    body['result']['message_id'] = len(self.calls)+100
                if url.endswith('sendMessageDraft'):
                    body['result'] = True
                if gate is not None and url.endswith(('sendMessage', 'sendMessageDraft')):
                    gate.set()
                return status, json.dumps(body).encode()
        endpoint = Endpoint()
        if mode == 'draft':
            h.runner.config.streaming.transport = 'draft'
            monkeypatch.setattr(h.adapter, 'supports_draft_streaming', lambda *a, **kw: True)
        if mode == 'fresh':
            monkeypatch.setattr(h.adapter, 'prefers_fresh_final_streaming', lambda *a, **kw: True)
        app = Application.builder().token(TOKEN).request(endpoint).build()
        h.adapter._bot = app.bot
        # Keep the production Telegram send/edit implementation and PTB SDK.
        # Only BaseRequest.do_request is synthetic; no socket is opened.
        h.adapter.send = type(h.adapter).send.__get__(h.adapter)
        h.adapter.edit_message = type(h.adapter).edit_message.__get__(h.adapter)
        async def run():
            async with app:
                await h.adapter._handle_text_message(h.update('Help verify deployment packaging', message_id=101), object())
                deadline = asyncio.get_running_loop().time()+10
                while len(h.results)<2 or h.adapter._active_sessions:
                    assert asyncio.get_running_loop().time()<deadline
                    await asyncio.sleep(.01)
        try:
            asyncio.run(run())
            assert wires[0]['stream'] is True
            assert len(requests)==1
            sends = [params for method, params in endpoint.calls if method=='sendMessage']
            if mode == 'fresh':
                assert len(sends)==3
                assert sends[1]['text']==text
                assert any(method=='deleteMessage' for method, _ in endpoint.calls)
            elif mode == 'draft':
                assert len(sends)==2 and sends[0]['text']==text
                assert any(method=='sendMessageDraft' for method, _ in endpoint.calls)
            elif mode == 'edit':
                assert len(sends)==2
                edits = [p for method, p in endpoint.calls if method=='editMessageText']
                assert edits and edits[-1]['text']==text
            else:
                assert len(sends)==4, 'multipart requested reply duplicated'
            assert 'Deployment packaging' in sends[-1]['text']
            assert [p['state'] for p in ThoughtStore().publications()]==['acknowledged']
        finally:
            client.close()
    finally:
        h.close()
