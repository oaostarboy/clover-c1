"""Actual gateway/AIAgent/consumer/PTB output, reconstructed by returned IDs.

No model or Telegram socket is opened. The synthetic HTTP endpoint also rejects
oversize payloads, so a permissive endpoint cannot hide formatting expansion.
"""
import json
import re
from collections import Counter

import pytest
import yaml

from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


def literal(text):
    text = re.sub(r'\\([_*\[\]()~`>#+\-=|{}.!\\])', r'\1', text)
    text = re.sub(r'\s\(\d+/\d+\)(?=\s|$)', '', text)
    text = re.sub(r'```(?:python)?\n?', '', text)
    return ''.join(text.split())


@pytest.mark.parametrize('style', ['unicode', 'escapes', 'code', 'list', 'inline'])
@pytest.mark.parametrize('rows', [123, 251, 503])
@pytest.mark.parametrize('progressive', [False, True])
def test_native_chunk_boundaries_preserve_every_literal_once(monkeypatch, tmp_path, style, rows, progressive):
    _run_native(monkeypatch, tmp_path, style, rows, progressive, 'none', True)


@pytest.mark.parametrize('fault', ['none', 'lost', 'corrupt', 'duplicate', 'out_of_order'])
@pytest.mark.parametrize('presence', [False, True])
def test_native_proof_faults_never_replay_or_admit_missing_proof(monkeypatch, tmp_path, fault, presence):
    _run_native(monkeypatch, tmp_path, 'unicode', 503, True, fault, presence)


@pytest.mark.parametrize('progressive', [False, True])
@pytest.mark.parametrize('presence', [False, True])
def test_nested_native_splits_are_not_finalized_again_as_whole_logical_tail(monkeypatch, tmp_path, progressive, presence):
    # The rich-capability budget can exceed the legacy native cap, then the
    # native send/edit falls back to legacy splitting. Exercise that actual
    # split geometry through the full gateway without disabling streaming.
    _run_native(monkeypatch, tmp_path, 'unicode', 503, progressive, 'none', presence, nested=True)


