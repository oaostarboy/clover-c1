"""Read-only diagnosis around the unchanged connected bridge test."""
import json
import time
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


def test_unchanged_native_renderer_bridge_ticket_diagnostics(monkeypatch,tmp_path,record_property):
    from tui_gateway import server,presence
    from tests.tui_gateway.test_presence_renderer_bridge import test_actual_desktop_native_frame_mounts_and_roundtrips_owner_ack
    real=server.dispatch
    def observe(request,*args,**kwargs):
        if request.get('method')=='presence.ack':
            ticket=presence._tickets.get(request['params']['token'])
            observation={'ticket_exists':ticket is not None}
            if ticket is not None:
                observation.update(seconds_until_expiry=ticket.expires-time.monotonic(),
                    authenticated_current=presence.current(ticket),
                    host_current=ticket.context.copy().run(ticket.host.current))
            record_property('ack_boundary_diagnostics',json.dumps(observation))
        return real(request,*args,**kwargs)
    monkeypatch.setattr(server,'dispatch',observe)
    test_actual_desktop_native_frame_mounts_and_roundtrips_owner_ack(monkeypatch,tmp_path,record_property)
