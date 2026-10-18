"""Real Telegram -> shared owner -> configured native request-count route."""
import json
import sqlite3
import yaml
import pytest
import httpx
from agent import auxiliary_client as aux
from cron.presence import ThoughtStore
from tests.gateway.test_presence_integration import setup_host
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from tests.agent.test_presence_provider_wire import route_fixture


@pytest.mark.parametrize('provider',['anthropic','gemini','openai-codex'])
def test_native_request_budget_is_reserved_before_http_shared_and_not_a_token_claim(monkeypatch,tmp_path,provider):
    resolver=aux.resolve_provider_client
    h,_,bot=setup_host(monkeypatch,tmp_path)
    monkeypatch.setattr(aux,'resolve_provider_client',resolver)
    requests=[]
    def respond(request):
        requests.append(request)
        # Real durable reservation precedes every physical dispatch.
        with sqlite3.connect(ThoughtStore().path) as db:
            assert db.execute('SELECT COUNT(*) FROM presence_budget').fetchone()[0]==len(requests)
        text=json.dumps({'affect': {'valence':-.8,'arousal':.8,'confidence':.9,
                                    'posture':'careful_brief'}, 'thought':None})
        if provider=='anthropic':return httpx.Response(200,json={'content':[{'type':'text','text':text}]})
        if provider=='gemini':return httpx.Response(200,json={'candidates':[{'content':{'parts':[{'text':text}]}}]})
        event={'type':'response.completed','response':{'output':[{'type':'message','content':[{'type':'output_text','text':text}]}]}}
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content='data: '+json.dumps(event)+'\n\n')
    config=yaml.safe_load((h.home/'config.yaml').read_text())
    native=route_fixture(monkeypatch,provider,respond)
    config['presence'].update(native['presence']);config['auxiliary']=native['auxiliary']
    (h.home/'config.yaml').write_text(yaml.safe_dump(config))
    from datetime import timedelta
    from agent import presence_runtime as runtime
    now = [runtime.clock()]
    monkeypatch.setattr(runtime, 'clock', lambda: now[0])
    try:
        for n in range(4):
            now[0] += timedelta(seconds=7)
            h.inbound_human(f'I am frustrated. Please be brief. Synthetic attempt {n}.',message_id=100+n)
        assert len(requests)==3 and len(h.results)==4
        assert len(h.scripted.requests)==4
        assert all('Tentative interaction posture: careful_brief' in str(r['messages'][-1]) for r in h.scripted.requests[:3])
        assert 'Tentative interaction posture' not in str(h.scripted.requests[3]['messages'][-1])
        from tests.gateway.test_emotions_command import inbound_command
        status=inbound_command(h,'/emotions',mid=110)
        assert 'daily_request_quota_exhausted' in status,status
        assert len(requests)==3
        assert bot.send_message.await_count==0
    finally:h.close()
