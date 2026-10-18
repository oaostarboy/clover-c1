"""Authenticated desktop server -> real agent -> presence producer -> renderer ACK.

Synthetic server-minted transport identity and HTTP provider; no external network.
"""
import threading
import time
import pytest
from types import SimpleNamespace
from pathlib import Path

from tests.gateway.test_presence_integration import setup_host, native_command
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from run_agent import AIAgent
from tui_gateway import server
from cron.presence import ThoughtStore


class DesktopTransport:
    closed = False
    acknowledge = True
    auth_identity = {'provider':'synthetic-login','user_id':'synthetic-human'}
    def __init__(self):
        self.frames = []
    def write(self, frame):
        self.frames.append(frame)
        event = frame.get('params', {})
        if event.get('type') == 'presence.initiative' and self.acknowledge:
            reply = server.dispatch({'jsonrpc':'2.0','id':'ack','method':'presence.ack',
                'params':{'session_id':event['session_id'],'token':event['payload']['ack_token']}}, transport=self)
            assert reply['result']['accepted'] is True
        return True


def desktop_host(h, monkeypatch):
    agent = AIAgent(api_key='synthetic-key', base_url='http://127.0.0.1:1/v1', provider='openai',
        model='gpt-4o-mini', enabled_toolsets=[], quiet_mode=True, skip_memory=True,
        skip_context_files=True, save_trajectories=False, platform='desktop', session_db=h.db,
        session_id='desktop-stored')
    transport = DesktopTransport()
    sid = 'desktop-runtime'
    session = dict(agent=agent, transport=transport, profile_home=str(h.home), profile='default',
        cwd=str(h.project), explicit_cwd=True, source='desktop', session_key='desktop-stored',
        history=[], history_lock=threading.RLock(), history_version=0, running=False,
        attached_images=[], edit_snapshots={}, tool_started_at={})
    monkeypatch.setitem(server._sessions, sid, session)
    monkeypatch.setattr(server, '_db', h.db)
    monkeypatch.setattr(server, '_clover_home', h.home)
    def send(text):
        expected = sum(f.get('params',{}).get('type') == 'message.complete' for f in transport.frames)+1
        submission = server._capture_ws_human_submission({'method':'prompt.submit',
            'params':{'session_id':sid,'text':text}}, transport)
        assert submission is not None
        session['running'] = True
        token = server.bind_transport(transport)
        try:
            server._run_prompt_submit('request', sid, session, text, trusted_human_submission=submission)
        finally:
            server.reset_transport(token)
        deadline = time.monotonic()+10
        while session.get('running') or sum(f.get('params',{}).get('type') == 'message.complete' for f in transport.frames) < expected:
            assert time.monotonic() < deadline, repr(transport.frames)
            time.sleep(0.02)
        complete = [f['params']['payload'] for f in transport.frames if f.get('params',{}).get('type') == 'message.complete'][-1]
        assert complete['status'] == 'complete'
        return complete
    return agent, transport, sid, session, send