def _run_native(monkeypatch, tmp_path, style, rows, progressive, fault, presence, *, nested=False):
    import tests.test_presence_stream_native_wire as native
    if nested:
        from gateway.stream_consumer import GatewayStreamConsumer
        monkeypatch.setattr(GatewayStreamConsumer, '_raw_message_limit', lambda self: 32768)
    from tests.test_owner_telegram_wire import TelegramEndpoint
    from telegram import Bot
    from cron.presence import ThoughtStore
    from gateway.platforms.base import utf16_len
    from gateway.requested_reply_delivery import RequestedReplyOwner

    suffix = {'unicode': ' 雪🙂é', 'escapes': ' _ [] () ! . + = | { }',
              'code': ' 雪🙂é', 'list': ' 雪🙂é', 'inline': ' `雪🙂é`'}[style]
    body = ''.join(('1. ' if style == 'list' else '') + f'row_{i:04d} evidence_{i:04d}{suffix}\n' for i in range(rows))
    target = 'Deployment ' + ('```python\n' + body + '```\n' if style == 'code' else body)
    stream = native.enable_native_stream
    def enable(mp, h, _text, **kw):
        if nested and progressive:
            kw['first_delta'] = target[:len(target)//2]
        return stream(mp, h, target, **kw)
    monkeypatch.setattr(native, 'enable_native_stream', enable)
    if not presence:
        setup = native.setup_host
        def default_off(*args, **kw):
            h, requests, bot = setup(*args, **kw)
            config_path = h.home / 'config.yaml'
            config = yaml.safe_load(config_path.read_text())
            config.pop('presence', None); config.pop('proactive', None)
            config_path.write_text(yaml.safe_dump(config))
            return h, requests, bot
        monkeypatch.setattr(native, 'setup_host', default_off)
    http = TelegramEndpoint.do_request
    async def bounded(self, url, method, request_data=None, **kw):
        if request_data is not None:
            params = request_data.parameters
            if url.endswith(('sendMessage', 'editMessageText')):
                assert utf16_len(params['text']) <= 4096, 'real Telegram payload exceeds the physical cap'
        return await http(self, url, method, request_data, **kw)
    monkeypatch.setattr(TelegramEndpoint, 'do_request', bounded)
    visible, operations = {}, []
    initiative_ids = set()
    from plugins.platforms.telegram.adapter import TelegramAdapter
    send_presence = TelegramAdapter.send_presence
    async def initiative(self, payload):
        mid = await send_presence(self, payload)
        initiative_ids.add(str(mid))
        return mid
    monkeypatch.setattr(TelegramAdapter, 'send_presence', initiative)
    for method in ('send_message', 'edit_message_text', 'delete_message'):
        original = getattr(Bot, method)
        def bind(method, original):
            async def call(self, *args, **kw):
                result = await original(self, *args, **kw)
                mid = str(result.message_id) if method == 'send_message' else str(kw.get('message_id', args[1] if len(args) > 1 else ''))
                operations.append((method, mid, kw.get('text')))
                if method == 'delete_message':
                    visible.pop(mid, None)
                else:
                    visible[mid] = kw['text']
                return result
            return call
        monkeypatch.setattr(Bot, method, bind(method, original))
    original_receipt = RequestedReplyOwner.receipt
    accepted = []
    def receipt(self, consumer, text):
        # Fault the optional proof only after complete physical delivery.
        if fault != 'none':
            from dataclasses import replace
            acks = consumer._final_transport_acks
            if fault == 'lost':
                del acks[len(acks)//2]
            elif fault == 'corrupt':
                acks[1] = replace(acks[1], content='Not the requested body')
            elif fault == 'duplicate':
                acks[1] = acks[0]
            else:
                acks.reverse()
        result = original_receipt(self, consumer, text)
        accepted.append(result is not None)
        (tmp_path/'receipt-debug.json').write_text(json.dumps(dict(
            target=text, delivered=consumer._delivered_final_text,
            splits=consumer._final_receipt_splits,
            revisions=getattr(consumer, '_physical_message_revisions', None),
            acks=[dict(content=a.content, ids=a.message_ids, revisions=getattr(a, 'revisions', ()), terminal=a.terminal)
                  for a in consumer._final_transport_acks],
        ), ensure_ascii=False, indent=2))
        return result
    monkeypatch.setattr(RequestedReplyOwner, 'receipt', receipt)
    with native.native_telegram_stream(monkeypatch, tmp_path, 'multipart', progressive=progressive) as observed:
        (tmp_path/'physical-wire.json').write_text(json.dumps(dict(target=target, visible=visible, operations=operations,
            http=observed.endpoint.calls, accepted=accepted), ensure_ascii=False, indent=2, default=str))
        # Exclude only the actual returned initiative ID. All requested sends,
        # edits and deletions remain in the reconstructed physical message map.
        answer = [s for mid, s in visible.items() if mid not in initiative_ids]
        assert ''.join(literal(s) for s in answer) == literal(target)
        found = re.findall(r'row_(\d{4})', ''.join(literal(s) for s in answer))
        assert Counter(found) == Counter(f'{i:04d}' for i in range(rows))
        assert len(observed.wires) == 1 and observed.wires[0]['stream'] is True
        assert len(observed.requests) == int(presence and fault == 'none')
        assert len(ThoughtStore().publications()) == int(presence and fault == 'none')
        if presence:
            assert accepted == [fault == 'none']
        assert all(str(p['chat_id']) == '12345' for m, p in observed.endpoint.calls if m in {'sendMessage', 'editMessageText'})
        (tmp_path/'physical-wire.json').write_text(json.dumps(dict(target=target, visible=visible, operations=operations,
            http=observed.endpoint.calls, accepted=accepted), ensure_ascii=False, indent=2, default=str))
