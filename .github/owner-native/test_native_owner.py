"""Additional native OS contracts; never substitutes product functions."""
import concurrent.futures
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import threading
import time

import pytest

SOURCE = Path(__file__).resolve().parents[3] / 'source'
sys.path.insert(0, str(SOURCE))
from gateway.pairing import VerifiedOwnerStore

WORKER = Path(__file__).with_name('worker.py')
TOKEN = '123456:test-token'


def child(op, home, **kw):
    p = subprocess.run([sys.executable, str(WORKER), op], input=json.dumps({'home': str(home), **kw}), text=True, capture_output=True, timeout=25)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def prepared(tmp_path):
    home = tmp_path / 'private-profile'
    home.mkdir(mode=0o700)
    s = VerifiedOwnerStore(TOKEN, home=home)
    code = s.prove(s.begin(), '12345678')
    return s, code


def test_native_process_replay_revoke_scope_and_unchanged_settings(tmp_path):
    s, code = prepared(tmp_path)
    config = s.home / 'config.yaml'
    config.write_text('proactive:\n  enabled: false\n')
    before = config.read_bytes()
    assert child('confirm', s.home, code=code) == '12345678'
    assert child('owner', s.home) == '12345678'
    assert child('confirm', s.home, code=code) is None
    assert not VerifiedOwnerStore('654321:other-bot', home=s.home).is_owner('12345678')
    assert child('revoke', s.home) is None
    assert child('owner', s.home) is None
    assert child('confirm', s.home, code=code) is None
    assert config.read_bytes() == before


