"""Bounded current-scope SQLite ownership evidence; never SQL or parameters."""
from contextlib import contextmanager
import itertools
import os
from pathlib import Path
import sys
import threading
import time

_lock = threading.Lock()
_tokens = itertools.count()
_active = {}


def _caller():
    frame = sys._getframe(1)
    for _ in range(16):
        if frame is None:
            break
        filename = Path(frame.f_code.co_filename).name
        function = frame.f_code.co_name
        if (filename not in {"contextlib.py", "sqlite_safe_read.py", "sqlite_operation_observer.py"}
                and function not in {"_read_ctx", "_read_ctx_unlatched", "_execute_write"}):
            return f"{filename}:{function}:{frame.f_lineno}"
        frame = frame.f_back
    return "unknown"


@contextmanager
def observe_connection_operation(connection, path, kind):
    """Register only the live operation, with no database queries or FD opens."""
    key = str(Path(path).resolve()) if path is not None else ":memory:"
    try:
        stat = Path(path).stat() if path is not None else None
        device, inode = (stat.st_dev, stat.st_ino) if stat is not None else (None, None)
    except OSError:
        device = inode = None
    operation = {"pid": os.getpid(), "thread_id": threading.get_ident(),
                 "connection_id": hex(id(connection)), "kind": kind,
                 "phase": "begin" if kind == "write" else "read",
                 "device": device, "inode": inode, "caller": _caller(),
                 "started": time.monotonic()}
    token = next(_tokens)
    with _lock:
        _active[token] = (key, connection, operation)
    try:
        yield operation
    finally:
        with _lock:
            _active.pop(token, None)


def active_connection_operations(path):
    """Snapshots are candidates for attribution, not proof of a lock holder."""
    key = str(Path(path).resolve()) if path is not None else ":memory:"
    with _lock:
        matching = [(connection, dict(row)) for db, connection, row in _active.values() if db == key]
    result = []
    for connection, row in matching:
        row["age_seconds"] = max(0, time.monotonic() - row.pop("started"))
        try:
            row["in_transaction"] = connection.in_transaction
        except Exception:
            row["in_transaction"] = None
        result.append(row)
    return result