@pytest.mark.parametrize('stop_kind', ['control', 'generic'])
@pytest.mark.parametrize('origin_surface', ['desktop', 'telegram'])
@pytest.mark.parametrize('opportunity', ['after_reply', 'project_return'])
def test_desktop_after_reply_owns_distinct_initiative_with_renderer_ack(monkeypatch, tmp_path, stop_kind, origin_surface, opportunity):
    h, requests, _ = setup_host(monkeypatch, tmp_path)
    if opportunity == 'project_return':
        import yaml
        config = yaml.safe_load((h.home/'config.yaml').read_text())
        config['presence']['project_return_enabled'] = True
        (h.home/'config.yaml').write_text(yaml.safe_dump(config))
    agent, transport, sid, session, send = desktop_host(h, monkeypatch)
    try:
        if origin_surface == 'telegram':
            # Linking setup is an explicit ordinary-posture control, not an assessment sample.
            send('Presence: ordinary. Help verify deployment packaging')
            h.inbound_human('Presence: ordinary. Prepare for explicit surface linking', message_id=100)
            challenge = native_command(h, '/proactive presence link', 101)
            assert 'Link code: ' in challenge, challenge
            code = challenge.split('Link code: ',1)[1].split()[0]
            linked = server.dispatch({'jsonrpc':'2.0','id':'link','method':'presence.link',
                'params':{'session_id':sid,'code':code,'confirmed':True}}, transport=transport)
            assert linked['result']['accepted'] is True
            h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=102)
        else:
            send('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.')
        if opportunity == 'after_reply':
            complete = send('Help verify deployment packaging')
            assert complete.get('presence_after_reply_token'), 'desktop has no actual presence producer'
            response = server.dispatch({'jsonrpc':'2.0','id':'after','method':'presence.after_reply',
                'params':{'session_id':sid,'token':complete['presence_after_reply_token']}}, transport=transport)
        else:
            # A view opportunity alone is not new human evidence. Admit later context first.
            send('Help verify deployment packaging')
            response = server.dispatch({'jsonrpc':'2.0','id':'return','method':'presence.project_return',
                'params':{'session_id':sid}}, transport=transport)
        assert response['result']['accepted'] is True
        deadline = time.monotonic()+7
        while not ThoughtStore().publications():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        while ThoughtStore().publications()[0]['state'] != 'acknowledged':
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert len(requests) == 1
        initiative = [f for f in transport.frames if f.get('params',{}).get('type') == 'presence.initiative']
        assert len(initiative) == 1
        assert initiative[0]['params']['payload']['focus'] is False
        control = initiative[0]['params']['payload'].get('control_token')
        assert control, 'desktop initiative has no server-owned controls'
        stolen = server.dispatch({'jsonrpc':'2.0','id':'stolen','method':'presence.control',
            'params':{'session_id':sid,'token':control,'action':'stop'}}, transport=DesktopTransport())
        assert stolen['result']['accepted'] is False
        if stop_kind == 'control':
            stopped = server.dispatch({'jsonrpc':'2.0','id':'stop','method':'presence.control',
                'params':{'session_id':sid,'token':control,'action':'stop'}}, transport=transport)
            assert stopped['result']['accepted'] is True
        else:
            stopped = server.dispatch({'jsonrpc':'2.0','id':'stop','method':'session.interrupt',
                'params':{'session_id':sid}}, transport=transport)
            assert stopped['result']['status'] == 'interrupted'
        assert ThoughtStore().initiative_status()[1] is True
        rows = h.db.get_messages('desktop-stored')
        expected_roles = ['user','assistant']*2
        assert [r['role'] for r in rows if r['role'] != 'session_meta'] == expected_roles
        assert all(r['content'] == 'ack' for r in rows if r['role'] == 'assistant')
    finally:
        h.close()


def test_desktop_frustration_is_ephemeral_current_compose_not_history_or_tools(monkeypatch, tmp_path):
    h, requests, _ = setup_host(monkeypatch, tmp_path)
    agent, transport, sid, session, send = desktop_host(h, monkeypatch)
    try:
        raw = 'The previous attempt failed and I am frustrated. Please be brief.'
        send(raw)
        first = h.scripted.requests[-1]['messages']
        assert 'Tentative interaction posture: careful_brief' in first[-1]['content']
        rows = h.db.get_messages('desktop-stored')
        current = next(r for r in rows if r['role'] == 'user')
        assert current['content'] == raw and current['api_content'] == first[-1]['content']
        send('Presence: ordinary. Continue deployment packaging.')
        second = h.scripted.requests[-1]['messages']
        assert second[:len(first)] == first
        assert 'Tentative interaction posture' not in second[-1]['content']
        assert h.scripted.requests[-1].get('tools') == h.scripted.requests[-2].get('tools')
        assert len(requests) == 1
    finally:
        agent.close()
        h.close()


@pytest.mark.parametrize('enabled', [True, False])
def test_busy_authenticated_submit_revokes_optional_work_without_attesting_queue(monkeypatch, tmp_path, enabled):
    import yaml
    h, requests, _ = setup_host(monkeypatch, tmp_path)
    agent, transport, sid, session, send = desktop_host(h, monkeypatch)
    try:
        if not enabled:
            config = yaml.safe_load((h.home/'config.yaml').read_text())
            config['presence']['enabled'] = False
            (h.home/'config.yaml').write_text(yaml.safe_dump(config))
        session['running'] = True
        before = ThoughtStore().generation()
        captured = server._capture_ws_human_submission({'method':'prompt.submit',
            'params':{'session_id':sid,'text':'New queued human input'}}, transport)
        assert captured is None, 'queue must not gain a new human-ingress receipt'
        assert ThoughtStore().generation() == before+(1 if enabled else 0)
        assert not requests
        if not enabled:
            assert not ThoughtStore().path.exists()
    finally:
        session['running'] = False
        agent.close()
        h.close()


def test_link_rpc_rejects_nonliteral_session_identifier_without_side_effect(monkeypatch, tmp_path):
    h, requests, _ = setup_host(monkeypatch, tmp_path)
    agent, transport, sid, session, send = desktop_host(h, monkeypatch)
    try:
        response = server.dispatch({'id':'invalid','method':'presence.link', 'params':{
            'session_id':[sid], 'code':'a'*24, 'confirmed':True}}, transport=transport)
        assert response['result']['accepted'] is False
        assert not requests and not ThoughtStore().path.exists()
    finally:
        agent.close()
        h.close()


