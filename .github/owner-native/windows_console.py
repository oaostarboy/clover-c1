"""Locked pywinpty 2.0.15 console verifier; no product replacements.

PtyProcess.read() has no blocking keyword and can wait forever in its UTF-8
completion loop. Read its actual transport socket with a deadline and a stateful
decoder instead. Keep the real ConPTY in an owned, kill-on-close Windows job so
neither a library reader thread nor a terminal child can outlive a failed probe.
"""
import codecs
import json
from pathlib import Path
import socket
import subprocess
import sys
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


def _console(request):
    # Load only AFTER the parent assigns this process to its private job and
    # sends stdin. Any subsequently spawned ConPTY process inherits that job.
    from winpty import PtyProcess
    from winpty.enums import Backend
    import win32event

    p = PtyProcess.spawn(request['command'], cwd=request['cwd'],
                         env=request['env'], backend=Backend.ConPTY)
    try:
        reader = Transcript(p.fileobj)
        reader.until(time.monotonic() + 20, 'Telegram confirmation code')
        assert 'Telegram confirmation code' in reader.text, 'ConPTY did not reach actual password prompt'
        p.write('\x03' if request['cancel'] else request['code'] + '\r\n')
        deadline = time.monotonic() + 15
        # EOF, not merely process exit: include every buffered trailing byte in
        # the hidden-input assertion, including a split final UTF-8 character.
        reader.until(deadline)
        remaining = deadline - time.monotonic()
        assert remaining > 0, 'native console child exceeded fixed bound'
        waited = win32event.WaitForSingleObject(p.pty.fd, int(remaining * 1000))
        assert waited == win32event.WAIT_OBJECT_0 and not p.isalive(), 'native console child exceeded fixed bound'
        status = p.exitstatus
        assert status == (1 if request['cancel'] else 0), 'unexpected native console exit status'
        result = {'transcript': reader.text, 'exitstatus': status,
                  'backend': 'ConPTY', 'eof': reader.eof}
    finally:
        # This is library-resource cleanup, not an extension of either success
        # deadline. The outer 35s cap also bounds spawn/close/join themselves.
        p.close(force=True)
        p._thread.join(timeout=2)
        assert not p._thread.is_alive(), 'pywinpty reader thread did not finish'
    return result


def run_console(command, cwd, env, code, cancel):
    import win32api
    import win32con
    import win32job

    job = win32job.CreateJobObject(None, None)
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
        assert result['backend'] == 'ConPTY' and result['eof'] is True
        assert result['exitstatus'] == (1 if cancel else 0)
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
