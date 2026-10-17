"""Locked pywinpty 2.0.15 console verifier; no product replacements.

PtyProcess.read() has no blocking keyword and can wait forever in its UTF-8
completion loop. Read its actual transport socket with a deadline and a stateful
decoder instead. Keep the real ConPTY in an owned, kill-on-close Windows job so
neither a library reader thread nor a terminal child can outlive a failed probe.

The locked wrapper's blocking reader can enter native read after the child has
exited and never return to its EOF check. Inherit its exact spawn/selector, but
own a nonblocking native-to-socket pump: drain native cache to an empty read AND
native EOF before closing the socket. Process exit alone is never transcript EOF.
"""
import codecs
import hashlib
import importlib.metadata
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import traceback
import time


class Transcript:
    def __init__(self, transport):
        self.transport = transport
        self.decoder = codecs.getincrementaldecoder('utf-8')()
        self.text = ''
        self.eof = False

    def until(self, deadline, needle=None):
        while not self.eof and (needle is None or needle not in self.text):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('ConPTY transcript exceeded fixed deadline')
            self.transport.settimeout(remaining)
            data = self.transport.recv(4096)
            self.eof = not data
            self.text += self.decoder.decode(data, final=self.eof)
        return self.text


# The reviewed CPython 3.11 / Windows x64 wheel from uv.lock. This binding has
# no Python backend getter. Its explicit constructor dispatches Some(0) ONLY to
# ConPTY::new, propagating errors without automatic/WinPTY fallback. Accept that
# identity contract only with these exact loaded artifacts, never just a label.
LOCKED_CONPTY = {
    'name': 'ConPTY', 'id': 0, 'pywinpty': '2.0.15',
    'verification': 'locked-explicit-constructor-no-fallback',
    'sha256': {
        'winpty/__init__.py': '95a4e2f6c2e308bc9cc9a4183b7c0cbe164d20adaf87fa4ebc64c94f639de10b',
        'winpty/enums.py': '7e11ef05f6f4722551d97b008819faed0fb33439541af6a4aa5c99d974627228',
        'winpty/ptyprocess.py': '388ad6ce2a59679cd94aa73b93ffe67a0099f725a47a94b300fd380eed06d87e',
        'winpty/winpty.cp311-win_amd64.pyd': '0bdbcda99f77f54ca1bb8fb10bbf9d78a721aac5f32ea95249254e19d3002298',
    },
}


