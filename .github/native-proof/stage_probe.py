"""Bounded diagnostic wrappers; no result/deadline/network-policy changes."""
import builtins
from contextlib import contextmanager
from contextvars import ContextVar
import functools
import inspect
import json
import os
from pathlib import Path
import sys
import time
import pytest

DEST = Path(__file__).resolve().parents[3] / 'evidence'
STACK = ContextVar('stage_probe_stack', default=())
STATS = {}
IMPORT_DEPTH = ContextVar('stage_probe_import_depth', default=0)


def measure(name, fn, *args, **kwargs):
    parent = STACK.get()
    frame = [name, time.perf_counter(), time.process_time(), 0., 0.]
    token = STACK.set(parent + (frame,))
    try:
        return fn(*args, **kwargs)
    finally:
        wall, cpu = time.perf_counter()-frame[1], time.process_time()-frame[2]
        STACK.reset(token)
        if parent:
            parent[-1][3] += wall
            parent[-1][4] += cpu
        row = STATS.setdefault(name, [0, 0., 0., 0., 0.])
        for i, v in enumerate((1, wall, cpu, wall-frame[3], cpu-frame[4])):
            row[i] += v


def wrapped(fn, name):
    @functools.wraps(fn)
    def call(*a, **kw):
        return measure(name, fn, *a, **kw)
    return call


def sample():
    import psutil
    p = psutil.Process()
    try:
        io = p.io_counters()._asdict()
    except (AttributeError, NotImplementedError, psutil.Error):
        io = None
    return dict(wall=time.perf_counter(), cpu=time.process_time(), io=io)


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_call(item):
    global STATS
    STATS = {}
    patches = []
    before = sample()
    def patch(obj, attr, value):
        patches.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)
    # Modules already naturally loaded by collection/setup only.
    targets = {
        'agent.presence_runtime': ['effective_policy', 'PresenceTurn.current', 'PresenceTurn.snapshot',
                                   'PresenceTurn.commit', 'SessionEvidence.read_text'],
        'agent.presence_authority': ['SelectedOwner.runtime', 'validate_active'],
        'agent.bounded_auth': ['_pool_disk_state', '_read_config', '_fingerprint'],
        'clover_cli.config': ['load_config'],
        'clover_cli.managed_scope': ['load_managed_config'],
    }
    for modname, attrs in targets.items():
        mod = sys.modules.get(modname)
        if mod is None:
            continue
        for attr in attrs:
            obj = mod
            bits = attr.split('.')
            for bit in bits[:-1]:
                obj = getattr(obj, bit)
            if hasattr(obj, bits[-1]):
                fn = getattr(obj, bits[-1])
                patch(obj, bits[-1], wrapped(fn, modname+'.'+attr))
    store = sys.modules.get('cron.presence')
    if store is not None:
        original = store.ThoughtStore._transaction
        @contextmanager
        def transaction(self, *a, **kw):
            caller = sys._getframe(2).f_code.co_name
            name = 'store.' + caller + ('.write' if kw.get('write') else '.read')
            cm = original(self, *a, **kw)
            value = measure(name+'.enter', cm.__enter__)
            try:
                yield value
            except BaseException:
                if not measure(name+'.exit', cm.__exit__, *sys.exc_info()):
                    raise
            else:
                measure(name+'.exit', cm.__exit__, None, None, None)
        patch(store.ThoughtStore, '_transaction', transaction)
    # Time only top-level imports during the real call; preserve exact imports.
    original_import = builtins.__import__
    def importer(name, *a, **kw):
        depth = IMPORT_DEPTH.get()
        token = IMPORT_DEPTH.set(depth+1)
        try:
            if depth == 0:
                return measure('import.'+name, original_import, name, *a, **kw)
            return original_import(name, *a, **kw)
        finally:
            IMPORT_DEPTH.reset(token)
    patch(builtins, '__import__', importer)
    try:
        yield
    finally:
        for obj, attr, value in reversed(patches):
            setattr(obj, attr, value)
        after = sample()
        dest = DEST / ('stage-'+str(os.getpid())+'.jsonl')
        with dest.open('a', encoding='utf-8') as f:
            f.write(json.dumps(dict(nodeid=item.nodeid,before=before,after=after,
                columns=['calls','inclusive_wall','inclusive_process_cpu','exclusive_wall','exclusive_process_cpu'],
                stats=STATS))+'\n')
