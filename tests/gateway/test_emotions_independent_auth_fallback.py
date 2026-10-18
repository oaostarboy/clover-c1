"""Real resolver fallback after final sealing; synthetic multiplex owner only."""
import asyncio,json,traceback
from pathlib import Path
import pytest
from tests.gateway.test_emotions_independent_review import host,seed,capture_command

@pytest.mark.parametrize('revoked',[False,True])
def test_final_multiplex_transport_revocation_must_not_resolve_profile_after_seal(host,monkeypatch,revoked):
 from agent.emotions_snapshot import ReadFence
 runner=host.runner
 monkeypatch.setattr(runner.config,'multiplex_profiles',True)
 monkeypatch.setattr(host.adapter,'gateway_runner',runner)
 monkeypatch.setattr(runner,'_profile_name_for_source',lambda src:'synthetic-runtime')
 monkeypatch.setattr(runner,'_resolve_profile_home_for_source',lambda src:host.home)
 seed(host)
 event=capture_command(host,monkeypatch)
 assert event.source.profile=='synthetic-runtime'
 armed=False;calls=[];real_seal=ReadFence.seal;real_resolve=Path.resolve
 def resolve(path,*args,**kwargs):
  if armed:calls.append({'path':str(path),'stack':[f.name for f in traceback.extract_stack(limit=9)]})
  return real_resolve(path,*args,**kwargs)
 def seal(fence):
  nonlocal armed
  real_seal(fence)
  if revoked:
   runner.adapters.pop(event.source.platform,None)
   for adapters in (getattr(runner,'_profile_adapters',{}) or {}).values():
    if adapters.get(event.source.platform) is host.adapter:adapters.pop(event.source.platform)
  armed=True
 monkeypatch.setattr(ReadFence,'seal',seal)
 monkeypatch.setattr(Path,'resolve',resolve)
 try:
  text=asyncio.run(runner._handle_message(event)) or ''
 finally:armed=False
 print('POST_SEAL_PROFILE_FALLBACK',json.dumps({'revoked':revoked,'text':text,'calls':calls}))
 if revoked:assert 'confidence=' not in text
 else:assert 'Provider route:' in text
 assert calls==[],calls
