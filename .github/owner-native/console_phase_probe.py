"""Temporary safe native phase probe: no frame locals, source or console data."""
import atexit
import json
import os
from pathlib import Path
import sys
import threading
import traceback


def start():
    target = os.environ.get('OWNER_CONSOLE_PHASE_FILE')
    if not target:
        return lambda: None
    lock = threading.Lock()
    def phase(name, stack=False):
        data = {'phase': name}
        if stack:
            frame = sys._current_frames().get(threading.main_thread().ident)
            data['stack'] = [{'file': Path(x.filename).name, 'function': x.name, 'line': x.lineno}
                             for x in traceback.extract_stack(frame)]
        with lock, open(target, 'a', encoding='utf-8') as f:
            f.write(json.dumps(data) + '\n')
    phase('before-command')
    timer = threading.Timer(8, lambda: phase('command-still-active', True))
    timer.daemon = True
    timer.start()
    atexit.register(lambda: phase('python-atexit'))
    def done():
        phase('command-returned')
        timer.cancel()
        timer.join(timeout=2)
    return done
