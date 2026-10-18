"""Lease expiry during synchronous owner readback must precede physical HTTP."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import httpx
import pytest
from agent import auxiliary_client as aux, presence_authority as handoff
from clover_constants import set_clover_home_override, reset_clover_home_override
from tests.test_presence_owner_lifecycle import selected_main, forbid_selection, CONFIG
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


def test_owner_lease_cannot_expire_inside_last_synchronous_read_and_dispatch(monkeypatch,tmp_path):
    agent,pool,owner=selected_main(tmp_path/'A')
    owner=replace(owner,expires=1)
    forbid_selection(monkeypatch,pool)
    tick=[0]
    armed=[False]
    monkeypatch.setattr(handoff,'time',SimpleNamespace(monotonic=lambda:tick[0]))
    read=handoff._pool_disk_state
    def cross_lease(*args):
        result=read(*args)
        if armed[0]:tick[0]=2
        return result
    monkeypatch.setattr(handoff,'_pool_disk_state',cross_lease)
    monkeypatch.setattr('clover_cli.config.load_config',lambda:CONFIG)
    requests=[]
    async def hook(request):armed[0]=True
    async def respond(request):
        requests.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'content':'must not publish'}}]})
    monkeypatch.setattr(aux,'_openai_http_client_kwargs',lambda *a,**kw:{'http_client':
        httpx.AsyncClient(transport=httpx.MockTransport(respond),event_hooks={'request':[hook]})})
    async def run():
        token=set_clover_home_override(tmp_path/'A')
        try:
            with handoff.scoped(owner,lambda:True):
                with pytest.raises(Exception):
                    await aux.presence_completion(messages=[{'role':'user','content':'synthetic'}],
                        max_output_tokens=64,timeout=2)
        finally:reset_clover_home_override(token)
    asyncio.run(run())
    assert not requests,'expired owner reached the physical HTTP boundary'
