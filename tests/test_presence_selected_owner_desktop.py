"""Desktop worker -> captured exact selected owner -> real after-reply/return.

Only ordinary provider/HTTP and authenticated desktop transport are synthetic.
"""
import json
import time

import httpx
import pytest
import yaml

from agent import auxiliary_client as aux
from cron.presence import ThoughtStore
from tests.gateway.test_presence_integration import setup_host
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from tests.tui_gateway import test_presence_integration as desktop
from tests.test_presence_owner_lifecycle import selected_main, forbid_selection, CONFIG
from tui_gateway import presence

REAL_RESOLVE = aux.resolve_provider_client


@pytest.mark.parametrize('opportunity', ['after_reply', 'project_return'])
@pytest.mark.parametrize('source', ['exact', 'no-source'])
def test_desktop_selected_owner_not_identity_link_owns_callback(monkeypatch, tmp_path, opportunity, source):
    h, unused, _ = setup_host(monkeypatch, tmp_path)
    # selected_main's real auth file lives under this same temporary profile.
    selected_home = h.home/'selected'
    fixture_agent, pool, owner = selected_main(selected_home)
    (h.home/'auth.json').write_bytes((selected_home/'auth.json').read_bytes())
    # Reload at the actual main profile so its persisted provenance is genuine.
    from agent.credential_pool import load_pool
    pool = load_pool('openai')
    assert pool.select().id == 'selected'
    selected = pool.current()
    forbid_selection(monkeypatch, pool)
    config = yaml.safe_load((h.home/'config.yaml').read_text())
    config['model'] = CONFIG['model']
    config['auxiliary']['presence'] = CONFIG['auxiliary']['presence']
    config['presence'].update(budget_mode='strict_tokens', project_return_enabled=True)
    config['proactive']['aux'] = dict(max_calls_per_event=1, max_calls_per_day=3, max_output_tokens=64)
    (h.home/'config.yaml').write_text(yaml.safe_dump(config))
    real_agent = desktop.AIAgent
    if source == 'exact':
        def make_agent(**kwargs):
            kwargs.update(api_key=selected.runtime_api_key, base_url='https://api.openai.com/v1',
                          api_mode='chat_completions', credential_pool=pool)
            return real_agent(**kwargs)
        monkeypatch.setattr(desktop, 'AIAgent', make_agent)
    monkeypatch.setattr(aux, 'resolve_provider_client', REAL_RESOLVE)
    monkeypatch.setattr('agent.image_routing._lookup_supports_vision', lambda *a, **kw: False)
    observed = []
    async def respond(request):
        runtime = aux._RUNTIME_MAIN_CONTEXT.get()
        assert runtime['credential_pool'] is pool and pool.current() is selected
        assert request.headers['authorization'] == 'Bearer ' + selected.runtime_api_key
        assert str(request.url) == 'https://api.openai.com/v1/chat/completions'
        observed.append(request)
        return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps({
            'connection':'Deployment packaging can verify manifest checksum before publishing.',
            'evidence_indices':[0], 'confidence':.9})}}]})
    monkeypatch.setattr(aux, '_openai_http_client_kwargs', lambda *a, **kw: {
        'http_client':httpx.AsyncClient(transport=httpx.MockTransport(respond))})
    agent, transport, sid, session, send = desktop.desktop_host(h, monkeypatch)
    try:
        send('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.')
        # Project return must nominate a later real human context too; it is
        # never itself newly invented human evidence.
        complete = send('Help verify deployment packaging')
        if opportunity == 'after_reply':
            params = {'session_id':sid, 'token':complete['presence_after_reply_token']}
        else:
            params = {'session_id':sid}
        anchor = session['_presence_anchor']
        assert anchor.context.get(aux._RUNTIME_MAIN_CONTEXT) is None
        futures = []
        submit = desktop.server._pool.submit
        def collect(fn):
            future = submit(fn)
            futures.append(future)
            return future
        monkeypatch.setattr(desktop.server._pool, 'submit', collect)
        response = desktop.server.dispatch({'id':'owner', 'method':'presence.'+opportunity,
                                           'params':params}, transport=transport)
        assert response['result']['accepted'] is True
        assert len(futures) == 1
        futures[0].result(timeout=10)
        assert len(observed) == (1 if source == 'exact' else 0)
        rows = ThoughtStore().publications()
        assert [r['state'] for r in rows] == (['acknowledged'] if source == 'exact' else [])
        initiatives = [f for f in transport.frames if f.get('params',{}).get('type') == 'presence.initiative']
        assert len(initiatives) == (1 if source == 'exact' else 0)
        assert not unused
        if opportunity == 'after_reply':
            repeated = desktop.server.dispatch({'id':'again', 'method':'presence.after_reply',
                                               'params':params}, transport=transport)
            assert repeated['result']['accepted'] is False
    finally:
        agent.close()
        h.close()
