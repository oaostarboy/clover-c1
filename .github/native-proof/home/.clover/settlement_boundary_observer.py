"""Diagnostic-only passive stage/SQLite clocks installed after fixture setup.

No imports of product or test modules, no policy/clock/assertion/return changes.
No arguments, locals, SQL, content, environment or capability tokens retained.
"""
import json
from pathlib import Path
import sys
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
            '/tests/gateway/test_workstream_suggestion_offer.py')
NAMES = {'inbound_human', 'send', 'write', 'respond', 'factory', 'completion', 'controlled',
         'resolve', 'transport', 'publish', '_publish', 'current', 'snapshot', 'commit',
         'reserve', 'assess_initiation', 'presence_completion', 'send_presence', 'handle_rpc',
         'acknowledge', 'unknown', 'propose', 'attempt', 'claim', '_transaction',
         'prepare_presence', 'prepare_publication', 'publish_current', '_execute_write',
         'append_message', 'append_messages_batch', 'generate_title', 'maybe_auto_title',
         'create', 'dispatch', 'stage_after_reply', 'run', 'issue', 'arm_owner'}

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
            record('enter', stage=label)
    elif event == 'return':
        label = stage(frame)
        if label:
            record('return', stage=label, result=arg if type(arg) in (bool, type(None)) else type(arg).__name__)
    elif event in ('c_call', 'c_return', 'c_exception'):
        if getattr(arg, '__name__', '') in ('commit', 'execute', 'rollback') and type(getattr(arg, '__self__', None)).__module__ == 'sqlite3':
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
    record('case_start', nodeid=item.nodeid, python=sys.version)
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
