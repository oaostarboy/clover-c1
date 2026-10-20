"""Physical revisions revoke optional proof, never requested-delivery state."""
import asyncio
from dataclasses import replace
from itertools import product
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import SendResult
from gateway.requested_reply_delivery import RequestedReplyOwner
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from tests.gateway.test_presence_integration import setup_host
from tests.gateway.test_trusted_human_origin_ingress import network_guard, _user_source, SESSION_KEY  # noqa: F401


@pytest.fixture
def delivered(monkeypatch, tmp_path):
    h, requests, _ = setup_host(monkeypatch, tmp_path)
    source = _user_source()
    generation = h.runner._begin_session_run_generation(SESSION_KEY)
    owner = RequestedReplyOwner.capture(h.runner, source, SESSION_KEY, generation)
    c = GatewayStreamConsumer(h.adapter, source.chat_id, StreamConsumerConfig(transport='edit', cursor=''))
    visible = {}
    async def send(**kw):
        mid = str(len(visible) + 1)
        visible[mid] = kw['content']
        return SendResult(success=True, message_id=mid)
    async def edit(**kw):
        visible[str(kw['message_id'])] = kw['content']
        return SendResult(success=True, message_id=kw['message_id'])
    async def delete(chat_id, message_id):
        visible.pop(str(message_id), None)
        return True
    h.adapter.send.side_effect = send
    h.adapter.edit_message = AsyncMock(side_effect=edit)
    h.adapter.delete_message = AsyncMock(side_effect=delete)
    text = ''.join(f'row_{i:04d} distinct evidence.\n' for i in range(600))
    async def run():
        c.on_delta(text); c.finish(final_text=text); await c.run()
    asyncio.run(run())
    receipt = owner.receipt(c, text)
    assert receipt is not None and len(visible) >= 3
    try:
        yield SimpleNamespace(h=h, requests=requests, c=c, owner=owner, source=source,
                              generation=generation, text=text, visible=visible, receipt=receipt)
    finally:
        h.close()


@pytest.mark.parametrize('mutation', ['preview', 'final_lost', 'delete', 'same_text_final'])
def test_minted_receipt_cannot_outlive_its_physical_revision(delivered, monkeypatch, mutation):
    d = delivered
    mid = list(d.visible)[1]
    old = d.visible[mid]
    async def mutate():
        if mutation == 'delete':
            await d.c._io.delete_message(d.source.chat_id, mid)
        else:
            if mutation == 'final_lost':
                monkeypatch.setattr(d.c, '_record_transport_ack', lambda *a, **kw: None)
            d.c._message_id = mid
            await d.c._send_or_edit(old if mutation == 'same_text_final' else 'Other body',
                                    finalize=mutation != 'preview', is_turn_final=False)
    asyncio.run(mutate())
    event = SimpleNamespace(internal=False, source=d.source)
    assert not d.receipt.accepts(d.h.runner, event, SESSION_KEY, d.generation, d.text)
    assert d.c.delivered_final_matches(d.text) is True
    assert not d.requests


@pytest.mark.parametrize('sequence', list(product(['preview', 'lost', 'restore'], repeat=2)))
def test_mutation_sequences_require_current_literal_ack(delivered, monkeypatch, sequence):
    d = delivered
    mid = list(d.visible)[1]
    old = d.visible[mid]
    recorder = d.c._record_transport_ack
    async def mutate():
        for operation in sequence:
            monkeypatch.setattr(d.c, '_record_transport_ack',
                                (lambda *a, **kw: None) if operation == 'lost' else recorder)
            d.c._message_id = mid
            d.c._last_sent_text = ''
            await d.c._send_or_edit(old if operation == 'restore' else 'Different body',
                                    finalize=operation != 'preview', is_turn_final=False)
            assert (d.owner.receipt(d.c, d.text) is not None) == (operation == 'restore')
            assert d.c.delivered_final_matches(d.text) is True
    asyncio.run(mutate())
    assert not d.requests


