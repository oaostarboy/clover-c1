"""Test-only safe wall/CPU/import/IPC observation; no deadline or ACK mutation.

No faulthandler, ctypes, async signal handler, local-variable/payload capture,
network, process termination or live profile read. Observed runs are separate
experiments and cannot replace unchanged baseline REDs.
"""
import asyncio
import builtins
import functools
import importlib.machinery
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time

import pytest

DEST = Path(__file__).resolve().parents[5] / 'evidence'
EVENTS = []
LOCK = threading.Lock()
START = time.monotonic()
ITEM = ''
PS = None


def note(kind, **values):
    row = dict(kind=kind, t=time.monotonic()-START, cpu=time.process_time(),
               thread_cpu=time.thread_time(), tid=threading.get_ident(), nodeid=ITEM, **values)
    with LOCK:
        if len(EVENTS) < 30000:
            EVENTS.append(row)


def span_start(label, **values):
    note('enter', label=label, **values)
    return time.monotonic(), time.process_time(), time.thread_time()


def span_end(label, start, **values):
    note('exit', label=label, wall=time.monotonic()-start[0],
         cpu_delta=time.process_time()-start[1], thread_cpu_delta=time.thread_time()-start[2], **values)


def wrap(monkeypatch, owner, name, label):
    original = getattr(owner, name, None)
    if original is None:
        return
    if asyncio.iscoroutinefunction(original):
        @functools.wraps(original)
        async def async_observed(*args, **kwargs):
            start = span_start(label, deadline_remaining=(kwargs['deadline']-asyncio.get_running_loop().time()) if kwargs.get('deadline') else None)
            try:
                return await original(*args, **kwargs)
            except BaseException as exc:
                note('raise', label=label, exception=type(exc).__name__)
                raise
            finally:
                span_end(label, start)
        observed = async_observed
    else:
        @functools.wraps(original)
        def observed(*args, **kwargs):
            start = span_start(label)
            try:
                return original(*args, **kwargs)
            except BaseException as exc:
                note('raise', label=label, exception=type(exc).__name__)
                raise
            finally:
                span_end(label, start)
    monkeypatch.setattr(owner, name, observed)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    global ITEM, PS
    ITEM = item.nodeid
    mp = pytest.MonkeyPatch()
    try:
        import psutil
        PS = psutil.Process()
    except ImportError:
        pass
    note('test_begin', platform=platform.platform(), python=sys.version,
         cpu_count=os.cpu_count(), sdk_loaded=[n for n in ('anthropic','openai','telegram') if n in sys.modules])
    installed = set()
    def install_loaded():
        for module, names in {
            'agent.bounded_completion': ('_complete', '_dispatch_route', '_native_complete', 'remaining_budget'),
            'agent.auxiliary_client': ('resolve_provider_client',),
            'tui_gateway.server': ('_run_prompt_submit', 'dispatch'),
        }.items():
            owner = sys.modules.get(module)
            if owner is not None and module not in installed and all(hasattr(owner, n) for n in names):
                installed.add(module)
                for name in names:
                    wrap(mp, owner, name, module+'.'+name)
    install_loaded()
    original_import = builtins.__import__
    def observed_import(name, *args, **kwargs):
        t, c, tc = time.monotonic(), time.process_time(), time.thread_time()
        try:
            value = original_import(name, *args, **kwargs)
            if name.startswith(('agent', 'tui_gateway')):
                install_loaded()
            return value
        finally:
            wall = time.monotonic()-t
            if wall >= .025:
                note('import', name=name, wall=wall, cpu_delta=time.process_time()-c,
                     thread_cpu_delta=time.thread_time()-tc)
    mp.setattr(builtins, '__import__', observed_import)
    loader = importlib.machinery.SourceFileLoader
    get_data, compile_source = loader.get_data, loader.source_to_code
    def observed_read(self, path):
        t, c = time.monotonic(), time.process_time()
        try:
            data = get_data(self, path)
        except BaseException:
            raise
        else:
            wall = time.monotonic()-t
            if wall >= .01 or '/anthropic/' in path.replace('\\','/'):
                note('source_read', path=path, size=len(data), wall=wall, cpu_delta=time.process_time()-c)
            return data
    def observed_compile(self, data, path, **kwargs):
        t, c = time.monotonic(), time.process_time()
        try:
            return compile_source(self, data, path, **kwargs)
        finally:
            wall = time.monotonic()-t
            if wall >= .01 or '/anthropic/' in path.replace('\\','/'):
                note('source_compile', path=path, size=len(data), wall=wall, cpu_delta=time.process_time()-c)
    mp.setattr(loader, 'get_data', observed_read)
    mp.setattr(loader, 'source_to_code', observed_compile)
    # Observe subprocess start only; preserve the exact Popen class/contract.
    wrap(mp, subprocess.Popen, '__init__', 'subprocess.Popen')
    stop = threading.Event()
    main_id = threading.get_ident()
    tmp = item.funcargs.get('tmp_path')
    seen = set()
    def sample():
        while not stop.wait(.5):
            frames = []
            for tid, frame in sys._current_frames().items():
                if tid == threading.get_ident():
                    continue
                stack = []
                while frame is not None and len(stack) < 14:
                    stack.append(dict(file=frame.f_code.co_filename, function=frame.f_code.co_name, line=frame.f_lineno))
                    frame = frame.f_back
                frames.append(dict(tid=tid, main=tid == main_id, stack=stack))
            counters = {}
            if PS is not None:
                try:
                    counters = dict(io=PS.io_counters()._asdict(), threads=PS.num_threads(), rss=PS.memory_info().rss,
                                    children=[dict(pid=p.pid, cpu=p.cpu_times()._asdict(), io=p.io_counters()._asdict()) for p in PS.children(recursive=True)])
                except Exception as exc:
                    counters = dict(counter_error=type(exc).__name__)
            note('sample', frames=frames, **counters)
            if tmp is not None:
                for path in Path(tmp).glob('actual-native-frame.json*'):
                    try:
                        if path.name not in seen:
                            size = path.stat().st_size
                            seen.add(path.name)
                            note('ipc_file_first_seen', file=path.name, size=size)
                    except FileNotFoundError:
                        pass
    thread = threading.Thread(target=sample, name='deadline-stage-observer', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)
        mp.undo()
        note('test_end')
        ITEM = ''


def pytest_unconfigure(config):
    DEST.mkdir(exist_ok=True)
    (DEST / ('stage-trace-'+str(os.getpid())+'.json')).write_text(json.dumps(EVENTS, indent=2), encoding='utf-8')
