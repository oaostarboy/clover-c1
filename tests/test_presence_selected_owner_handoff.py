"""Actual main-selected persisted account -> posture/status -> initiative ownership."""
import json
import sqlite3
import httpx
import pytest
import yaml
from agent import auxiliary_client as aux
from agent.credential_pool import load_pool, PooledCredential
from cron.presence import ThoughtStore
from tests.gateway.test_presence_integration import setup_host
from tests.gateway.test_emotions_command import inbound_command
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401

REAL=aux.resolve_provider_client
@pytest.mark.parametrize('race', ['unchanged', 'delete-authority', 'replace-owner', 'expire-owner', 'source-changed', 'replace-object', 'select-sibling', 'stop', 'input', 'delete-evidence'])
def test_actual_callback_preserves_or_revokes_exact_main_owner_before_http(monkeypatch,tmp_path,record_property,race):
    h,unused,bot=setup_host(monkeypatch,tmp_path)
    key='sk-synthetic-selected-not-real'
    selected=PooledCredential(provider='openai',id='exact-selected',label='synthetic selected',source='manual',auth_type='api_key',access_token=key,priority=0)
    sibling=PooledCredential(provider='openai',id='unselected',label='synthetic unselected',source='manual',auth_type='api_key',access_token='sk-synthetic-unselected-not-real',priority=1)
    auth=h.home/'auth.json'
    auth.write_text(json.dumps({'version':1,'authority_version':7,'credential_pool':{'openai':[selected.to_dict(),sibling.to_dict()]}}))
    pool=load_pool('openai');assert pool.select().id=='exact-selected';owner=pool.current()
    cfg=yaml.safe_load((h.home/'config.yaml').read_text())
    cfg['model']={'provider':'openai','default':'gpt-4o-mini','base_url':'https://api.openai.com/v1'}
    cfg['auxiliary']['presence']={'provider':'auto','model':'auto'}
    cfg['presence']['budget_mode']='strict_tokens'
    cfg['proactive']['aux']={'max_calls_per_event':1,'max_calls_per_day':3,'max_output_tokens':64}
    (h.home/'config.yaml').write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(h.runner,'_resolve_session_agent_runtime',lambda **kw:('gpt-4o-mini',dict(
        provider='openai',api_key=key,base_url='https://api.openai.com/v1',api_mode='chat_completions',credential_pool=pool)))
    monkeypatch.setattr(aux,'resolve_provider_client',REAL)
    observed=[];outcomes=[]
    real_completion=aux.presence_completion
    async def completion(**kwargs):
        runtime=aux._RUNTIME_MAIN_CONTEXT.get() or {}
        item={'runtime_provider':runtime.get('provider'),
              'runtime_pool_is_selected_owner':runtime.get('credential_pool') is pool,
              'profile':str(h.home)}
        outcomes.append(item)
        try:
            result=await real_completion(**kwargs);item['outcome']='success';return result
        except Exception as exc:
            item['outcome']=getattr(exc,'reason',type(exc).__name__);raise
    monkeypatch.setattr(aux,'presence_completion',completion)
    # Pin only ordinary-main vision catalog discovery; its network seam is not
    # part of this presence ownership probe. All network guards remain active.
    monkeypatch.setattr('agent.image_routing._lookup_supports_vision',lambda *a,**kw:False)
    async def respond(request):
        payload=json.loads(request.content);affect='valence' in str(payload)
        assert request.headers['authorization']=='Bearer '+key
        assert str(request.url)=='https://api.openai.com/v1/chat/completions'
        assert payload['model']=='gpt-4o-mini' and payload['max_completion_tokens']==64
        assert pool.current() is owner
        with sqlite3.connect(ThoughtStore().path) as conn:
            reserved=conn.execute('SELECT COUNT(*) FROM presence_budget').fetchone()[0]
        assert reserved==len(observed)+1
        observed.append(dict(url=str(request.url),selected=pool.current().id,reserved=reserved,affect=affect))
        body=({'affect': {'valence':-.8,'arousal':.8,'confidence':.9,'posture':'careful_brief'}, 'thought':None} if affect else {
            'connection':'Deployment packaging can verify manifest checksum before publishing.',
            'evidence_indices':[0],'confidence':.9})
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(body)}}],'usage':{'completion_tokens':3}})
    monkeypatch.setattr(aux,'_openai_http_client_kwargs',lambda *a,**kw:{'http_client':httpx.AsyncClient(transport=httpx.MockTransport(respond))})
    armed=False
    callbacks=[]
    register=h.adapter.register_post_delivery_callback
    def register_owned(session_key, callback, *, generation=None):
        if getattr(callback, '__module__', '') != 'agent.presence_runtime':
            return register(session_key, callback, generation=generation)
        async def checked():
            before=aux._RUNTIME_MAIN_CONTEXT.get()
            if armed:
                data=json.loads(auth.read_text())
                if race=='delete-authority': auth.unlink()
                elif race=='replace-owner':
                    data['credential_pool']['openai'][0]['access_token']='synthetic-replaced'
                    auth.write_text(json.dumps(data))
                elif race=='expire-owner':
                    data['credential_pool']['openai'][0]['expires_at_ms']=1
                    auth.write_text(json.dumps(data))
                elif race=='source-changed':
                    data['authority_version']+=1
                    auth.write_text(json.dumps(data))
                elif race=='replace-object':
                    pool._entries[0]=PooledCredential.from_dict('openai',owner.to_dict())
                elif race=='select-sibling': pool._current_id='unselected'
                elif race=='stop': ThoughtStore().stop()
                elif race=='input': ThoughtStore().advance_generation()
                elif race=='delete-evidence':
                    row=h.user_rows()[1]['id']
                    h.db._execute_write(lambda conn:conn.execute('DELETE FROM messages WHERE id=?',(row,)))
                callbacks.append(callback)
            try:
                await callback()
            finally:
                assert aux._RUNTIME_MAIN_CONTEXT.get() is before
                from agent.presence_authority import _ACTIVE
                assert _ACTIVE.get() is None
        return register(session_key, checked, generation=generation)
    monkeypatch.setattr(h.adapter,'register_post_delivery_callback',register_owned)
    def forbidden(*a,**kw): pytest.fail('optional callback selected or refreshed another account')
    monkeypatch.setattr(pool,'select',forbidden)
    monkeypatch.setattr(pool,'_refresh_entry',forbidden)
    try:
        h.inbound_human('I am frustrated. Please be brief.',message_id=100)
        assert len(observed)==1 and not unused
        status=inbound_command(h,'/emotions')
        assert 'confidence=0.90' in status,status
        assert len(observed)==1
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.',message_id=102)
        armed=True
        h.inbound_human('Help verify deployment packaging',message_id=103)
        record_property('completion_outcomes',json.dumps(outcomes))
        record_property('selected_profile_wire',json.dumps(observed))
        record_property('emotions_status',status)
        print('SELECTED_MAIN_COMBINED_OBSERVATIONS',json.dumps(outcomes),json.dumps(observed))
        assert len(observed)==(2 if race=='unchanged' else 1)
        if race not in {'replace-object','select-sibling'}: assert pool.current() is owner
        import asyncio
        assert len(callbacks)==1
        asyncio.run(callbacks[0]())
        assert len(observed)==(2 if race=='unchanged' else 1)
        if race=='unchanged':
            assert [p['state'] for p in ThoughtStore().publications()]==['acknowledged']
            assert bot.send_message.await_count==1
        else:
            assert not ThoughtStore().publications() and bot.send_message.await_count==0
        assert h.results[-1]['final_response']=='ack'
        record_property('selected_profile_wire',json.dumps(observed))
        record_property('emotions_status',status)
        record_property('race',race)
    finally:h.close()
