"""Diagnostic-only pytest plugin; observes real persistence without SQL payloads.

Loaded explicitly by the bounded Windows A/B helper. No product file changes,
no persistence substitution, deadline changes, title suppression, or retries.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import pytest


_output = None
_output_lock = threading.Lock()


def pytest_addoption(parser):
    parser.addoption('--sqlite-diag-output', default=None)


def pytest_configure(config):
    global _output
    value = config.getoption('--sqlite-diag-output')
    if value:
        _output = Path(value)


def emit(event, **data):
    row = json.dumps(dict(event=event, pid=os.getpid(),
          monotonic=time.monotonic(), thread=threading.current_thread().name,
          thread_id=threading.get_ident(), **data), sort_keys=True)
    if _output is not None:
        with _output_lock:
            with _output.open('a', encoding='utf-8') as stream:
                stream.write(row + '\n')
    print('SQLITE_DIAG ' + row, flush=True)


@pytest.fixture(autouse=True)
def observe_activity_commit(monkeypatch):
    from tests.gateway.test_trusted_human_origin_ingress import Harness
    real_init = Harness.__init__

    def init(h, *args, **kwargs):
        real_init(h, *args, **kwargs)
        conn = h.db._conn
        original_class = type(conn)
        state = dict(activity_count=0, stage='ready', activity_thread=None)
        local = threading.local()
        database_list = [list(row) for row in conn.execute('PRAGMA database_list')]
        settings = {name: conn.execute('PRAGMA ' + name).fetchone()[0]
                    for name in ('journal_mode', 'synchronous', 'busy_timeout',
                                 'locking_mode', 'wal_autocheckpoint')}
        import clover_state
        emit('connection_ready', python=sys.version, executable=sys.executable,
             sqlite=sqlite3.sqlite_version, sqlite_threadsafety=sqlite3.threadsafety,
             connection_id=id(conn), connection_class=original_class.__name__,
             connection_creator_thread=threading.get_ident(),
             connection_check_same_thread=False, isolation_level=conn.isolation_level,
             in_transaction=conn.in_transaction, database_list=database_list,
             db_path=str(h.db_path.resolve()), settings=settings,
             state_module=clover_state.__file__,
             state_sha256=hashlib.sha256(Path(clover_state.__file__).read_bytes()).hexdigest())

        class ObservedConnection(original_class):
            def execute(self, sql, *a, **kw):
                # Never log statement text or bindings. Observe BEGIN only.
                observe = getattr(local, 'activity', False) and sql == 'BEGIN IMMEDIATE'
                if not observe:
                    return super().execute(sql, *a, **kw)
                began = time.monotonic()
                state['stage'] = 'begin'
                emit('begin_enter', connection_id=id(self), in_transaction=self.in_transaction)
                try:
                    return super().execute(sql, *a, **kw)
                finally:
                    state['stage'] = 'write_callback'
                    emit('begin_exit', connection_id=id(self),
                         elapsed=time.monotonic()-began, in_transaction=self.in_transaction)

            def commit(self):
                if not getattr(local, 'activity', False):
                    return super().commit()
                began = time.monotonic()
                state['stage'] = 'commit'
                emit('commit_enter', connection_id=id(self), in_transaction=self.in_transaction)
                try:
                    return super().commit()
                finally:
                    state['stage'] = 'after_commit'
                    emit('commit_exit', connection_id=id(self),
                         elapsed=time.monotonic()-began, in_transaction=self.in_transaction)

        # Keep the original tracking mixin and real sqlite3 implementation.
        # This is the same supported class-retag seam as sqlite_safe_read.
        conn.__class__ = ObservedConnection
        real_touch = h.db.touch_session_activity

        def touch(*a, **kw):
            state['activity_count'] += 1
            if state['activity_count'] != 1:
                return real_touch(*a, **kw)
            began = time.monotonic()
            state.update(stage='await_write_lock', activity_thread=threading.get_ident())
            done = threading.Event()
            local.activity = True
            emit('first_activity_enter', connection_id=id(conn),
                 lock_held=h.db._lock.locked(), in_transaction=conn.in_transaction)

            def watch():
                for delay in (2, 3, 10):
                    if done.wait(delay):
                        return
                    frames = {}
                    for tid, frame in sys._current_frames().items():
                        stack = []
                        while frame is not None:
                            stack.append(dict(file=Path(frame.f_code.co_filename).name,
                                              line=frame.f_lineno, function=frame.f_code.co_name))
                            frame = frame.f_back
                        frames[str(tid)] = stack[:14]
                    emit('slow_activity_snapshot', elapsed=time.monotonic()-began,
                         stage=state['stage'], activity_thread=state['activity_thread'],
                         lock_held=h.db._lock.locked(), frames=frames)
            watcher = threading.Thread(target=watch, name='sqlite-diagnostic-watch', daemon=True)
            watcher.start()
            try:
                return real_touch(*a, **kw)
            finally:
                local.activity = False
                done.set()
                emit('first_activity_exit', connection_id=id(conn),
                     elapsed=time.monotonic()-began, in_transaction=conn.in_transaction,
                     lock_held=h.db._lock.locked())
                watcher.join(timeout=1)
        monkeypatch.setattr(h.db, 'touch_session_activity', touch)

    monkeypatch.setattr(Harness, '__init__', init)
    yield
