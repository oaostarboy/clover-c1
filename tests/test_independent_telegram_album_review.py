"""Independent frozen-tree probes. No fixed sleeps as test synchronization."""
import asyncio
import dataclasses
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from tests.test_telegram_album_queue_ownership import replay, photo
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.run import _dequeue_pending_event
from agent.image_routing import build_native_content_parts

OUT = Path(__file__).resolve().parents[2] / ('observations-' + Path(__file__).resolve().parents[1].name)


def record(name, payload):
    OUT.mkdir(exist_ok=True)
    (OUT / (name + '.json')).write_text(json.dumps(payload, indent=2) + '\n')


def waiting(x):
    a, r = x.adapter, x.runner
    return ([a._pending_messages[x.key]] if x.key in a._pending_messages else []) + list(r._session_state(x.key).conversation.queued_events)


async def finish_timers(a):
    # Snapshot/gather real owned tasks. Timeout is a deadlock bound, not proof.
    for _ in range(10):
        tasks = set(a._media_group_tasks.values()) | set(a._pending_photo_batch_tasks.values()) | set(getattr(a, '_media_dispatch_tasks', set()))
        tasks = {t for t in tasks if not t.done()}
        if not tasks:
            return
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
    raise AssertionError('task ownership did not settle')


async def real_wire(x):
    await x.bot.initialize()
    x.adapter._bot = x.bot
    x.adapter._send_with_retry = BasePlatformAdapter._send_with_retry.__get__(x.adapter)


@pytest.mark.asyncio
async def test_native_awaited_ack_late_members_retry_and_contexts(replay):
    x = replay
    a = x.adapter
    await real_wire(x)
    await a._handle_media_message(photo(x.bot, 10, '/queue hero', album=None), None)
    await finish_timers(a)
    entered, release = asyncio.Event(), asyncio.Event()
    request = x.bot.request
    original = request.do_request
    async def gate(url, method, request_data=None, **kwargs):
        if url.endswith('/sendMessage'):
            entered.set()
            await release.wait()
        return await original(url, method, request_data=request_data, **kwargs)
    request.do_request = gate
    await a._handle_media_message(photo(x.bot, 20, '/queue pricing'), None)
    await asyncio.wait_for(entered.wait(), 2)
    first = next(iter(a._media_dispatch_tasks))
    assert not a._media_group_tasks
    last_update = photo(x.bot, 21)
    for update in [photo(x.bot, 22), last_update, last_update]:
        await a._handle_media_message(update, None)
    # Wait only for the continuation; pricing's acknowledgement remains blocked.
    continuation = list(a._media_group_tasks.values())
    await asyncio.wait_for(asyncio.gather(*continuation), 2)
    assert not first.done()
    release.set()
    await finish_timers(a)
    events = waiting(x)
    for e in events:
        ledger=e.metadata.get('telegram_album_members')
        if ledger:
            assert e.media_urls == [url for key in sorted(ledger, key=int) for url in ledger[key][0]]
    contexts = [build_native_content_parts(e.text, e.media_urls)[0] for e in events]
    result = {'acknowledgments': [p['text'] for p in request.sent], 'captions': [e.text for e in events], 'image_counts': [sum(p['type'] == 'image_url' for p in c) for c in contexts], 'member_ids': [list(e.metadata.get('telegram_album_members', {})) for e in events], 'active_task_intact': not x.active.done()}
    record('awaited-ack', result)
    assert result['captions'] == ['hero', 'pricing']
    assert result['image_counts'] == [1, 3]
    assert len(request.sent) == 2
    assert not a._held_inbound_events
    assert not a._media_dispatch_tasks
    await x.bot.shutdown()


@pytest.mark.asyncio
async def test_reconnect_late_member_keeps_album_owner(replay):
    x = replay
    a = x.adapter
    await a._handle_media_message(photo(x.bot, 20, '/queue pricing'), None)
    await finish_timers(a)
    a._drop_delayed_deliveries = True
    update = photo(x.bot, 21)
    await a._handle_media_message(update, None)
    assert len(a._held_inbound_events) == 1
    held = a._held_inbound_events[0]
    a._drop_delayed_deliveries = False
    await a._redispatch_held_inbound()
    result = {'held_batch': held.metadata.get('telegram_media_batch'), 'requests': len(waiting(x)), 'captions': [e.text for e in waiting(x)], 'image_counts': [len(e.media_urls) for e in waiting(x)], 'ack_count': a._send_with_retry.await_count}
    record('reconnect-late-member', result)
    assert result['captions'] == ['pricing']
    assert result['image_counts'] == [2]


