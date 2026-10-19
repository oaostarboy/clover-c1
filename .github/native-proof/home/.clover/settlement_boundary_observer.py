"""Non-acceptance boundary observer: no clock/return/assertion edits."""
import functools
import json
from pathlib import Path
import sys
import threading
import time

DEST = Path(__file__).resolve().parents[5] / 'evidence' / 'boundary-observation.jsonl'
EVENTS = []


def record(event, **facts):
    EVENTS.append(dict(event=event, clock=time.monotonic(), **facts))


def flush():
    with DEST.open('a', encoding='utf-8') as stream:
        for event in EVENTS:
            stream.write(json.dumps(event) + '\n')
    EVENTS.clear()


def stacks():
    result = []
    frames = sys._current_frames()  # Caller holds the GIL; no native watchdog.
    for thread in threading.enumerate():
        frame = frames.get(thread.ident) if thread.ident is not None else None
        chain = []
        while frame is not None and len(chain) < 16:
            chain.append(dict(file=Path(frame.f_code.co_filename).name,
                              function=frame.f_code.co_name, line=frame.f_lineno))
            frame = frame.f_back
        result.append(dict(name=thread.name, stack=chain))
    return result


def pytest_runtest_setup(item):
    record('case_setup', nodeid=item.nodeid)
    if item.nodeid.startswith('tests/tui_gateway/test_presence_renderer_bridge_preparation.py'):
        from tests.tui_gateway import test_presence_renderer_bridge as bridge
        from tui_gateway import server
        if not getattr(bridge.test_actual_desktop_native_frame_mounts_and_roundtrips_owner_ack,
                       '_boundary_observer', False):
            original = bridge.test_actual_desktop_native_frame_mounts_and_roundtrips_owner_ack

            @functools.wraps(original)
            def observed_bridge(*args, **kwargs):
                record('bridge_enter')
                try:
                    return original(*args, **kwargs)
                except BaseException as exc:
                    record('bridge_exception', kind=type(exc).__name__, message=str(exc)[:1500],
                           threads=stacks())
                    raise
                finally:
                    record('bridge_exit')
                    flush()
            setattr(observed_bridge, '_boundary_observer', True)
            bridge.test_actual_desktop_native_frame_mounts_and_roundtrips_owner_ack = observed_bridge
            dispatch = server.dispatch

            @functools.wraps(dispatch)
            def observed_dispatch(request, *args, **kwargs):
                method = request.get('method')
                if method in {'presence.after_reply', 'presence.ack'}:
                    record('dispatch_enter', method=method)
                response = dispatch(request, *args, **kwargs)
                if method in {'presence.after_reply', 'presence.ack'}:
                    record('dispatch_return', method=method,
                           accepted=response.get('result', {}).get('accepted') if isinstance(response, dict) else None)
                return response
            server.dispatch = observed_dispatch
    elif item.nodeid.startswith('tests/test_presence_joined_automatic.py'):
        from tests.gateway.test_workstream_suggestion_offer import OfferHarness
        if not getattr(OfferHarness.inbound_human, '_boundary_observer', False):
            original = OfferHarness.inbound_human

            @functools.wraps(original)
            def observed_inbound(self, *args, **kwargs):
                record('inbound_enter', results=len(self.results), sends=self.adapter.send.await_count)
                try:
                    return original(self, *args, **kwargs)
                except BaseException as exc:
                    record('inbound_exception', kind=type(exc).__name__, message=str(exc)[:1500],
                           results=len(self.results), sends=self.adapter.send.await_count,
                           active_sessions=len(self.adapter._active_sessions), threads=stacks())
                    raise
                finally:
                    record('inbound_exit', results=len(self.results), sends=self.adapter.send.await_count,
                           active_sessions=len(self.adapter._active_sessions))
                    flush()
            setattr(observed_inbound, '_boundary_observer', True)
            OfferHarness.inbound_human = observed_inbound


def pytest_runtest_teardown(item):
    record('case_teardown', nodeid=item.nodeid)
    flush()
