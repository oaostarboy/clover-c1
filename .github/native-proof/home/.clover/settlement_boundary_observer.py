"""Diagnostic-only passive stage/SQLite clocks installed after fixture setup.

No imports of product or test modules, no policy/clock/assertion/return changes.
No arguments, locals, SQL, content, environment or capability tokens retained.
"""
import json
from pathlib import Path
import sys
import sqlite3
import threading
import time
import pytest

DEST = Path(__file__).resolve().parents[5] / 'evidence' / 'boundary-observation.jsonl'
EVENTS = []
CACHE = {}
ACTIVE = {}
LOCK = threading.Lock()
PREFIXES = ('/agent/presence_', '/agent/auxiliary_client.py', '/agent/title_generator.py',
            '/cron/presence.py', '/gateway/presence_delivery.py', '/tui_gateway/presence.py',
            '/tests/test_presence_joined_automatic.py', '/tests/tui_gateway/test_presence_',
            '/tests/gateway/test_workstream_suggestion_offer.py', '/gateway/platforms/base.py')
NAMES = {'inbound_human', 'send', 'write', 'respond', 'factory', 'completion', 'controlled',
         'resolve', 'transport', 'publish', '_publish', 'current', 'snapshot', 'commit',
         'reserve', 'assess_initiation', 'presence_completion', 'send_presence', 'handle_rpc',
         'acknowledge', 'unknown', 'propose', 'attempt', 'claim', '_transaction',
         'prepare_presence', 'prepare_publication', 'publish_current', '_execute_write',
         'append_message', 'append_messages_batch', 'generate_title', 'maybe_auto_title',
         'create', 'dispatch', 'stage_after_reply', 'run', 'issue', 'arm_owner',
         '_process_message_background', '_stop_typing_refresh', '_run_processing_hook',
         'follow_latest_message', '_handle_message', 'after_ack', 'chained'}

def record(event, **facts):
    EVENTS.append(dict(event=event, clock=time.monotonic(), cpu=time.process_time(),
                       thread=threading.get_ident(), **facts))

def stage(frame):
    code = frame.f_code
    if code not in CACHE:
        path = code.co_filename.replace('\\', '/')
        relevant = (any(p in path for p in PREFIXES) or path.endswith('/clover_state.py')
                    or path.endswith('/test_trusted_human_origin_ingress.py')
                    or path.endswith('/tui_gateway/server.py'))
        CACHE[code] = (Path(path).name + ':' + code.co_name) if relevant and code.co_name in NAMES else None
    return CACHE[code]

def profile(frame, event, arg):
    if event == 'call':
        label = stage(frame)
        if label:
            facts = {}
            if label == 'presence.py:issue':
                ticket = frame.f_locals.get('ticket')
                facts = dict(kind=getattr(ticket, 'kind', None), seconds_until_expiry=getattr(ticket, 'expires', 0)-time.monotonic())
            elif label == 'presence.py:handle_rpc':
                action = frame.f_locals.get('action')
                facts = dict(action=action if action in {'ack','after_reply','project_return','control','link'} else 'other')
            record('enter', stage=label, **facts)
    elif event == 'return':
        label = stage(frame)
        if label:
            record('return', stage=label, line=frame.f_lineno, result=arg if type(arg) in (bool, type(None)) else type(arg).__name__)
    elif event in ('c_call', 'c_return', 'c_exception'):
        if getattr(arg, '__name__', '') in ('commit', 'execute', 'rollback') and isinstance(getattr(arg, '__self__', None), sqlite3.Connection):
            record(event, stage='sqlite3.'+arg.__name__, caller=frame.f_code.co_name)

def stacks():
    result = []
    for tid, f in sys._current_frames().items():
        chain = []
        while f is not None and len(chain) < 20:
            chain.append(dict(file=Path(f.f_code.co_filename).name,
                              function=f.f_code.co_name, line=f.f_lineno))
            f = f.f_back
        result.append(dict(thread=tid, stack=chain))
    return result

def flush():
    with DEST.open('a', encoding='utf-8') as stream:
        for event in EVENTS:
            stream.write(json.dumps(event)+'\n')
    EVENTS.clear()

@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    if not (item.nodeid.startswith('tests/test_presence_joined_automatic.py') or item.nodeid.startswith('tests/tui_gateway/test_presence_renderer_bridge_preparation.py')):
        yield
        return
    record('case_start', nodeid=item.nodeid, python=sys.version)
    monkeypatch = item.funcargs['monkeypatch']
    # Load only now: the original offline network guard has already run.
    from tests.gateway.test_workstream_suggestion_offer import OfferHarness
    original_inbound = OfferHarness.inbound_human
    def inbound(host, *args, **kwargs):
        def counters():
            return dict(results=len(host.results), main=host.scripted.calls,
                        sends=host.adapter.send.await_count,
                        presence_sends=getattr(getattr(host.adapter._bot, 'send_message', None), 'await_count', 0),
                        active=len(host.adapter._active_sessions),
                        tasks=[dict(done=t.done(), cancelled=t.cancelled()) for t in host.adapter._session_tasks.values()])
        record('inbound_start', **counters())
        try:
            return original_inbound(host, *args, **kwargs)
        except BaseException as exc:
            record('inbound_exception', kind=type(exc).__name__, **counters(), threads=stacks())
            raise
        finally:
            record('inbound_end', **counters())
    monkeypatch.setattr(OfferHarness, 'inbound_human', inbound)
    if item.nodeid.startswith('tests/tui_gateway/'):
        from tests.tui_gateway import test_presence_renderer_bridge as bridge
        original_bridge = bridge.test_actual_desktop_native_frame_mounts_and_roundtrips_owner_ack
        def observed_bridge(*args, **kwargs):
            try:
                return original_bridge(*args, **kwargs)
            except BaseException as exc:
                tb = exc.__traceback__
                frames = []
                while tb is not None:
                    frames.append(dict(file=Path(tb.tb_frame.f_code.co_filename).name,
                                       function=tb.tb_frame.f_code.co_name, line=tb.tb_lineno))
                    tb = tb.tb_next
                record('bridge_exception', kind=type(exc).__name__, frames=frames, threads=stacks())
                raise
        monkeypatch.setattr(bridge, 'test_actual_desktop_native_frame_mounts_and_roundtrips_owner_ack', observed_bridge)
    done = threading.Event()
    def sample():
        while not done.wait(1):
            record('sample', threads=stacks())
    sampler = threading.Thread(target=sample, name='safe-stage-sampler', daemon=True)
    old = sys.getprofile()
    old_threads = threading.getprofile()
    sys.setprofile(profile)
    threading.setprofile(profile)
    sampler.start()
    try:
        outcome = yield
        if outcome.excinfo:
            record('case_exception', kind=outcome.excinfo[0].__name__, threads=stacks())
    finally:
        done.set()
        sampler.join(timeout=3)
        sys.setprofile(old)
        threading.setprofile(old_threads)
        record('case_end', nodeid=item.nodeid)
        flush()
