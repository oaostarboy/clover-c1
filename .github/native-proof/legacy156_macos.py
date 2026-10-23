"""Native disposable launchd old-source update with a real tool/provider barrier.

The deterministic loopback provider is a fixture, not an external LLM. The
HTTP receiver records actual replies. Only product updater code may restart
the installed service after the initial installation. No credentials required.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.request

HELPERS = Path(__file__).resolve().parent
sys.path.insert(0, str(HELPERS))
import inflight_witness as witness
import legacy156_checks as legacy
from macos_external_env import native_guard, dump, sha, write_state, verify_state, identity


def read_json(path):
    return json.loads(Path(path).read_text())


def wait_for(fn, seconds=180):
    end = time.monotonic() + seconds
    while True:
        try:
            result = fn()
            if result:
                return result
        except (OSError, ValueError, KeyError, AssertionError, sqlite3.Error) as exc:
            last = repr(exc)
        else:
            last = 'condition false'
        if time.monotonic() >= end:
            raise RuntimeError('bounded observation expired: ' + last)
        time.sleep(1)


def request(path, data=None, timeout=20):
    req = urllib.request.Request('http://127.0.0.1:18791' + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Authorization': 'Bearer native-macos-fixture-only',
                 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


class LocalProvider:
    def __init__(self, evidence, effect, token, reply):
        self.evidence, self.effect, self.token, self.reply = evidence, effect, token, reply
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                self.send_json({'object': 'list', 'data': [{'id': 'native-fixture', 'object': 'model'}]})
            def send_json(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                tools = [m for m in data.get('messages', []) if m.get('role') == 'tool']
                owner.calls.append({'time': time.time(), 'path': self.path,
                    'stream': data.get('stream'), 'tool_results': len(tools),
                    'offered_tools': [t.get('function', {}).get('name') for t in data.get('tools', [])]})
                dump(evidence / 'provider-observations.json', owner.calls)
                if tools:
                    # The real agent has executed the real terminal tool, and
                    # now awaits this response. No product methods are patched.
                    owner.entered.set()
                    if not owner.release.wait(720):
                        raise TimeoutError('test barrier not released')
                    message = {'role': 'assistant', 'content': owner.reply}
                    finish = 'stop'
                else:
                    names = [t.get('function', {}).get('name') for t in data.get('tools', [])]
                    if 'terminal' not in names:
                        raise RuntimeError('real terminal tool not offered: ' + repr(names))
                    command = '/usr/bin/printf ' + shlex.quote('%s\n') + ' ' + shlex.quote(owner.token) + ' >> ' + shlex.quote(str(owner.effect))
                    message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                        'id': 'native-call-once', 'type': 'function', 'function': {
                            'name': 'terminal', 'arguments': json.dumps({'command': command, 'timeout': 10})}}]}
                    finish = 'tool_calls'
                if data.get('stream'):
                    # Real wire streaming, for agents whose SDK uses streaming
                    # even when platform display streaming is disabled.
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    delta = dict(message)
                    if 'tool_calls' in delta:
                        delta['tool_calls'][0]['index'] = 0
                    chunks = [{'id': 'fixture', 'object': 'chat.completion.chunk', 'created': int(time.time()),
                        'model': 'native-fixture', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                        {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': int(time.time()),
                        'model': 'native-fixture', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}]
                    for chunk in chunks:
                        self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                    self.wfile.write(b'data: [DONE]\n\n')
                    self.wfile.flush()
                else:
                    self.send_json({'id': 'fixture', 'object': 'chat.completion', 'created': int(time.time()),
                        'model': 'native-fixture', 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
                        'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}})
        self.server = ThreadingHTTPServer(('127.0.0.1', 18792), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class LocalIRC:
    def __init__(self, evidence):
        self.evidence, self.clients = evidence, []
        self.joined = threading.Event()
        self.lock = threading.Lock()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 16667))
        self.sock.listen(8)
        threading.Thread(target=self.accept, daemon=True).start()
    def log(self, text):
        with self.lock, (self.evidence / 'irc.log').open('a') as out:
            out.write(str(time.time()) + ' ' + text + '\n')
    def send(self, client, text):
        client['sock'].sendall((text + '\r\n').encode())
        self.log('-> ' + text)
    def accept(self):
        while True:
            sock, _ = self.sock.accept()
            c = {'sock': sock, 'nick': '', 'ready': False}
            self.clients.append(c)
            threading.Thread(target=self.handle, args=(c,), daemon=True).start()
    def handle(self, c):
        try:
            for raw in c['sock'].makefile('rb'):
                line = raw.decode().strip()
                self.log('<< ' + line)
                parts = line.split()
                if not parts:
                    continue
                if parts[0] == 'NICK':
                    c['nick'] = parts[1]
                elif parts[0] == 'USER':
                    self.send(c, ':fixture 001 ' + c['nick'] + ' :Welcome')
                    c['ready'] = True
                elif parts[0] == 'JOIN':
                    self.send(c, ':' + c['nick'] + '!bot@localhost JOIN ' + parts[1])
                    self.joined.set()
                elif parts[0] == 'PING':
                    self.send(c, ':fixture PONG fixture :' + parts[-1].lstrip(':'))
        except OSError:
            pass
        finally:
            c['ready'] = False
    def update(self):
        c = [c for c in self.clients if c['ready']][-1]
        self.send(c, ':ant!ant@localhost PRIVMSG ' + c['nick'] + ' :/update')


def observe(home, source, expected, live):
    import psutil
    from clover_cli.gateway import get_launchd_label, _locate_launchd_gateway_service
    state = read_json(home / 'gateway_state.json')
    assert state['code_sha'] == expected, state
    assert state['gateway_state'] == 'running' and state['restart_requested'] is False, state
    p = psutil.Process(state['pid'])
    assert p.status() != psutil.STATUS_ZOMBIE
    assert abs(p.create_time() * 100 - float(state['start_time'])) < 1
    argv = p.cmdline()
    assert str(live / 'bin/python') == argv[0], argv
    assert p.environ()['CLOVER_HOME'] == str(home)
    assert p.environ()['VIRTUAL_ENV'] == str(live)
    roots = []
    for proc in psutil.process_iter(['cmdline']):
        args = proc.info['cmdline'] or []
        if 'clover_cli.main' in args and 'gateway' in args and 'run' in args and '--' not in args:
            try:
                if proc.environ().get('CLOVER_HOME') == str(home):
                    roots.append(proc.pid)
            except psutil.NoSuchProcess:
                pass
    assert roots == [p.pid], roots
    assert any(c.status == psutil.CONN_LISTEN and c.laddr.port == 18791 for c in p.net_connections(kind='inet'))
    assert request('/health')['status'] == 'ok'
    domain, manager_pid = _locate_launchd_gateway_service(get_launchd_label())
    ancestry = [q.pid for q in p.parents()]
    assert domain and manager_pid and manager_pid in [p.pid, *ancestry], (domain, manager_pid, ancestry)
    return dict(state=state, argv=argv, executable=p.exe(), create_time=p.create_time(),
        manager_pid=manager_pid, domain=domain, label=get_launchd_label(), parents=ancestry,
        interpreter=identity(), observed=time.time())


def child(a):
    native_guard()
    home, source, live = Path(os.environ['CLOVER_HOME']), Path(a.source), Path(sys.prefix)
    if a.mode == 'observe':
        result = observe(home, source, a.expected, live)
    elif a.mode == 'state':
        result = write_state(a.label)
    elif a.mode == 'verify':
        result = verify_state(read_json(a.records))
    elif a.mode == 'sqlite':
        result = identity()
        assert not result['vulnerable'], result
    elif a.mode == 'service':
        from clover_cli.gateway import get_launchd_plist_path, get_launchd_label
        path = get_launchd_plist_path()
        result = dict(path=str(path), label=get_launchd_label())
    else:
        raise ValueError(a.mode)
    dump(a.output, result)


def main(a):
    native_guard()
    for value in [a.target, a.tree, a.old]:
        assert re.fullmatch('[0-9a-f]{40}', value)
    evidence = Path(a.evidence).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    scratch = Path.home() / ('native-mac-upgrade-' + os.environ['GITHUB_RUN_ID'])
    scratch.mkdir()
    source, live, home = scratch / 'source', scratch / 'external environment', Path.home() / '.clover'
    assert not home.exists(), 'refuse existing home'
    assert not (Path.home() / 'Library/LaunchAgents/ai.clover.gateway.plist').exists(), 'refuse existing service'
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k in {
        'PATH', 'HOME', 'TMPDIR', 'RUNNER_TEMP', 'GITHUB_ACTIONS', 'RUNNER_ENVIRONMENT',
        'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_SHA', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}}
    env.update(CLOVER_HOME=str(home), UV_PYTHON_INSTALL_DIR=str(scratch / 'python'),
        UV_CACHE_DIR=str(scratch / 'cache'), PYTHONUNBUFFERED='1')
    uv = shutil.which('uv')
    assert uv
    commands = []
    script = str(Path(__file__).resolve())
    def run(argv, name, *, cwd=scratch, allow=False, timeout=1800):
        started = time.time()
        with (evidence / (name + '.log')).open('w') as log:
            try:
                rc = subprocess.run([str(x) for x in argv], cwd=cwd, env=env,
                    stdout=log, stderr=subprocess.STDOUT, timeout=timeout).returncode
            except subprocess.TimeoutExpired:
                rc = 124
        commands.append(dict(argv=list(map(str, argv)), started=started, ended=time.time(), exit=rc, name=name))
        dump(evidence / 'commands.json', commands)
        if rc and not allow:
            raise RuntimeError(name + ' failed with ' + str(rc) + '; see log')
        return rc
    def call(mode, name, *extra, allow=False):
        rc = run([live / 'bin/python', '-I', script, '--mode', mode, '--source', source,
            '--output', evidence / (name + '.json'), *extra], name, allow=allow, timeout=30)
        return read_json(evidence / (name + '.json')) if rc == 0 else None
    def git(*args):
        return subprocess.check_output(['git', '-C', str(source), *args], text=True).strip()
    run(['git', 'clone', '--no-hardlinks', '--no-checkout', a.source, source], 'clone')
    assert git('rev-parse', a.target + '^{tree}') == a.tree
    run(['git', '-C', source, 'checkout', '--detach', a.old], 'old-checkout')
    dump(evidence / 'pins.json', dict(workflow_sha=os.environ['GITHUB_SHA'], target=a.target,
        target_tree=a.tree, old=a.old, old_tree=git('rev-parse', 'HEAD^{tree}'), entry=a.entry,
        helper_sha256=sha(script), host=platform.platform()))
    # The loopback bare origin supplies exact candidate objects as main. It
    # does not publish or modify GitHub/main, nor replace any commit identity.
    run([sys.executable, HELPERS / 'proof.py', 'origin', '--repo', source,
        '--mirror', scratch / 'origin.git', '--target', a.target], 'fixture-origin')
    (home / '.skip_upstream_prompt').touch()
    run([uv, 'python', 'install', '3.12', '--no-config'], 'safe-python')
    run([uv, 'venv', live, '--python', '3.12', '--managed-python', '--no-config'], 'external-venv')
    env.update(UV_PROJECT_ENVIRONMENT=str(live), VIRTUAL_ENV=str(live))
    run([uv, 'sync', '--locked', '--extra', 'all', '--python', live / 'bin/python'], 'old-install', cwd=source)
    env.pop('UV_PROJECT_ENVIRONMENT')
    # VIRTUAL_ENV/PATH are also persisted by the unmodified product installer.
    env['PATH'] = str(live / 'bin') + os.pathsep + env['PATH']
    call('sqlite', 'sqlite-before')
    run([live / 'bin/python', HELPERS / 'proof.py', 'identity', '--repo', source,
        '--sha', a.old, '--tree', git('rev-parse', 'HEAD^{tree}'),
        '--output', evidence / 'installed-before.json'], 'old-source-identity')
    bin = live / 'bin/clover'
    for key, value in [('model.provider', 'custom'), ('model.default', 'native-fixture'),
        ('model.base_url', 'http://127.0.0.1:18792/v1'), ('model.context_length', '131072'),
        ('terminal.backend', 'local'), ('terminal.cwd', str(scratch)),
        ('platform_toolsets.api_server', '[terminal]'), ('streaming.enabled', 'false')]:
        run([bin, 'config', 'set', key, value], 'config-' + key)
    (home / '.env').write_text('API_SERVER_ENABLED=true\nAPI_SERVER_KEY=native-macos-fixture-only\nAPI_SERVER_PORT=18791\nAPI_SERVER_HOST=127.0.0.1\nOPENAI_API_KEY=fixture-not-a-secret\nIRC_SERVER=127.0.0.1\nIRC_PORT=16667\nIRC_USE_TLS=false\nIRC_CHANNEL=#clover\nIRC_NICKNAME=cloverbot\nIRC_ALLOWED_USERS=ant\n')
    effect, receiver = evidence / 'effects.txt', evidence / 'received.txt'
    token, reply = 'native-effect-' + os.environ['GITHUB_RUN_ID'], 'native-reply-' + os.environ['GITHUB_RUN_ID']
    provider = LocalProvider(evidence, effect, token, reply)
    irc = LocalIRC(evidence)
    records = [call('state', 'seed-state', '--label', 'old-before-start')]
    service = None
    try:
        run([bin, 'gateway', 'install'], 'service-install', timeout=180)
        service = call('service', 'service-definition')
        shutil.copy2(service['path'], evidence / 'installed-before.plist')
        def initial():
            return call('observe', 'gateway-before', '--expected', a.old, allow=True)
        before = wait_for(initial, 180)
        assert irc.joined.wait(60), 'installed gateway did not join local IRC'
        request('/api/sessions', {'id': 'native-inflight', 'title': 'native controlled turn'})
        received, errors = [], []
        def turn():
            try:
                response = request('/api/sessions/native-inflight/chat', {'message': 'Run the one fixture effect, then finish.'}, timeout=900)
                dump(evidence / 'received-response.json', response)
                text = response['message']['content']
                with receiver.open('a') as out:
                    out.write(text + '\n')
                received.append(text)
            except Exception:
                errors.append(traceback.format_exc())
                dump(evidence / 'receiver-error.json', errors)
        thread = threading.Thread(target=turn, daemon=True)
        thread.start()
        assert provider.entered.wait(180), 'real provider/tool barrier not reached'
        assert thread.is_alive() and not received and effect.read_text().splitlines() == [token]
        # Provider's blocked request proves active work independently of DB tail.
        def capture():
            return witness.capture(home, 'native-inflight', 'native-call-once', effect, token, reply)
        pending = wait_for(capture, 30)
        dump(evidence / 'inflight-before.json', pending)
        records.append(call('state', 'latest-state', '--label', 'latest-at-barrier'))
        dump(evidence / 'records.json', records)
        update_paths = [evidence / 'terminal-update.log', home / 'logs/gateway.log',
                        home / '.update_output.txt', evidence / 'irc.log']
        update_offsets = {str(path): path.stat().st_size if path.exists() else 0
                          for path in update_paths}
        began = time.time()
        update = None
        update_log = (evidence / 'terminal-update.log').open('w')
        if a.entry == 'terminal':
            update = subprocess.Popen([str(bin), 'update', '--yes'], cwd=scratch, env=env,
                stdout=update_log, stderr=subprocess.STDOUT)
        else:
            irc.update()
        # Hold a real outstanding model request through actual update activity.
        # Do not release merely because a timer elapsed: require the original
        # gateway's real draining state or new product drain/refusal output.
        def update_activity():
            state = read_json(home / 'gateway_state.json')
            if state.get('pid') == before['state']['pid'] and state.get('gateway_state') == 'draining':
                # The gateway publishes this from its real drain loop even
                # when the --gateway updater does not print a drain banner.
                dump(evidence / 'actual-drain-or-refusal.json', dict(time=time.time(),
                    lines=[], observed_state=state, basis='live original gateway draining'))
                return True
            chunks = []
            for path in update_paths:
                if path.exists():
                    with path.open('rb') as handle:
                        handle.seek(update_offsets[str(path)])
                        chunks.append(handle.read().decode('utf-8', errors='replace'))
            text = '\n'.join(chunks)
            matches = [line for line in text.splitlines() if any(s in line.lower() for s in [
                'draining in-flight', 'in-flight turn(s)', '/update refused by pre-check',
                'deferring stop() until they finish', 'restart deferred: waiting on',
                'update blocked', 'refusing to update'])]
            if matches:
                dump(evidence / 'actual-drain-or-refusal.json', dict(time=time.time(), lines=matches))
                return True
            return False
        wait_for(update_activity, 600)
        assert thread.is_alive() and not received and not errors, 'turn did not survive until actual updater drain'
        if (home / '.update_output.txt').exists():
            shutil.copy2(home / '.update_output.txt', evidence / 'update-output-at-drain.log')
        dump(evidence / 'barrier-update-observation.json', dict(observed=time.time(),
            source_sha=git('rev-parse', 'HEAD'), provider_blocked=not provider.release.is_set(),
            receiver_running=thread.is_alive(), update_pid=update.pid if update else None,
            state=read_json(home / 'gateway_state.json')))
        provider.release.set()
        thread.join(120)
        assert not thread.is_alive() and not errors and received == [reply], errors
        dump(evidence / 'inflight-after.json', legacy.verify_relaxed(pending, home, effect, receiver, evidence, evidence / 'irc.log'))
        if update:
            rc = update.wait(timeout=1800)
            dump(evidence / 'terminal-update-exit.json', dict(exit=rc, pid=update.pid))
            assert rc == 0, 'real terminal update failed'
        else:
            # Do not send a second request while the first detached updater is
            # merely still fetching. Require completion or a measured refusal.
            def first_outcome():
                text = (home / 'logs/gateway.log').read_text(errors='replace')
                new = text.encode()[update_offsets[str(home / 'logs/gateway.log')]:].decode(errors='replace')
                return (git('rev-parse', 'HEAD') != a.old
                    or (home / '.update_exit_code').exists()
                    or '/update refused by pre-check' in new)
            wait_for(first_outcome, 900)
        update_log.close()
        # A truthful busy refusal may require a second user update after drain.
        # Preserve the first outcome; never label this seamless.
        retry = False
        if git('rev-parse', 'HEAD') == a.old:
            retry = True
            if a.entry == 'terminal':
                run([bin, 'update', '--yes'], 'terminal-retry')
            else:
                time.sleep(5)
                irc.update()
        def candidate():
            if git('rev-parse', 'HEAD') != a.target:
                return None
            observed = call('observe', 'gateway-after', '--expected', a.target, allow=True)
            if observed and observed['state']['pid'] != before['state']['pid']:
                return observed
            return None
        after = wait_for(candidate, 900)
        samples = [after]
        end = time.monotonic() + 90
        while time.monotonic() < end:
            current = call('observe', 'stable', '--expected', a.target)
            assert current['state']['pid'] == after['state']['pid']
            assert current['state']['start_time'] == after['state']['start_time']
            samples.append(current)
            time.sleep(3)
        final = call('observe', 'stable-final', '--expected', a.target)
        assert final['state']['pid'] == after['state']['pid']
        assert final['state']['start_time'] == after['state']['start_time']
        assert final['observed'] - after['observed'] >= 90
        samples.append(final)
        dump(evidence / 'gateway-window.json', samples)
        assert git('rev-parse', 'HEAD^{tree}') == a.tree and not git('diff', '--name-only', 'HEAD')
        run([live / 'bin/python', HELPERS / 'proof.py', 'identity', '--repo', source,
            '--sha', a.target, '--tree', a.tree, '--output', evidence / 'installed-after.json'], 'source-identity')
        call('sqlite', 'sqlite-after')
        call('verify', 'state-after', '--records', evidence / 'records.json')
        dump(evidence / 'inflight-after-successor.json', legacy.verify_relaxed(pending, home, effect, receiver, evidence, evidence / 'irc.log'))
        dump(evidence / 'state-db-check.json', legacy.state_db_check(home))
        dump(evidence / 'state-db-readable-by-1.5.6.json', legacy.readable_by_156(source, home, live / 'bin/python', 'native-inflight', scratch))
        receipt = home / 'logs/update_receipts/latest.json'
        shutil.copy2(receipt, evidence / 'update-receipt.json')
        import proof
        proof.check_receipt(read_json(receipt), began, a.old, a.target)
        assert read_json(receipt)['delivery_restart_guard']['status'] == 'legacy-proceed', read_json(receipt).get('delivery_restart_guard')
        assert not any((home / n).exists() for n in ('fleet_restart_pending', '.update-incomplete')), 'stale restart markers remain'
        shutil.copy2(service['path'], evidence / 'installed-after.plist')
        dump(evidence / 'result.json', dict(status='PASS', target=a.target, old=a.old,
            workflow_sha=os.environ['GITHUB_SHA'], entry=a.entry, retry_after_busy_refusal=retry,
            actual_launchd=True, updater_owned_successor=True, stability_seconds=90,
            real_terminal_tool=True, local_deterministic_provider=True, real_external_llm=False,
            real_telegram=False, inflight_witness=True, latest_write_state=True,
            rollback='separate-runtime-probe-required'))
    finally:
        provider.release.set()
        # Cleanup is after acceptance or failure; it can never repair a result.
        for path in (home / 'logs').glob('*.log'):
            shutil.copy2(path, evidence / ('product-' + path.name))
        if service:
            for domain in [f'gui/{os.getuid()}', f'user/{os.getuid()}']:
                run(['launchctl', 'print', domain + '/' + service['label']], 'cleanup-before-' + domain.split('/')[0], allow=True, timeout=15)
                run(['launchctl', 'bootout', domain + '/' + service['label']], 'cleanup-' + domain.split('/')[0], allow=True, timeout=90)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', default='main')
    for name in ['source', 'target', 'tree', 'old', 'entry', 'evidence', 'output', 'expected', 'label', 'records']:
        p.add_argument('--' + name)
    args = p.parse_args()
    try:
        main(args) if args.mode == 'main' else child(args)
    except Exception:
        if args.evidence:
            Path(args.evidence).mkdir(parents=True, exist_ok=True)
            dump(Path(args.evidence) / 'failure.json', dict(status='FAIL_OR_NOT_EXERCISED', error=traceback.format_exc()))
        raise
