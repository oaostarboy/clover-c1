"""Mounted bridge setup is not a live ACK delivery budget.

These controls call the exact positive bridge, including its real server ACK
and unchanged five-second owner ticket. HTTP/credentials stay synthetic.
"""
import json
import time

import pytest

from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


def test_prepublication_preparation_does_not_spend_renderer_delivery_clock(monkeypatch, tmp_path, record_property):
    from tests.tui_gateway import test_presence_renderer_bridge as bridge
    original = bridge.desktop_host
    observed = []

    def prepared(*args, **kwargs):
        agent, transport, sid, session, send = original(*args, **kwargs)

        def slow_preparation(text):
            # Two ordinary requested turns precede any presence.after_reply or
            # five-second ACK ticket. Deliberately exceed the renderer's15s
            # global test budget without delaying a live ACK by even one tick.
            start = time.monotonic()
            time.sleep(8)
            value = send(text)
            observed.append(time.monotonic()-start)
            return value

        return agent, transport, sid, session, slow_preparation

    monkeypatch.setattr(bridge, 'desktop_host', prepared)
    bridge.test_actual_desktop_native_frame_mounts_and_roundtrips_owner_ack(monkeypatch, tmp_path, record_property)
    assert len(observed) == 2 and sum(observed) >= 16
    record_property('separate_prepublication_preparation_seconds', json.dumps(observed))


@pytest.mark.parametrize('control', ['missing_ack', 'expired_ack', 'stop', 'post_ack_stop'])
def test_prepared_bridge_cannot_certify_missing_expired_or_revoked_ack(monkeypatch, tmp_path, record_property, control):
    from cron.presence import ThoughtStore
    from tests.tui_gateway import test_presence_renderer_bridge as bridge
    from tui_gateway import presence, server

    original = server.dispatch
    properties = {}
    boundaries = []

    def record(name, value):
        properties[name] = value
        record_property(name, value)

    def controlled(request, *args, **kwargs):
        if request.get('method') != 'presence.ack':
            return original(request, *args, **kwargs)
        ticket = presence._tickets.get(request['params']['token'])
        assert ticket is not None
        boundaries.append(dict(seconds_until_expiry=ticket.expires-time.monotonic()))
        if control == 'missing_ack':
            request = dict(request, params=dict(request['params'], token='synthetic-unknown-ack'))
        elif control == 'expired_ack':
            ticket.expires = time.monotonic()-1  # Shorten only; never extend a capability.
        elif control == 'stop':
            ThoughtStore().stop()
        response = original(request, *args, **kwargs)
        assert isinstance(response, dict)
        if control == 'post_ack_stop':
            # A genuine positive owner ACK is not enough after revocation.
            assert response['result']['accepted'] is True
            ThoughtStore().stop()
        return response

    monkeypatch.setattr(server, 'dispatch', controlled)
    with pytest.raises(AssertionError):
        bridge.test_actual_desktop_native_frame_mounts_and_roundtrips_owner_ack(monkeypatch, tmp_path, record)
    assert len(boundaries) == 1 and 0 < boundaries[0]['seconds_until_expiry'] <= 5
    response = json.loads(properties['actual_owner_ack'])
    assert response['result']['accepted'] is (control == 'post_ack_stop')
    publications = ThoughtStore().publications()
    assert publications and all(p['state'] != 'acknowledged' for p in publications)
    record_property('negative_ack_control', json.dumps(dict(control=control, boundary=boundaries[0],
        actual_owner_accepted=response['result']['accepted'], states=[p['state'] for p in publications])))
