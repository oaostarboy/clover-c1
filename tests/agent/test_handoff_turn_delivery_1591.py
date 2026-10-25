"""Real parent loop/finalizer -> actual base carrier -> scripted send sink.
No historical content. No mail tool. Persistence hooks are fixture spies.
"""
import asyncio,json,threading
from pathlib import Path
from unittest.mock import patch
import pytest,yaml
from tests.agent.provider_fixtures import scripted_provider_edges
from tests.tools.test_delegate_strict_background import harness
from tests.agent.test_handoff_completion_seam import _root,_tools,_text,_call,_declare
from tests.gateway.test_send_retry import _StubAdapter
from gateway.platforms.base import MessageEvent,MessageType
from gateway.session import SessionSource
from gateway.config import Platform
from agent.router_backstop import _pending_handoff_text
import tools.async_delegation as ad

DRAFT='Subject: Synthetic audit example\nHello Example Reader,\nThis draft exists only in the private audit.\nRegards, Example Writer'
NOTICE='Draft only. Not sent.\n\n'+DRAFT
OLD='OLD TURN: never send this old draft again.'

@pytest.fixture(autouse=True)
def registry():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()

@pytest.mark.asyncio
@pytest.mark.parametrize('case',['current','old','streamed','off','no-delegation','multiple','model-omission','stop','blocked'])
async def test_parent_final_to_actual_carrier(case,harness,tmp_path,monkeypatch):
    from clover_constants import get_clover_home
    from gateway import delivery_ledger as dl
    (get_clover_home()/'config.yaml').write_text(yaml.safe_dump({'delegation':{'checkpoint':{'enabled':case!='off'}}}))
    monkeypatch.setattr(dl,'_db_path',lambda:tmp_path/'carrier.db')
    first=tmp_path/'draft.txt';second=tmp_path/'second.txt'
    writes=[_call('write_file',{'path':str(first),'content':DRAFT})]
    if case=='multiple': writes.append(_call('write_file',{'path':str(second),'content':'second synthetic artifact'}))
    script=[_tools(_declare('direct')),_tools(*writes)]
    if case=='blocked':
        script[1].choices[0].message.content=NOTICE
        for i in range(4):
            script.append(_tools(_call('write_file',{'path':str(tmp_path/f'allowed-{i}.txt'),'content':'allowed synthetic work'})))
        for i in range(2):
            script.append(_tools(_call('write_file',{'path':str(tmp_path/f'blocked-{i}.txt'),'content':'must remain blocked'})))
        script.append(_text('unreachable after blocked checkpoint'))
    deleg=case not in {'no-delegation','blocked'}
    if deleg:
        handoff=_tools(_call('delegate_task',{'goal':'Review independent code only; never send email','handoff':{'work':'reviewing code','outcome':'the code review'}}))
        if case not in {'old','model-omission'}:handoff.choices[0].message.content=NOTICE
        script.append(handoff)
    script.append(_text(NOTICE))
    agent=_root(script);harness.hold=threading.Event()
    carrier=_StubAdapter();carrier.config.typing_indicator=False
    event=MessageEvent(text='Prepare a draft in this reply, do not send it. Separately review code.',message_type=MessageType.TEXT,source=SessionSource(platform=Platform.TELEGRAM,chat_id='synthetic',chat_type='dm'),message_id='parent-final')
    key='agent:main:telegram:dm:synthetic'; carrier._active_sessions[key]=asyncio.Event()
    result={}
    if case=='streamed':
        # Real interim-delivery callback records ownership after successful sink delivery.
        def interim(text,**kw):carrier._send_calls.append(('synthetic',text))
        agent.interim_assistant_callback=interim
    async def handler(_):
        with patch.object(agent,'_persist_session'),patch.object(agent,'_save_trajectory'),patch.object(agent,'_cleanup_task_resources'):
            r=agent.run_conversation(event.text,conversation_history=[{'role':'user','content':'Old request'},{'role':'assistant','content':OLD}])
        result.update(r)
        if case=='stop':
            carrier._active_sessions[key].set()
            carrier._pending_messages[key]=MessageEvent(text='/stop',source=event.source)
        return r['final_response']
    carrier._message_handler=handler
    # Keep pending Stop available through the stale-output decision; do not process another turn.
    if case=='stop': monkeypatch.setattr(carrier,'get_pending_message',lambda k:None)
    try:
        await carrier._process_message_background(event,key)
        sink=[v for _,v in carrier._send_calls]
        pending=_pending_handoff_text(agent,result['messages'])
        receipt={'case':case,'final':result['final_response'],'sink':sink,'pending':pending,'draft_body':DRAFT,'draft_exact':first.read_text()==DRAFT,'body_count':sum(v.count(DRAFT) for v in sink),'ack_count':sum(v.count('**delegated:**') for v in sink),'old_count':sum(v.count(OLD) for v in sink),'provider_calls':agent.client.chat.completions.create.call_count,'external_effects':0,'persistence_spied':True,'child_stub':True,'stop_scope':'carrier interrupt+pending-input suppression' if case=='stop' else None}
        Path(tmp_path, case + '.json').write_text(json.dumps(receipt,indent=2))
        assert receipt['draft_exact'] and receipt['old_count']==0
        if case in {'current','multiple'}:
            assert receipt['body_count']==1 and receipt['ack_count']==1
            assert NOTICE in result['final_response']
            assert NOTICE in pending # already generated, not model omission
        elif case in {'off','no-delegation','streamed'}:
            assert receipt['body_count']==1
            assert receipt['ack_count']==int(case=='streamed')
            assert NOTICE in '\n'.join(sink) # exact newlines, not only saved-path notice
        elif case in {'old','model-omission'}:
            assert receipt['body_count']==0 and receipt['ack_count']==1
            assert NOTICE not in pending # no deterministic reconstruction authority
        elif case=='stop': assert sink==[]
        elif case=='blocked':
            assert 'foreground allowance' in result['final_response']
            assert NOTICE in result['final_response'] and NOTICE in pending
            assert receipt['body_count']==1 and receipt['ack_count']==0
            assert receipt['provider_calls']==8
    finally:harness.hold.set()
