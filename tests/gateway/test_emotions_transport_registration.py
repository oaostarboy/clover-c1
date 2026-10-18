"""R4 registration identity must be sealed without calling a fallback resolver."""
import asyncio
import pytest
from tests.gateway.test_emotions_independent_review import host,seed,capture_command


@pytest.mark.parametrize('mutation',['none','default-map','adapter','reference','profile-map','profile-registry'])
def test_final_registered_transport_identity_is_local(host,monkeypatch,mutation):
    from agent.emotions_snapshot import ReadFence
    runner=host.runner
    platform=next(k for k,v in runner.adapters.items() if v is host.adapter)
    if mutation in {'profile-map','profile-registry'}:
        runner.adapters.pop(platform)
        monkeypatch.setattr(runner,'_profile_adapters',{'transport-owner':{platform:host.adapter}})
    monkeypatch.setattr(runner.config,'multiplex_profiles',True)
    monkeypatch.setattr(host.adapter,'gateway_runner',runner)
    monkeypatch.setattr(runner,'_profile_name_for_source',lambda src:'synthetic-runtime')
    monkeypatch.setattr(runner,'_resolve_profile_home_for_source',lambda src:host.home)
    seed(host)
    event=capture_command(host,monkeypatch)
    real_seal=ReadFence.seal
    real_resolver=runner._adapter_for_source
    armed=False;late=[]
    def resolve(source):
        if armed:late.append(True)
        return real_resolver(source)
    def seal(fence):
        nonlocal armed
        real_seal(fence)
        if mutation=='default-map':runner.adapters=dict(runner.adapters)
        elif mutation=='adapter':runner.adapters[platform]=object()
        elif mutation=='reference':event.source._transport_adapter_ref=lambda:host.adapter
        elif mutation=='profile-map':runner._profile_adapters['transport-owner']=dict(runner._profile_adapters['transport-owner'])
        elif mutation=='profile-registry':runner._profile_adapters=dict(runner._profile_adapters)
        armed=True
    monkeypatch.setattr(runner,'_adapter_for_source',resolve)
    monkeypatch.setattr(ReadFence,'seal',seal)
    try:text=asyncio.run(runner._handle_message(event)) or ''
    finally:armed=False
    assert not late,'general adapter resolver ran after seal'
    if mutation=='none':assert 'confidence=0.90' in text,text
    else:assert 'confidence=' not in text and 'withheld' in text,text
