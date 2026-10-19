"""Mounted bridge setup is not a live ACK delivery budget.

These controls call the exact positive bridge, including its real server ACK
and unchanged five-second owner ticket. HTTP/credentials stay synthetic.
"""
import json
import time

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
