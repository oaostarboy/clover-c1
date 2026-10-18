"""Two real gateway turns and adapter ACKs, overlapping selected HTTP owners.

The fixture delays only execution of the registered adapter callback until both
ordinary requested replies have real successful synthetic transport receipts.
"""
import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
import yaml

from agent import auxiliary_client as aux
from agent.credential_pool import load_pool, PooledCredential
from clover_constants import get_clover_home, set_clover_home_override, reset_clover_home_override
from cron.presence import ThoughtStore
from tests.gateway.test_presence_integration import setup_host
from tests.gateway.test_emotions_command import inbound_command
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401

REAL_RESOLVE = aux.resolve_provider_client


def test_two_real_after_reply_callbacks_preserve_selected_profile_accounts(monkeypatch, tmp_path, record_property):
    hosts = {}
    pending = []
    observed = []
    barrier = None
    async def respond(request):
        home = get_clover_home()
        h, pool, selected, bot = hosts[str(home)]
        runtime = aux._RUNTIME_MAIN_CONTEXT.get()
        assert runtime['credential_pool'] is pool and pool.current() is selected
        assert request.headers['authorization'] == 'Bearer ' + selected.runtime_api_key
        assert str(request.url) == 'https://api.openai.com/v1/chat/completions'
        payload = json.loads(request.content)
        assert payload['model'] == 'gpt-4o-mini' and payload['max_completion_tokens'] == 64
        affect = 'valence' in str(payload)
        with sqlite3.connect(ThoughtStore().path) as conn:
            reserved = conn.execute('SELECT COUNT(*) FROM presence_budget').fetchone()[0]
        assert reserved == (1 if affect else 2)
        observed.append((str(home), selected.id, affect))
        if not affect:
            if len([row for row in observed if not row[2]]) == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), 2)
            assert get_clover_home() == home
            assert aux._RUNTIME_MAIN_CONTEXT.get()['credential_pool'] is pool
        body = ({'valence':-.8,'arousal':.8,'confidence':.9} if affect else {
            'connection':'Deployment packaging can verify manifest checksum before publishing.',
            'evidence_indices':[0], 'confidence':.9})
        return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(body)}}]})
    def transport(*a, **kw):
        return {'http_client':httpx.AsyncClient(transport=httpx.MockTransport(respond))}
    try:
        for name in ('A', 'B'):
            h, unused, bot = setup_host(monkeypatch, tmp_path/name)
            selected = PooledCredential(provider='openai',id='selected-'+name,label='synthetic '+name,
                source='manual',auth_type='api_key',access_token='synthetic-selected-'+name,priority=0)
            sibling = PooledCredential(provider='openai',id='sibling-'+name,label='synthetic sibling',
                source='manual',auth_type='api_key',access_token='synthetic-sibling-'+name,priority=1)
            (h.home/'auth.json').write_text(json.dumps({'version':1,'authority_version':7,
                'credential_pool':{'openai':[selected.to_dict(),sibling.to_dict()]}}))
            pool=load_pool('openai')
            assert pool.select().id == 'selected-'+name
            selected=pool.current()
            hosts[str(h.home)] = (h,pool,selected,bot)
            cfg=yaml.safe_load((h.home/'config.yaml').read_text())
            cfg['model']={'provider':'openai','default':'gpt-4o-mini','base_url':'https://api.openai.com/v1'}
            cfg['auxiliary']['presence']={'provider':'auto','model':'auto'}
            cfg['presence']['budget_mode']='strict_tokens'
            cfg['proactive']['aux']={'max_calls_per_event':1,'max_calls_per_day':3,'max_output_tokens':64}
            (h.home/'config.yaml').write_text(yaml.safe_dump(cfg))
            monkeypatch.setattr(h.runner,'_resolve_session_agent_runtime',lambda pool=pool, selected=selected, **kw: (
                'gpt-4o-mini',dict(provider='openai',api_key=selected.runtime_api_key,
                base_url='https://api.openai.com/v1',api_mode='chat_completions',credential_pool=pool)))
            monkeypatch.setattr(aux,'resolve_provider_client',REAL_RESOLVE)
            monkeypatch.setattr(aux,'_openai_http_client_kwargs',transport)
            monkeypatch.setattr('agent.image_routing._lookup_supports_vision',lambda *a,**kw:False)
            h.inbound_human('I am frustrated. Please be brief.',message_id=100)
            assert 'confidence=0.90' in inbound_command(h,'/emotions')
            h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.',message_id=102)
            register=h.adapter.register_post_delivery_callback
            def defer(session_key, callback, *, generation=None, register=register, home=h.home):
                if getattr(callback,'__module__','') != 'agent.presence_runtime':
                    return register(session_key,callback,generation=generation)
                async def confirmed_then_delay():
                    pending.append((home,callback))
                return register(session_key,confirmed_then_delay,generation=generation)
            monkeypatch.setattr(h.adapter,'register_post_delivery_callback',defer)
            h.inbound_human('Help verify deployment packaging',message_id=103)
            assert h.results[-1]['final_response']=='ack'
            assert bot.send_message.await_count==0 and not unused
            def forbidden(*a,**kw): pytest.fail('optional callback selected/refreshed another account')
            monkeypatch.setattr(pool,'select',forbidden)
            monkeypatch.setattr(pool,'_refresh_entry',forbidden)
        assert len(pending)==2
        async def together():
            nonlocal barrier
            barrier=asyncio.Event()
            async def run(home,callback):
                token=set_clover_home_override(home)
                from agent.runtime_cwd import set_session_cwd, _SESSION_CWD
                # OfferHarness intentionally disables the gateway session-env
                # binder. Supply the public multiplex cwd binding, not owner
                # credentials or an authorization flag, in each task.
                cwd_token=set_session_cwd(str(hosts[str(home)][0].project))
                try:
                    before=aux._RUNTIME_MAIN_CONTEXT.get()
                    await callback()
                    assert aux._RUNTIME_MAIN_CONTEXT.get() is before
                    from agent.presence_authority import _ACTIVE
                    assert _ACTIVE.get() is None
                finally:
                    _SESSION_CWD.reset(cwd_token)
                    reset_clover_home_override(token)
            await asyncio.gather(*(run(home,cb) for home,cb in pending))
            await asyncio.gather(*(run(home,cb) for home,cb in pending))
        asyncio.run(together())
        assert len(observed)==4
        for home,(h,pool,selected,bot) in hosts.items():
            token=set_clover_home_override(Path(home))
            try:
                assert [row['state'] for row in ThoughtStore().publications()]==['acknowledged']
                assert pool.current() is selected and bot.send_message.await_count==1
            finally:
                reset_clover_home_override(token)
        record_property('overlapping_real_callback_owners',json.dumps(observed))
    finally:
        for h,pool,selected,bot in hosts.values():
            h.close()
