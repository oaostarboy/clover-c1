"""Read-only stack/stage diagnostics; never records locals or request values."""
import asyncio
import faulthandler
import functools
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import pytest

DEST = Path(__file__).resolve().parents[5] / 'evidence'
OUT = DEST / ('diagnostic-' + str(os.getpid()) + '.jsonl')
LOCK = threading.Lock()
START = time.monotonic()
STACK = None
TIMES = {}

def note(kind, **values):
    with LOCK, OUT.open('a', encoding='utf-8') as f:
        f.write(json.dumps(dict(t=time.monotonic()-START, cpu=time.process_time(), pid=os.getpid(), kind=kind, **values))+'\n')

def sample():
    while True:
        time.sleep(4)
        frames = sys._current_frames()
        note('threads', stacks={str(tid): traceback.format_stack(frame) for tid, frame in frames.items() if tid != threading.get_ident()})

def pytest_configure(config):
    global STACK
    STACK = (DEST / ('faulthandler-'+str(os.getpid())+'.log')).open('w', encoding='utf-8')
    faulthandler.enable(STACK, all_threads=True)
    faulthandler.dump_traceback_later(8, repeat=True, file=STACK)
    threading.Thread(target=sample, daemon=True, name='settlement-diagnostic').start()
    note('configure', python=sys.executable, args=list(map(str, config.args)))

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    # Patch only diagnostic wrappers after normal fixtures imported product code.
    # Wrappers preserve every argument/result/exception and do not cache reads.
    from cron import presence
    patched = []
    def wrap(target, name):
        original = getattr(target, name)
        key = target.__name__+'.'+name
        @functools.wraps(original)
        def observed(*args, **kwargs):
            start=time.monotonic()
            try:
                return original(*args, **kwargs)
            finally:
                elapsed=time.monotonic()-start
                with LOCK:
                    count, wall, maximum = TIMES.get(key, (0,0,0))
                    TIMES[key]=(count+1,wall+elapsed,max(maximum,elapsed))
                if elapsed > .25:
                    note('slow_stage', function=key, wall=elapsed)
        setattr(target,name,observed)
        patched.append((target,name,original))
    for name in ['_validate_records', '_check_capacity']:
        wrap(presence,name)
    for name in ['put','runtime_state','source_is_current','reserve_assessment','propose','claim','attempt','acknowledge','advance_generation','stop','list','linked_key']:
        wrap(presence.ThoughtStore,name)
    note('call_start', nodeid=item.nodeid)
    try:
        yield
    finally:
        note('call_end', nodeid=item.nodeid, stages=TIMES.copy())
        for target,name,original in reversed(patched):
            setattr(target,name,original)

def pytest_runtest_logreport(report):
    note('phase', nodeid=report.nodeid, when=report.when, outcome=report.outcome, duration=report.duration)

def pytest_unconfigure(config):
    faulthandler.cancel_dump_traceback_later()
    note('end', stages=TIMES.copy())
    if STACK is not None:
        STACK.close()