def test_native_competing_lock_blocks_then_releases(tmp_path):
    s, code = prepared(tmp_path)
    lock = s._lock()
    with lock:
        p = subprocess.Popen([sys.executable, str(WORKER), 'lock'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        p.stdin.write(json.dumps({'home': str(s.home)}))
        p.stdin.close()
        lines = []
        ready = threading.Event()
        def read():
            for line in p.stdout:
                lines.append(line.strip())
                ready.set()
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        assert ready.wait(10), 'child did not reach native lock'
        time.sleep(0.4)
        assert lines == ['READY'] and p.poll() is None
    assert p.wait(timeout=15) == 0, p.stderr.read()
    thread.join(timeout=2)
    assert lines == ['READY', 'ACQUIRED']
    assert not s.is_owner('12345678')


def test_native_lock_namespace_failure_preserves_record(tmp_path):
    s, code = prepared(tmp_path)
    before = s.path.read_bytes()
    path = s.path.with_suffix('.lock')
    path.unlink()
    path.mkdir()
    with pytest.raises((OSError, RuntimeError)):
        s.confirm(code)
    assert s.path.read_bytes() == before
    assert not s.is_owner('12345678')


def test_native_permission_denial_and_private_directory_contract(tmp_path):
    s, code = prepared(tmp_path)
    before = s.path.read_bytes()
    if os.name == 'nt':
        import csv
        sid = list(csv.reader([subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True).strip()]))[0][1]
        # Test an explicit NTFS denial on a disposable directory, not POSIX chmod emulation.
        args = ['icacls', str(s.path.parent), '/deny', f'*{sid}:(W)']
        granted = subprocess.run(args, capture_output=True, text=True)
        assert granted.returncode == 0, granted.stdout + granted.stderr
        try:
            with pytest.raises(OSError):
                s.confirm(code)
            assert s.path.read_bytes() == before
        finally:
            removed = subprocess.run(['icacls', str(s.path.parent), '/remove:d', f'*{sid}'], capture_output=True, text=True)
            assert removed.returncode == 0, removed.stdout + removed.stderr
        acl = subprocess.run(['icacls', str(s.path)], capture_output=True, text=True)
        assert acl.returncode == 0
        print('NATIVE_INHERITED_ACL', acl.stdout)
        # Product does not provision a private ACL. Its documented private-home prerequisite remains.
    else:
        assert stat.S_IMODE(s.home.stat().st_mode) == 0o700
        assert stat.S_IMODE(s.path.stat().st_mode) == 0o600
        parent = s.path.parent
        old = stat.S_IMODE(parent.stat().st_mode)
        parent.chmod(0o500)
        try:
            with pytest.raises(PermissionError):
                s.confirm(code)
            assert s.path.read_bytes() == before
        finally:
            parent.chmod(old)
    assert not s.is_owner('12345678')
    assert s.confirm(code) == '12345678'


def test_native_actual_replace_failure_preserves_previous_record(tmp_path):
    s, code = prepared(tmp_path)
    before = s.path.read_bytes()
    # The actual target is made a directory; OS replacement must reject it.
    s.path.unlink()
    s.path.mkdir()
    from gateway.pairing import _secure_write
    with pytest.raises(OSError):
        _secure_write(s.path, before.decode())
    assert s.path.is_dir()
    assert list(s.path.parent.glob('*.tmp')) == []
    assert not s.is_owner('12345678')


def test_native_reader_contention_preserves_durable_confirmation(tmp_path):
    s, code = prepared(tmp_path)
    # CPython's native Windows open does not share DELETE; this exercises the
    # shared atomic writer's genuine retry/in-place fallback. POSIX replaces inode.
    with s.path.open('rb') as reader:
        before = reader.read()
        assert child('confirm', s.home, code=code) == '12345678'
        reader.seek(0)
        readback = reader.read()
        if os.name != 'nt':
            assert readback == before
        else:
            assert json.loads(readback)['owner'] == '12345678'
    assert child('owner', s.home) == '12345678'
    assert not list(s.path.parent.glob('*.tmp'))


def test_native_trusted_directory_symlink_limit(tmp_path):
    s, code = prepared(tmp_path)
    external = tmp_path / 'external-record.json'
    s.path.replace(external)
    try:
        s.path.symlink_to(external)
    except OSError as exc:
        if os.name == 'nt' and getattr(exc, 'winerror', None) == 1314:
            pytest.skip('native symlink creation requires an unavailable privilege; reparse behavior UNVERIFIED')
        raise
    assert s.confirm(code) == '12345678'
    assert s.path.is_symlink()
    assert json.loads(external.read_text())['owner'] == '12345678'
    # This measures, not repairs, the approved inherited trusted-directory limit.


@pytest.mark.parametrize('cancel', [False, True])
def test_native_console_hidden_confirmation_and_cancellation(tmp_path, cancel):
    s, code = prepared(tmp_path)
    (s.home / 'config.yaml').write_text('platforms:\n  telegram:\n    enabled: true\nproactive:\n  enabled: false\n')
    env = dict(os.environ, HOME=str(s.home), CLOVER_HOME=str(s.home), TELEGRAM_BOT_TOKEN=TOKEN, TERM='xterm-256color')
    command = [sys.executable, str(WORKER), 'console']
    transcript = ''
    if os.name == 'nt':
        from winpty import PtyProcess
        p = PtyProcess.spawn(subprocess.list2cmdline(command), cwd=str(SOURCE), env=env)
        try:
            deadline = time.monotonic() + 20
            while 'Telegram confirmation code' not in transcript and time.monotonic() < deadline:
                try:
                    transcript += p.read(4096, blocking=False)
                except EOFError:
                    break
                time.sleep(.05)
            assert 'Telegram confirmation code' in transcript, 'ConPTY did not reach actual password prompt'
            p.write('\x03' if cancel else code + '\r\n')
            deadline = time.monotonic() + 15
            while p.isalive() and time.monotonic() < deadline:
                try:
                    transcript += p.read(4096, blocking=False)
                except EOFError:
                    break
                time.sleep(.05)
            assert not p.isalive(), 'native console child exceeded fixed bound'
        finally:
            if p.isalive():
                p.terminate(force=True)
    else:
        import pty
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(SOURCE)
            os.execve(sys.executable, command, env)
        try:
            deadline = time.monotonic() + 20
            while 'Telegram confirmation code' not in transcript and time.monotonic() < deadline:
                if select.select([fd], [], [], .2)[0]:
                    transcript += os.read(fd, 4096).decode(errors='replace')
            assert 'Telegram confirmation code' in transcript
            os.write(fd, b'\x03' if cancel else (code + '\r').encode())
            deadline = time.monotonic() + 15
            done = False
            while time.monotonic() < deadline:
                if select.select([fd], [], [], .2)[0]:
                    try:
                        buf = os.read(fd, 4096)
                    except OSError:
                        break
                    if not buf:
                        break
                    transcript += buf.decode(errors='replace')
            waited, status = os.waitpid(pid, os.WNOHANG)
            assert waited == pid, 'PTY child exceeded fixed bound'
        finally:
            os.close(fd)
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
    assert code not in transcript, 'native echo exposed synthetic confirmation'
    assert 'GetPassWarning' not in transcript
    if cancel:
        assert not s.is_owner('12345678')
        assert 'verified and saved' not in transcript
    else:
        assert s.is_owner('12345678')
        assert 'verified and saved' in transcript
    assert 'enabled: false' in (s.home / 'config.yaml').read_text()
