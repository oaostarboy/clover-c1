"""Losing a weak pooled owner cannot become a concrete-token authority."""
import asyncio
import gc
import httpx
import pytest
from agent import auxiliary_client as aux, presence_authority as handoff
from agent.bounded_completion import CompletionHeld
from clover_constants import set_clover_home_override, reset_clover_home_override
from tests.test_presence_owner_lifecycle import selected_main, CONFIG
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


def test_garbage_collected_pool_is_revoked_not_downgraded_to_unowned_runtime_token(monkeypatch,tmp_path):
    agent,pool,owner=selected_main(tmp_path/'A')
    agent._credential_pool=None
    del pool
    gc.collect()
    assert owner.pool() is None
    (tmp_path/'A'/'auth.json').unlink()
    monkeypatch.setattr('clover_cli.config.load_config',lambda:CONFIG)
    requests=[]
    async def respond(request):
        requests.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'content':'must not send'}}]})
    monkeypatch.setattr(aux,'_openai_http_client_kwargs',lambda *a,**kw:{'http_client':
        httpx.AsyncClient(transport=httpx.MockTransport(respond))})
    async def run():
        token=set_clover_home_override(tmp_path/'A')
        try:
            with pytest.raises(CompletionHeld):
                with handoff.scoped(owner,lambda:True):
                    await aux.presence_completion(messages=[{'role':'user','content':'synthetic'}],
                        max_output_tokens=64,timeout=2)
        finally:reset_clover_home_override(token)
    asyncio.run(run())
    assert not requests