def _locked_conpty():
    import winpty
    import winpty.enums as enums
    import winpty.ptyprocess as wrapper
    import winpty.winpty as native

    dist = importlib.metadata.distribution('pywinpty')
    if dist.version != LOCKED_CONPTY['pywinpty'] or native.__version__ != dist.version:
        raise RuntimeError('locked pywinpty version required for backend identity')
    modules = [winpty, enums, wrapper, native]
    observed = {}
    for relative, module in zip(LOCKED_CONPTY['sha256'], modules):
        path = Path(module.__file__).resolve()
        if path != Path(dist.locate_file(relative)).resolve():
            raise RuntimeError('locked pywinpty loaded artifact origin mismatch')
        observed[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if observed != LOCKED_CONPTY['sha256']:
        raise RuntimeError('locked pywinpty loaded artifact digest mismatch')
    if (wrapper.PTY is not native.PTY or winpty.PtyProcess is not wrapper.PtyProcess
            or enums.Backend.ConPTY != LOCKED_CONPTY['id']):
        raise RuntimeError('locked pywinpty backend API mismatch')
    names = [name for name, value in vars(enums.Backend).items()
             if not name.startswith('_') and value == LOCKED_CONPTY['id']]
    if names != [LOCKED_CONPTY['name']]:
        raise RuntimeError('locked pywinpty backend identity mismatch')
    identity = {'name': names[0], 'id': enums.Backend.ConPTY,
                'pywinpty': dist.version, 'verification': LOCKED_CONPTY['verification'],
                'sha256': observed}
    return wrapper.PtyProcess, native.PTY, identity


def _bounded_process_type(base):
    from winpty import WinptyError

    class ConsoleProcess(base):
        # Inherit the locked wheel's spawn (including explicit backend dispatch),
        # writes and process APIs. Only its Python transport lifetime is owned.
        def __init__(self, pty):
            self.pty = pty
            self.pid = pty.pid
            self.closed = False
            self.flag_eof = False
            self.read_blocking = False
            self.delayafterterminate = self.delayafterclose = .1
            self.native_eof = False
            self.reader_error = None
            self.reader_phase = 'starting'
            self.reader_calls = 0
            self._stop_reader = threading.Event()
            self.fileobj, self._server = socket.socketpair()
            self.fd = self.fileobj.fileno()
            self._thread = threading.Thread(target=self._pump, daemon=True)
            self._thread.start()

        def _pump(self):
            try:
                while not self._stop_reader.is_set():
                    try:
                        self.reader_phase = 'native-read-false'
                        self.reader_calls += 1
                        data = self.pty.read(4096, blocking=False)
                        self.reader_phase = 'native-read-returned'
                    except WinptyError as exc:
                        # Native cache EOF is a specific error in this binding;
                        # arbitrary native read errors must never become success.
                        if str(exc) != 'Standard out reached EOF':
                            raise
                        data = ''
                    if data:
                        self.reader_phase = 'socket-send'
                        self._server.sendall(data.encode('utf-8'))
                    # iseof alone does not account for native cached output.
                    # Keep reading cached chunks, even after the child is dead.
                    self.reader_phase = 'native-eof-check'
                    if not data and self.pty.iseof() and not self.pty.isalive():
                        self.native_eof = True
                        break
                    self._stop_reader.wait(.001)
            except BaseException as exc:
                self.reader_error = exc
            finally:
                self._server.close()

    return ConsoleProcess


def _close_console(p):
    try:
        p.close(force=True)
    finally:
        try:
            if hasattr(p, '_stop_reader'):
                p._stop_reader.set()
                # Locked isalive() sets closed=True without closing resources.
                # Do not let that bookkeeping skip our actual socket cleanup.
                try:
                    p.fileobj.close()
                finally:
                    p._server.close()
        finally:
            p._thread.join(timeout=2)
            assert not p._thread.is_alive(), 'pywinpty reader thread did not finish'


def _console(request):
    # Load only AFTER the parent assigns this process to its private job and
    # sends stdin. Any subsequently spawned ConPTY process inherits that job.
    import win32event

    process_type, native_type, identity = _locked_conpty()
    # Locked spawn uses `backend or env_override` then int(backend). The public
    # conversion accepts truthy '0', so it reaches native Some(0) even if the
    # environment requests WinPTY. Numeric zero would select auto/the override.
    p = _bounded_process_type(process_type).spawn(request['command'], cwd=request['cwd'],
                           env=request['env'], backend=str(identity['id']))
    try:
        if type(p.pty) is not native_type:
            raise RuntimeError('locked pywinpty returned a mismatched native PTY')
        reader = Transcript(p.fileobj)
        reader.until(time.monotonic() + 20, 'Telegram confirmation code')
        assert 'Telegram confirmation code' in reader.text, 'ConPTY did not reach actual password prompt'
        p.write('\x03' if request['cancel'] else request['code'] + '\r\n')
        deadline = time.monotonic() + 15
        # EOF, not merely process exit: include every buffered trailing byte in
        # the hidden-input assertion, including a split final UTF-8 character.
        reader.until(deadline)
        assert p.native_eof and p.reader_error is None, 'native transport did not drain to EOF'
        remaining = deadline - time.monotonic()
        assert remaining > 0, 'native console child exceeded fixed bound'
        waited = win32event.WaitForSingleObject(p.pty.fd, int(remaining * 1000))
        assert waited == win32event.WAIT_OBJECT_0 and not p.isalive(), 'native console child exceeded fixed bound'
        status = p.exitstatus
        assert status == (1 if request['cancel'] else 0), 'unexpected native console exit status'
        result = {'transcript': reader.text, 'exitstatus': status,
                  'backend': identity['name'], 'backend_identity': identity,
                  'eof': reader.eof}
    except BaseException:
        frame = sys._current_frames().get(p._thread.ident)
        safe = {'phase': 'nonblocking-console-failure', 'cancel': request['cancel'],
                'child_alive': p.pty.isalive(), 'child_exitstatus': p.exitstatus,
                'reader_alive': p._thread.is_alive(), 'reader_phase': p.reader_phase,
                'reader_calls': p.reader_calls, 'native_eof': p.native_eof,
                'reader_stack': [{'file': Path(x.filename).name, 'function': x.name, 'line': x.lineno}
                                 for x in traceback.extract_stack(frame)]}
        print(json.dumps(safe), file=sys.stderr, flush=True)
        raise
    finally:
        # This is library-resource cleanup, not an extension of either success
        # deadline. The outer 35s cap also bounds spawn/close/join themselves.
        _close_console(p)
    return result


def run_console(command, cwd, env, code, cancel):
    import win32api
    import win32con
    import win32job

    # pywin32 311's WCHAR* typemap rejects None. Empty Unicode creates an
    # unnamed job (asserted against ObjectNameInformation in native preflight).
    job = win32job.CreateJobObject(None, '')
    p = None
    try:
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        p = subprocess.Popen([sys.executable, str(Path(__file__).resolve())],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env, cwd=cwd)
        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, p.pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            win32api.CloseHandle(handle)
        request = {'command': command, 'cwd': cwd, 'env': env, 'code': code, 'cancel': cancel}
        # Same total as the original 20s prompt + 15s exit budgets, now also
        # bounding native calls and library cleanup. No retries or sleeps.
        stdout, stderr = p.communicate(json.dumps(request), timeout=35)
        assert p.returncode == 0, stderr
        result = json.loads(stdout)
        assert result['backend_identity'] == LOCKED_CONPTY, 'native backend identity was not established'
        assert result['backend'] == result['backend_identity']['name'] and result['eof'] is True
        assert result['exitstatus'] == (1 if cancel else 0)
        # Safe backend provenance only: never print the transcript or proof code.
        print(json.dumps({'console_backend_identity': result['backend_identity']}), flush=True)
        return result['transcript']
    finally:
        try:
            # Always terminate the private job, including on timeout/assertion.
            # This owns only this probe's reader/console/descendant processes.
            win32job.TerminateJobObject(job, 1)
            if p is not None:
                if p.poll() is None:
                    # Covers assignment failure before any request was sent.
                    p.kill()
                p.communicate(timeout=2)
            cleanup_deadline = time.monotonic() + 2
            while win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)['ActiveProcesses']:
                if time.monotonic() >= cleanup_deadline:
                    raise AssertionError('owned Windows console job retained a process')
        finally:
            win32api.CloseHandle(job)


if __name__ == '__main__':
    request = json.load(sys.stdin)
    print(json.dumps(_console(request)))