@pytest.mark.asyncio
async def test_cancel_after_handoff_during_redispatch_does_not_rehold(replay):
    x = replay
    a = x.adapter
    a._drop_delayed_deliveries = True
    await a._handle_media_message(photo(x.bot, 20, '/queue pricing', album=None), None)
    assert len(a._held_inbound_events) == 1
    a._drop_delayed_deliveries = False
    entered = asyncio.Event()
    async def blocked_send(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    a._send_with_retry = blocked_send
    task = asyncio.create_task(a._redispatch_held_inbound())
    a._held_inbound_redispatch_task = task
    await asyncio.wait_for(entered.wait(), 2)
    a._drop_delayed_deliveries = True
    await a._cancel_pending_delivery_tasks()
    first = {'requests':len(waiting(x)), 'held':len(a._held_inbound_events)}
    a._send_with_retry = AsyncMock(return_value=SendResult(True, message_id='99'))
    a._drop_delayed_deliveries = False
    await a._redispatch_held_inbound()
    result = {'after_cancel':first, 'after_recovery':{'requests':len(waiting(x)), 'captions':[e.text for e in waiting(x)], 'image_counts':[len(e.media_urls) for e in waiting(x)]}}
    record('redispatch-cancel', result)
    assert result['after_recovery']['captions'] == ['pricing']
    assert result['after_recovery']['image_counts'] == [1]


@pytest.mark.asyncio
async def test_after_model_admission_does_not_mutate_past_context(replay):
    x = replay
    a = x.adapter
    await a._handle_media_message(photo(x.bot, 20, '/queue pricing'), None)
    await finish_timers(a)
    admitted = _dequeue_pending_event(a, x.key)
    parts, _ = build_native_content_parts(admitted.text, admitted.media_urls)
    await a._handle_media_message(photo(x.bot, 21), None)
    await finish_timers(a)
    result = {'past_event_images':len(admitted.media_urls), 'serialized_past_images':sum(p['type']=='image_url' for p in parts), 'new_request_images':[len(e.media_urls) for e in waiting(x)], 'new_request_captions':[e.text for e in waiting(x)]}
    record('post-admission', result)
    assert result['past_event_images'] == result['serialized_past_images'] == 1
    assert result['new_request_images'] == [1]


@pytest.mark.asyncio
async def test_queue_clone_preserves_all_dataclass_fields_and_memory_receipt(replay):
    x = replay
    a = x.adapter
    from plugins.platforms.telegram.update_admission import attach_receipt
    a._inflight_update_ids = {'123:88': 1.0}
    a._inflight_with_event = set()
    a._seen_update_ids = {}
    event = a._build_message_event(photo(x.bot, 88).message, MessageType.PHOTO, update_id=88)
    event.text='/queue pricing'
    event.user_id='actor-7'
    event.user_name='actor name'
    event.media_urls=['/offline/image.jpg']
    event.media_types=['image/jpg']
    event.metadata={'telegram_media_batch':'album:owned', 'admission':'admitted', 'trusted_human_receipt':{'user':'7','thread':'3'}, 'gateway_session_key':x.key}
    attach_receipt(a, '123', 88, event)
    assert len(event.inbound_receipts) == 1
    before = {f.name:getattr(event, f.name) for f in dataclasses.fields(event)}
    await a.handle_message(event)
    queued=waiting(x)[0]
    for field,value in before.items():
        if field != 'text':
            assert getattr(queued,field) == value, field
    assert queued.metadata is not event.metadata
    assert queued.inbound_receipts is not event.inbound_receipts
    assert queued.text=='pricing'
    assert '123:88' not in a._inflight_update_ids
    assert not a._seen_update_ids
    record('queue-metadata',{'field_count_checked':len(before)-1,'receipts':len(queued.inbound_receipts),'claimed':bool(a._inflight_update_ids),'durable_receipt':bool(a._seen_update_ids)})


@pytest.mark.asyncio
@pytest.mark.parametrize('key', ['clover_plugin_id', 'clover_plugin_injection', 'gateway_session_key', 'gateway_session_id', 'gateway_session_strict'])
async def test_same_batch_different_trust_metadata_must_not_merge(replay, key):
    x=replay
    a=x.adapter
    original=a._build_message_event(photo(x.bot,20).message,MessageType.PHOTO)
    original.media_urls=['/offline/original.jpg']; original.media_types=['image/jpg']
    original.metadata={'telegram_media_batch':'album:owned',key:'trusted-A'}
    a._pending_messages[x.key]=original
    # Positive control first: an identical security context can merge.
    positive=dataclasses.replace(original, metadata=dict(original.metadata), media_urls=['/offline/positive.jpg'])
    assert x.runner._queue_or_replace_pending_event(x.key, positive)
    assert len(original.media_urls)==2
    incoming=dataclasses.replace(original,metadata={**original.metadata,key:'untrusted-B'},media_urls=['/offline/incoming.jpg'])
    accepted=x.runner._queue_or_replace_pending_event(x.key,incoming)
    record('security-'+key,{'accepted':accepted,'requests':len(waiting(x)),'original_images':original.media_urls,'original_value':original.metadata[key],'incoming_value':incoming.metadata[key]})
    assert accepted
    assert len(original.media_urls)==2
    assert len(waiting(x))==2


@pytest.mark.asyncio
async def test_unauthorized_late_member_never_enters_queue(replay):
    x=replay
    a=x.adapter
    await a._handle_media_message(photo(x.bot,20,'/queue pricing'),None)
    await finish_timers(a)
    x.runner._is_user_authorized=lambda source:False
    await a._handle_media_message(photo(x.bot,21),None)
    await finish_timers(a)
    assert [len(e.media_urls) for e in waiting(x)]==[1]
    assert a._send_with_retry.await_count==1


@pytest.mark.asyncio
async def test_stop_caption_bypasses_album_queue_policy(replay):
    x=replay
    seen=[]
    original=x.adapter._message_handler
    async def handler(event):
        seen.append(event.get_command())
        return 'Stopped' if event.get_command()=='stop' else await original(event)
    x.adapter._message_handler=handler
    await x.adapter._handle_media_message(photo(x.bot,20,'/stop'),None)
    await finish_timers(x.adapter)
    assert seen==['stop']
    assert not waiting(x)
    assert x.active.done()


@pytest.mark.asyncio
async def test_text_queue_commands_keep_separate_boundaries(replay):
    x=replay
    for n,text in [(30,'/queue first'),(31,'/queue second')]:
        event=x.adapter._build_message_event(photo(x.bot,n).message,MessageType.TEXT)
        event.text=text
        await x.adapter.handle_message(event)
    assert [e.text for e in waiting(x)]==['first','second']
    assert x.adapter._send_with_retry.await_count==2
    assert not x.active.done()


@pytest.mark.asyncio
async def test_native_single_album_ack_and_actual_model_admission(replay):
    x=replay
    await real_wire(x)
    for n,caption in [(20,'/queue pricing'),(21,None)]:
        await x.adapter._handle_media_message(photo(x.bot,n,caption),None)
    await finish_timers(x.adapter)
    queued=_dequeue_pending_event(x.adapter,x.key)
    x.runner._decide_image_input_mode=lambda **kwargs:'native'
    text=await x.runner._prepare_inbound_message_text(event=queued,source=queued.source,history=[],session_key=x.key)
    paths=x.runner._consume_pending_native_image_paths(x.key)
    parts,skipped=build_native_content_parts(text,paths)
    serialized=json.loads(json.dumps({'role':'user','content':parts}))
    result={'ack_count':len(x.bot.request.sent),'dequeued_requests':1,'model_image_count':sum(p['type']=='image_url' for p in serialized['content']),'caption_count':serialized['content'][0]['text'].count('pricing'),'remaining_requests':len(waiting(x))}
    record('single-album-model',result)
    assert not skipped
    assert result=={'ack_count':1,'dequeued_requests':1,'model_image_count':2,'caption_count':1,'remaining_requests':0}
    await x.bot.shutdown()


@pytest.mark.asyncio
async def test_real_admission_same_update_retry_after_cancel_and_handoff(replay):
    from plugins.platforms.telegram.update_admission import make_admission_handler,make_finalize_handler
    from telegram.ext import ApplicationHandlerStop
    x=replay
    a=x.adapter
    a._update_admission=True
    admit=make_admission_handler(a,'123')
    finalize=make_finalize_handler(a,'123')
    update=photo(x.bot,20,'/queue pricing')
    entered=asyncio.Event()
    original=a._message_handler
    async def blocked(event):
        entered.set()
        await asyncio.Event().wait()
    a._message_handler=blocked
    await admit(update,None)
    await a._handle_media_message(update,None)
    await asyncio.wait_for(entered.wait(),2)
    a._drop_delayed_deliveries=True
    await a._cancel_pending_delivery_tasks()
    assert len(a._held_inbound_events)==1
    with pytest.raises(ApplicationHandlerStop):
        await admit(update,None)
    a._message_handler=original
    a._drop_delayed_deliveries=False
    await a._redispatch_held_inbound()
    assert not a._inflight_update_ids
    # The exact same raw Update remains replayable without a false durable receipt.
    await admit(update,None)
    await a._handle_media_message(update,None)
    await finalize(update,None)
    await finish_timers(a)
    result={'requests':len(waiting(x)),'image_counts':[len(e.media_urls) for e in waiting(x)],'captions':[e.text for e in waiting(x)],'ack_count':a._send_with_retry.await_count,'durable_receipts':len(a._seen_update_ids),'inflight':len(a._inflight_update_ids)}
    record('admission-retry',result)
    assert result=={'requests':1,'image_counts':[1],'captions':['pricing'],'ack_count':1,'durable_receipts':0,'inflight':0}


@pytest.mark.asyncio
async def test_genuine_trusted_human_receipt_survives_queue_owner_copy(replay):
    from agent.workstream_origin import mint_human_ingress,redeem_human_ingress,ingress_is_valid
    x=replay
    def redeem(event):
        return redeem_human_ingress(event,platform='telegram',profile='disposable-review',session_id='offline-session',session_key=x.key,run_generation=1,is_current=lambda:True)
    control=x.adapter._build_message_event(photo(x.bot,90).message,MessageType.TEXT)
    control.text='control'
    assert mint_human_ingress(control,platform='telegram')
    proof=redeem(control)
    assert proof is not None and ingress_is_valid(proof, profile='disposable-review')
    event=x.adapter._build_message_event(photo(x.bot,91).message,MessageType.TEXT)
    event.text='/queue pricing'
    assert mint_human_ingress(event,platform='telegram')
    await x.adapter.handle_message(event)
    queued=waiting(x)[0]
    proof=redeem(queued)
    record('genuine-human-receipt',{'queued_copy_has_provenance':proof is not None,'original_still_has_provenance':redeem(event) is not None})
    assert proof is not None and ingress_is_valid(proof, profile='disposable-review')


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['user','chat','thread','internal','synthetic','control'])
async def test_real_queue_merge_retains_security_context_boundaries(replay,mutation):
    x=replay
    original=x.adapter._build_message_event(photo(x.bot,20).message,MessageType.PHOTO)
    original.media_urls=['/offline/original.jpg']; original.media_types=['image/jpg']
    original.metadata={'telegram_media_batch':'album:owned'}
    x.adapter._pending_messages[x.key]=original
    incoming=dataclasses.replace(original,metadata=dict(original.metadata),media_urls=['/offline/incoming.jpg'])
    if mutation in {'user','chat','thread'}:
        incoming.source=dataclasses.replace(original.source,**{mutation+'_id':'different'})
    elif mutation=='control':incoming.allow_gateway_control=False
    else:setattr(incoming,mutation,True)
    x.runner._queue_or_replace_pending_event(x.key,incoming)
    assert original.media_urls==['/offline/original.jpg']


