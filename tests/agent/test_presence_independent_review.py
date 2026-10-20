"""Independent application-boundary probes; all provider/login/send data synthetic."""
import json
from types import SimpleNamespace
import pytest
import yaml
from tests.gateway.test_presence_integration import setup_host
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from cron.presence import ThoughtStore

@pytest.mark.parametrize('race', ['stop', 'policy_off', 'new_input'])
def test_revocation_before_current_sidecar_commit_must_remove_posture(monkeypatch, tmp_path, record_property, race):
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    real = h.db.set_latest_user_api_content
    observed = []
    def racing(session_id, content, api_content, **kw):
        if 'Tentative interaction posture' in api_content:
            if race == 'stop':
                ThoughtStore().stop()
            elif race == 'policy_off':
                config = yaml.safe_load((h.home/'config.yaml').read_text())
                config['presence']['enabled'] = False
                from clover_cli.config import save_config
                save_config(config)
            else:
                from tui_gateway import server
                from tests.tui_gateway.test_presence_integration import DesktopTransport
                transport = DesktopTransport()
                session = {'session_key': 'desktop-revocation-input', 'profile_home': str(h.home),
                           'clover_home': str(h.home), 'cwd': str(h.project), 'transport': transport}
                monkeypatch.setitem(server._sessions, 'independent-race', session)
                previous = ThoughtStore().generation()
                submission = server._capture_ws_human_submission({'method': 'prompt.submit',
                    'params': {'session_id': 'independent-race', 'text': 'New authenticated human input'}}, transport)
                assert submission is not None
                assert ThoughtStore().generation() == previous + 1
            observed.append(race)
        return real(session_id, content, api_content, **kw)
    monkeypatch.setattr(h.db, 'set_latest_user_api_content', racing)
    try:
        h.inbound_human('The previous attempt failed and I am frustrated. Please be brief.', message_id=100)
        assert observed == [race]
        assert len(requests) == 1 and bot.send_message.await_count == 0
        wire = h.scripted.requests[-1]['messages'][-1]['content']
        persisted = h.user_rows()[-1]['api_content']
        record_property('wire_content', wire)
        record_property('persisted_api_content', persisted)
        assert h.results[-1]['final_response'] == 'ack'
        assert 'Tentative interaction posture' not in wire, 'revoked optional context reached the ordinary provider request'
    finally:
        h.close()

def test_irrelevant_first_thought_must_not_starve_later_relevant_thought(monkeypatch, tmp_path, record_property):
    import agent.presence_state as state
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    ids = iter(['a'*32, 'b'*32])
    monkeypatch.setattr(state, 'uuid4', lambda: SimpleNamespace(hex=next(ids)))
    try:
        h.inbound_human('Carry thought: garden irrigation\nEvidence: drainage watering before planting.', message_id=100)
        h.inbound_human('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.', message_id=101)
        h.inbound_human('Help verify deployment packaging', message_id=102)
        before_requests = len(requests)
        before_sends = bot.send_message.await_count
        record_property('auxiliary_requests_before', json.dumps(requests))
        record_property('publications_before', json.dumps(ThoughtStore().publications()))
        # Positive control: remove only the unrelated first candidate. The same
        # genuine second source and provider path must now publish successfully.
        from agent.presence_state import ContextKey
        from agent.presence_runtime import clock
        from tests.gateway.test_workstream_suggestion_offer import scope
        key = ContextKey(scope(h), h.session_id())
        old = ThoughtStore().list(key)[0]
        assert old.summary == 'garden irrigation'
        assert ThoughtStore().invalidate(old, 'deleted', clock())
        h.inbound_human('Help verify deployment packaging', message_id=103)
        record_property('auxiliary_requests_after_positive_control', json.dumps(requests))
        assert len(requests) == 1 and bot.send_message.await_count == 1
        assert before_requests == 1 and before_sends == 1, 'first sorted irrelevant thought starved eligible relevant thought'
    finally:
        h.close()

def test_desktop_inspect_must_leave_stop_control_available(monkeypatch, tmp_path, record_property):
    import time
    from tests.tui_gateway.test_presence_integration import desktop_host
    from tui_gateway import server
    h, requests, bot = setup_host(monkeypatch, tmp_path)
    agent, transport, sid, session, send = desktop_host(h, monkeypatch)
    try:
        send('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.')
        complete = send('Help verify deployment packaging')
        response = server.dispatch({'id':'after','method':'presence.after_reply','params':{'session_id':sid,'token':complete['presence_after_reply_token']}},transport=transport)
        assert response['result']['accepted']
        deadline=time.monotonic()+7
        while not ThoughtStore().publications() or ThoughtStore().publications()[0]['state'] != 'acknowledged':
            assert time.monotonic()<deadline
            time.sleep(.02)
        frame=next(f for f in transport.frames if f.get('params',{}).get('type')=='presence.initiative')
        token=frame['params']['payload']['control_token']
        def control(action):
            return server.dispatch({'id':action,'method':'presence.control','params':{'session_id':sid,'token':token,'action':action}},transport=transport)['result']
        inspected=control('inspect')
        stopped=control('stop')
        record_property('inspect_response',json.dumps(inspected))
        record_property('stop_response',json.dumps(stopped))
        assert inspected['accepted'] is True
        assert stopped['accepted'] is True, 'read-only Inspect consumed the only card control capability'
    finally:
        agent.close()
        h.close()
