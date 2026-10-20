"""Real owner capabilities with synthetic login/transport; no real networking."""
import time
from types import SimpleNamespace

import pytest

from cron.presence import ThoughtStore
from tests.gateway.test_presence_integration import setup_host
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from tests.tui_gateway.test_presence_integration import DesktopTransport, desktop_host
from tui_gateway import presence, server


def await_publication(state):
    deadline = time.monotonic()+8
    while not ThoughtStore().publications() or ThoughtStore().publications()[0]['state'] != state:
        assert time.monotonic() < deadline
        time.sleep(.01)


def test_real_owner_expired_ack_is_rejected_and_unknown_never_replays(monkeypatch, tmp_path, record_property):
    h, requests, _ = setup_host(monkeypatch, tmp_path)
    agent, transport, sid, session, send = desktop_host(h, monkeypatch)
    observed = []
    def write(frame):
        transport.frames.append(frame)
        if frame.get('params', {}).get('type') != 'presence.initiative':
            return True
        payload = frame['params']['payload']
        token = payload['ack_token']
        ticket = presence._tickets[token]
        assert presence.current(ticket) and ticket.context.copy().run(ticket.host.current)
        remaining = ticket.expires-time.monotonic()
        assert 0 < remaining <= 5, 'production expiry was widened'
        request = {'id': 'expired', 'method': 'presence.ack', 'params': {'session_id': sid, 'token': token}}
        # Advance only the owner's clock past its REAL issued ticket deadline.
        # No expiry replacement, artificial ACK, or real-time sleep as proof.
        with monkeypatch.context() as clock:
            clock.setattr(presence, 'time', SimpleNamespace(monotonic=lambda: ticket.expires+.001))
            reply = server.dispatch(request, transport=transport)
            assert reply['result']['accepted'] is False
            assert not ticket.event.is_set()
        observed.append((request, remaining, reply))
        return True
    monkeypatch.setattr(transport, 'write', write)
    try:
        send('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.')
        complete = send('Help verify deployment packaging')
        reply = server.dispatch({'id':'after', 'method':'presence.after_reply', 'params':{
            'session_id':sid,'token':complete['presence_after_reply_token']}}, transport=transport)
        assert reply['result']['accepted']
        await_publication('unknown')
        assert len(observed) == 1 and len(requests) == 1
        assert server.dispatch(observed[0][0], transport=transport)['result']['accepted'] is False
        send('Help verify deployment packaging')
        assert len(requests) == 1
        assert sum(f.get('params',{}).get('type') == 'presence.initiative' for f in transport.frames) == 1
        record_property('real_ticket_remaining_before_expiry_probe', str(observed[0][1]))
        record_property('expired_ack_accepted', 'false')
        record_property('durable_outcome', 'unknown; no replay')
    finally:
        agent.close()
        h.close()


@pytest.mark.parametrize('boundary', ['transport', 'principal_instance', 'session', 'revision', 'expired', 'new_input', 'stop'])
def test_inspect_retains_expiry_and_binding_then_mutation_is_single_use(monkeypatch, tmp_path, boundary):
    h, requests, _ = setup_host(monkeypatch, tmp_path)
    agent, transport, sid, session, send = desktop_host(h, monkeypatch)
    try:
        send('Carry thought: deployment packaging\nEvidence: manifest checksum before publishing.')
        complete = send('Help verify deployment packaging')
        assert server.dispatch({'id':'after', 'method':'presence.after_reply', 'params':{
            'session_id':sid, 'token':complete['presence_after_reply_token']}}, transport=transport)['result']['accepted']
        await_publication('acknowledged')
        frame = next(f for f in transport.frames if f.get('params',{}).get('type') == 'presence.initiative')
        token = frame['params']['payload']['control_token']
        ticket = presence._tickets[token]
        deadline = ticket.expires
        def control(action, actor=transport, session_id=sid):
            return server.dispatch({'id':action, 'method':'presence.control', 'params':{
                'session_id':session_id,'token':token,'action':action}}, transport=actor)['result']['accepted']
        assert control('inspect') and control('inspect')
        assert presence._tickets[token] is ticket and ticket.expires == deadline
        if boundary == 'transport':
            assert not control('stop', actor=DesktopTransport())
        elif boundary == 'principal_instance':
            identity = transport.auth_identity
            transport.auth_identity = dict(identity)
            assert not control('stop')
            transport.auth_identity = identity
        elif boundary == 'session':
            assert not control('stop', session_id='other-session')
        elif boundary == 'expired':
            with monkeypatch.context() as clock:
                clock.setattr(presence, 'time', SimpleNamespace(monotonic=lambda: deadline+.001))
                assert not control('inspect') and not control('stop')
            return
        elif boundary == 'revision':
            thought = next(t for t in ticket.host.state.inspect() if t.thought_id == ticket.thought_id)
            ticket.host.state.invalidate_source(thought.source, status='corrected')
            assert not control('stop')
            return
        elif boundary == 'new_input':
            send('New authenticated input')
            assert not control('inspect') and not control('stop')
            return
        else:
            ThoughtStore().stop()
            assert not control('inspect') and not control('stop')
            return
        assert control('stop')
        assert not control('stop') and not control('inspect')
        assert token not in presence._tickets
    finally:
        agent.close()
        h.close()