def test_explicit_link_supports_desktop_source_telegram_send_once_across_surfaces(monkeypatch, tmp_path):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    agent, transport, sid, session, send = desktop_host(h, monkeypatch)
    try:
        send('Presence: ordinary. Help verify deployment packaging')
        h.inbound_human('Presence: ordinary. Prepare explicit surface linking', message_id=100)
        challenge = native_command(h, '/proactive presence link', 101)
        code = challenge.split('Link code: ',1)[1].split()[0]
        linked = server.dispatch({'id':'link-command','method':'command.dispatch', 'params':{
            'session_id':sid,'name':'proactive','arg':'presence link '+code+' confirm'}}, transport=transport)
        assert 'linked for future explicit thoughts' in linked['result']['output']
        send('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.')
        h.inbound_human('Help verify deployment packaging', message_id=102)
        assert len(requests) == 1 and bot.send_message.await_count == 1
        assert ThoughtStore().publications()[0]['state'] == 'acknowledged'
        complete = send('Help verify deployment packaging again')
        server.dispatch({'id':'repeat','method':'presence.after_reply', 'params':{
            'session_id':sid,'token':complete['presence_after_reply_token']}}, transport=transport)
        time.sleep(0.2)
        assert len(requests) == 1 and bot.send_message.await_count == 1
        assert not [f for f in transport.frames if f.get('params',{}).get('type') == 'presence.initiative']
    finally:
        agent.close()
        h.close()


def test_desktop_missing_renderer_ack_is_unknown_and_never_replayed(monkeypatch, tmp_path):
    h, requests, _ = setup_host(monkeypatch, tmp_path)
    agent, transport, sid, session, send = desktop_host(h, monkeypatch)
    transport.acknowledge = False
    try:
        send('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.')
        complete = send('Help verify deployment packaging')
        reply = server.dispatch({'id':'after','method':'presence.after_reply', 'params':{
            'session_id':sid,'token':complete['presence_after_reply_token']}}, transport=transport)
        assert reply['result']['accepted']
        deadline = time.monotonic()+7
        while not ThoughtStore().publications() or ThoughtStore().publications()[0]['state'] != 'unknown':
            assert time.monotonic() < deadline
            time.sleep(0.02)
        complete = send('Help verify deployment packaging again')
        server.dispatch({'id':'later','method':'presence.after_reply', 'params':{
            'session_id':sid,'token':complete['presence_after_reply_token']}}, transport=transport)
        time.sleep(0.2)
        assert len(requests) == 1
        assert len([f for f in transport.frames if f.get('params',{}).get('type') == 'presence.initiative']) == 1
        assert ThoughtStore().publications()[0]['state'] == 'unknown'
    finally:
        agent.close()
        h.close()


@pytest.mark.parametrize('race', ['credential','input','stop'])
def test_desktop_after_http_revocation_prevents_proposal_and_native_frame(monkeypatch, tmp_path, race):
    from agent import auxiliary_client as aux
    from agent.presence_runtime import PresenceTurn
    h, requests, _ = setup_host(monkeypatch, tmp_path)
    agent, transport, sid, session, send = desktop_host(h, monkeypatch)
    original, publish = aux.presence_completion, PresenceTurn.publish
    finished = threading.Event()
    observations = []
    async def racing(**kwargs):
        raw = await original(**kwargs)
        if race == 'credential':
            transport.auth_identity = dict(transport.auth_identity)
        elif race == 'input':
            submission = server._capture_ws_human_submission({'method':'prompt.submit',
                'params':{'session_id':sid,'text':'New authenticated input'}}, transport)
            observations.append(submission is not None)
        else:
            ThoughtStore().stop()
        observations.append(True)
        return raw
    async def observe(*args, **kwargs):
        try:
            return await publish(*args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(aux, 'presence_completion', racing)
    monkeypatch.setattr(PresenceTurn, 'publish', observe)
    try:
        send('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.')
        complete = send('Help verify deployment packaging')
        reply = server.dispatch({'id':'after','method':'presence.after_reply', 'params':{
            'session_id':sid,'token':complete['presence_after_reply_token']}}, transport=transport)
        assert reply['result']['accepted']
        assert finished.wait(7), 'actual desktop publisher did not finish'
        assert observations and all(observations)
        assert len(requests) == 1
        assert not ThoughtStore().publications()
        assert not [f for f in transport.frames if f.get('params',{}).get('type') == 'presence.initiative']
    finally:
        agent.close()
        h.close()