@pytest.mark.parametrize('corruption', ['reverse', 'missing_revision', 'duplicate_id', 'wrong_revision'])
def test_corrupt_receipt_records_fail_closed_without_visible_change(delivered, corruption):
    d = delivered
    before = dict(d.visible)
    if corruption == 'reverse':
        d.c._final_transport_acks.reverse()
    else:
        ack = d.c._final_transport_acks[1]
        if corruption == 'missing_revision':
            ack = replace(ack, revisions=())
        elif corruption == 'duplicate_id':
            ack = replace(ack, message_ids=ack.message_ids * 2, revisions=ack.revisions * 2)
        else:
            ack = replace(ack, revisions=(999999,))
        d.c._final_transport_acks[1] = ack
    assert d.owner.receipt(d.c, d.text) is None
    assert d.visible == before and d.c.delivered_final_matches(d.text) is True
    assert not d.requests


@pytest.mark.parametrize('storage', ['revisions', 'results'])
def test_optional_revision_storage_fault_never_changes_successful_delivery(delivered, storage):
    d = delivered
    mid = list(d.visible)[1]
    class Broken(dict):
        def __setitem__(self, key, value):
            raise LookupError('optional proof storage unavailable')
    attr = '_physical_message_revisions' if storage == 'revisions' else '_transport_result_revisions'
    setattr(d.c, attr, Broken(getattr(d.c, attr)))
    async def mutate():
        result = await d.c._io.edit_message(chat_id=d.source.chat_id, message_id=mid,
                                          content='Successful new visible text', finalize=False)
        assert result.success is True
    asyncio.run(mutate())
    assert d.visible[mid] == 'Successful new visible text'
    assert d.owner.receipt(d.c, d.text) is None
    assert d.c.delivered_final_matches(d.text) is True


def test_late_ack_cannot_bless_a_newer_overwrite(delivered):
    d = delivered
    mid = list(d.visible)[1]
    old = d.visible[mid]
    async def mutate():
        entered, release = asyncio.Event(), asyncio.Event()
        async def edit(**kw):
            d.visible[mid] = kw['content']
            if kw['content'] == old:
                entered.set()
                await release.wait()
            return SendResult(success=True, message_id=mid)
        d.h.adapter.edit_message.side_effect = edit
        d.c._final_delivery_active = True
        first = asyncio.create_task(d.c._io.edit_message(chat_id=d.source.chat_id, message_id=mid,
                                                        content=old, finalize=True))
        await entered.wait()
        await d.c._io.edit_message(chat_id=d.source.chat_id, message_id=mid,
                                   content='Newer preview wins', finalize=False)
        release.set()
        await first
    asyncio.run(mutate())
    assert d.visible[mid] == 'Newer preview wins'
    assert d.owner.receipt(d.c, d.text) is None
    assert d.c.delivered_final_matches(d.text) is True


@pytest.mark.parametrize('member', ['root', 'sibling'])
def test_partial_native_identity_overlap_cannot_discard_live_sibling(delivered, member):
    d = delivered
    async def mutate():
        # A native operation reports two physical messages for one payload.
        async def send(**kw):
            d.visible['root'] = kw['content'][:10]
            d.visible['sibling'] = kw['content'][10:]
            return SendResult(success=True, message_id='root', raw_response={'message_ids': ['root', 'sibling']})
        d.h.adapter.send.side_effect = send
        d.c._message_id = None
        await d.c._send_or_edit(d.text.strip(), finalize=True)
        assert d.owner.receipt(d.c, d.text) is not None
        d.c._message_id = member
        d.c._last_sent_text = ''
        await d.c._send_or_edit(d.text.strip(), finalize=True)
    asyncio.run(mutate())
    assert d.owner.receipt(d.c, d.text) is None, 'whole replacement ACK cannot erase a still-visible native sibling'
    assert d.c.delivered_final_matches(d.text) is True


def test_delete_proof_storage_fault_does_not_change_physical_success(delivered):
    d = delivered
    mid = list(d.visible)[1]
    class Broken(set):
        def add(self, item):
            raise LookupError('delete proof unavailable')
    d.c._deleted_final_message_ids = Broken()
    assert asyncio.run(d.c._io.delete_message(d.source.chat_id, mid)) is True
    assert mid not in d.visible
    assert d.owner.receipt(d.c, d.text) is None
    assert d.c.delivered_final_matches(d.text) is True
