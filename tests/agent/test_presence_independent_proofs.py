"""Independent source-origin and unmocked resolver probes, synthetic HTTP only."""
from pathlib import Path
import asyncio
import importlib
import json
import time
import pytest
import httpx
import yaml


def test_candidate_module_origins(record_property):
    root = Path(__file__).resolve().parents[2]
    names = ['agent.auxiliary_client','agent.presence_initiation','agent.presence_lifecycle',
             'agent.presence_runtime','agent.presence_shadow','agent.presence_state',
             'agent.turn_context','agent.workstream_origin','agent.workstream_suggestions',
             'clover_cli.config','clover_state','cron.presence','gateway.presence_controls_host',
             'gateway.presence_delivery','gateway.run','plugins.platforms.telegram.adapter',
             'tui_gateway.presence','tui_gateway.server']
    origins = {name: str(Path(importlib.import_module(name).__file__).resolve()) for name in names}
    record_property('module_origins', json.dumps(origins,sort_keys=True))
    assert all(Path(origin).is_relative_to(root) for origin in origins.values())


@pytest.mark.parametrize('mode', ['success','rate_limit','redirect'])
def test_presence_uses_real_resolver_and_exactly_one_synthetic_http_request(monkeypatch,tmp_path,record_property,mode):
    from agent import auxiliary_client as aux
    home=tmp_path/'isolated-home';home.mkdir()
    monkeypatch.setenv('CLOVER_HOME',str(home))
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-review-key')
    (home/'config.yaml').write_text(yaml.safe_dump({'presence':{'budget_mode':'requests'},'auxiliary':{'presence':{
        'provider':'custom','model':'synthetic-review-model','base_url':'https://synthetic-review.invalid/v1',
        'key_env':'OPENAI_API_KEY'}}}))
    requests=[]
    clients=[]
    def sync_transport(request):
        raise AssertionError('presence resolver attempted a synchronous probe')
    async def async_transport(request):
        assert request.headers['authorization']=='Bearer synthetic-review-key'
        requests.append({'url':str(request.url),'payload':json.loads(request.content)})
        if mode=='rate_limit': return httpx.Response(429,json={'error':{'message':'synthetic quota','type':'rate_limit'}})
        if mode=='redirect': return httpx.Response(307,headers={'location':'https://forbidden-redirect.invalid/v1/chat/completions'})
        return httpx.Response(200,json={'id':'synthetic','object':'chat.completion','created':0,
            'model':'synthetic-review-model','choices':[{'index':0,'finish_reason':'stop',
             'message':{'role':'assistant','content':'synthetic success'}}]})
    def transport_kwargs(base_url,*,async_mode=False):
        client = (httpx.AsyncClient(transport=httpx.MockTransport(async_transport),follow_redirects=True)
                  if async_mode else httpx.Client(transport=httpx.MockTransport(sync_transport),follow_redirects=True))
        clients.append(client)
        return {'http_client':client}
    # Resolver, model/credential selection and native async SDK construction
    # remain real. Substitute only construction of synthetic HTTP transports.
    monkeypatch.setattr(aux,'_openai_http_client_kwargs',transport_kwargs)
    outcome=None
    started=time.perf_counter()
    # This positive integration proof includes cold config/auth/SDK setup. Use
    # the ordinary presence_runtime budget, not a 1s latency microbenchmark.
    # Deadline/cancellation behavior has separate controlled-clock proofs.
    try:
        outcome=asyncio.run(aux.presence_completion(messages=[{'role':'user','content':'synthetic bounded evidence'}],max_output_tokens=32,timeout=5))
    except Exception as exc:
        outcome=type(exc).__name__
    record_property('completion_wall_seconds',time.perf_counter()-started)
    record_property('actual_resolver_outcome',outcome)
    record_property('physical_synthetic_requests',json.dumps(requests))
    assert len(requests)==1
    assert clients and all(client.is_closed for client in clients)
    assert requests[0]['url']=='https://synthetic-review.invalid/v1/chat/completions'
    assert requests[0]['payload']['model']=='synthetic-review-model'
    assert requests[0]['payload']['max_tokens']==32
    assert outcome=='synthetic success' if mode=='success' else outcome!='synthetic success'