@pytest.mark.asyncio
async def test_ack_send_error_does_not_drop_accepted_album(replay):
    x=replay
    x.adapter._send_with_retry=AsyncMock(side_effect=RuntimeError('offline ack failure'))
    await x.adapter._handle_media_message(photo(x.bot,20,'/queue pricing'),None)
    await finish_timers(x.adapter)
    assert [e.text for e in waiting(x)]==['pricing']
    assert [len(e.media_urls) for e in waiting(x)]==[1]
    assert not x.adapter._held_inbound_events


@pytest.mark.asyncio
async def test_handler_error_before_handoff_retains_work(replay):
    x=replay
    async def broken(event):
        raise RuntimeError('offline handler failure before queue admission')
    x.adapter._message_handler=broken
    await x.adapter._handle_media_message(photo(x.bot,20,'/queue pricing'),None)
    await finish_timers(x.adapter)
    result={'queued':len(waiting(x)),'held':len(x.adapter._held_inbound_events)}
    record('handler-error',result)
    assert result['queued']+result['held']==1


@pytest.mark.asyncio
@pytest.mark.parametrize('caption',['/queue pricing','pricing'])
async def test_native_bot_api_ack_cancellation_keeps_one_queue_owner(replay,caption):
    x=replay
    await real_wire(x)
    entered=asyncio.Event()
    attempts=[]
    request=x.bot.request
    original=request.do_request
    async def blocked(url,method,request_data=None,**kwargs):
        if url.endswith('/sendMessage'):
            attempts.append(json.loads(json.dumps(request_data.parameters)))
            entered.set()
            await asyncio.Event().wait()
        return await original(url,method,request_data=request_data,**kwargs)
    request.do_request=blocked
    await x.adapter._handle_media_message(photo(x.bot,20,caption),None)
    await asyncio.wait_for(entered.wait(),2)
    x.adapter._drop_delayed_deliveries=True
    await x.adapter._cancel_pending_delivery_tasks()
    result={'ack_attempts':len(attempts),'confirmed_wire_sends':len(request.sent),'queued_requests':len(waiting(x)),'held_events':len(x.adapter._held_inbound_events),'captions':[e.text for e in waiting(x)],'active_task_intact':not x.active.done()}
    record('native-cancel-'+('command' if caption.startswith('/') else 'ordinary'),result)
    await x.bot.shutdown()
    assert result['ack_attempts']==1
    assert result['confirmed_wire_sends']==0
    assert result['queued_requests']==1
    assert result['held_events']==0
    assert result['captions']==['pricing']
    assert result['active_task_intact']
