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

HELPERS = Path(os.environ["CLOVER_PROOF_HELPERS"]).resolve()
sys.path.insert(0, str(HELPERS))
import inflight_witness as witness
import legacy156_checks as legacy
from macos_external_env import dump, sha, write_state, verify_state, identity

WINDOWS = sys.platform == "win32"
def native_guard():
    assert platform.system() == "Windows"
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"

def python(live):
    return live / ("Scripts/python.exe" if WINDOWS else "bin/python")

def binary(live):
    return live / ("Scripts/clover.exe" if WINDOWS else "bin/clover")


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


def wait_update_exit(home, update, evidence, *, seconds=1800):
    """Observe completion only; never finish or restart the updater for it."""
    if update is not None:
        rc = update.wait(timeout=seconds)
        dump(evidence / 'terminal-update-exit.json', dict(exit=rc, pid=update.pid))
    else:
        marker = home / '.update_exit_code'
        wait_for(marker.exists, seconds)
        rc = int(marker.read_text().strip())
        dump(evidence / 'gateway-update-exit.json', dict(exit=rc))
    return rc


def wait_update_receipt(home, evidence, began, old, target, *, seconds=1800):
    """Wait for the logical run, not a shim parent's provisional exit zero."""
    path = home / 'logs/update_receipts/latest.json'
    def completed():
        data = read_json(path)
        return data if data.get('finished_at') and data.get('outcome') != 'running' else None
    data = wait_for(completed, seconds)
    # Preserve the actual terminal failure/partial receipt before rejecting it.
    shutil.copy2(path, evidence / 'update-receipt.json')
    import proof
    proof.check_receipt(data, began, old, target)
    return data


def legacy_stop_witness(trace, original_pid, old):
    died = next((x for x in trace if x.get('state', {}).get('pid') == original_pid
                 and x.get('pid_alive') is False), None)
    assert died is not None, 'no measured original process death'
    assert died['disk_sha'] == old, 'original death not witnessed before source pull'
    assert died['state']['code_sha'] == old, 'original runtime was not the installed base'
    return died


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
                if any(m.get('tool_call_id') == 'native-call-once' for m in tools):
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
                        self.send_json({'id':'fixture-aux','object':'chat.completion',
                            'model':'native-fixture','choices':[{'index':0,
                            'message':{'role':'assistant','content':'OK'},'finish_reason':'stop'}]})
                        return
                    command = '/usr/bin/printf ' + shlex.quote('%s\n') + ' ' + shlex.quote(owner.token) + ' >> ' + shlex.quote(owner.effect.as_posix())
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


def observe(home, source, expected, live, diagnostic=None):
    import psutil
    state = read_json(home / 'gateway_state.json')
    assert state['code_sha'] == expected, state
    assert state['gateway_state'] == 'running' and state['restart_requested'] is False, state
    p = psutil.Process(state['pid'])
    assert p.status() != psutil.STATUS_ZOMBIE
    assert abs(p.create_time() * 100 - float(state['start_time'])) < 1
    # Task Scheduler supplies a fresh environment. Its pythonw action does not
    # inherit our driver's VIRTUAL_ENV/CLOVER_HOME; the default native home is
    # still independently bound by the status row and listening worker PID.
    assert Path(state['clover_home']).resolve() == home.resolve()
    if 'CLOVER_HOME' in p.environ():
        assert Path(p.environ()['CLOVER_HOME']).resolve() == home.resolve()
    if 'VIRTUAL_ENV' in p.environ():
        assert Path(p.environ()['VIRTUAL_ENV']).resolve() == live.resolve()
    measured = []
    for q in psutil.process_iter(['cmdline', 'exe', 'pid', 'ppid']):
        argv = q.info['cmdline'] or []
        if 'clover_cli.main' in argv and 'gateway' in argv and 'run' in argv and '--' not in argv:
            measured.append(dict(pid=q.pid, ppid=q.info['ppid'], exe=q.info['exe'], argv=argv))
    base_image = str(Path(sys._base_executable).resolve(strict=True))
    if diagnostic is not None:
        dump(diagnostic, dict(state=state, processes=measured,
             installed_python=str(python(live).resolve(strict=True)), measured_base=base_image))
    # Match the native roles from the already exercised Read-ExactGateway PS
    # probe: exact installed image -> direct child -> exact measured base image.
    # In particular, do not require a Scheduled Task's fresh environment or the
    # worker's argv[0] to name the driver's venv. Those are not owner witnesses.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import topology
    members = topology.pair(measured, str(python(live).resolve(strict=True)), base_image)
    assert len(measured) == len(members), dict(reason='unexpected extra gateway processes', measured=measured, members=members)
    owned = [psutil.Process(row['pid']) for row in members]
    assert p.pid == members[-1]['pid'], dict(state=state, actual_members=members)
    schedule = psutil.win_service_get('Schedule').as_dict()
    assert schedule['status'] == 'running'
    supervisor = dict(schedule_pid=schedule['pid'], schedule_status=schedule['status'],
                      ancestors=[dict(pid=q.pid, name=q.name()) for q in p.parents()])
    assert any(c.status==psutil.CONN_LISTEN and c.laddr.port==18791 for c in p.net_connections(kind='inet'))
    assert request('/health')['status']=='ok'
    return dict(state=state, argv=p.cmdline(), executable=p.exe(), create_time=p.create_time(),
                supervisor=supervisor, owned=[dict(pid=q.pid,ppid=q.ppid(),argv=q.cmdline()) for q in owned], observed=time.time())


def child(a):
    native_guard()
    home, source, live = Path(os.environ['CLOVER_HOME']), Path(a.source), Path(sys.prefix)
    if a.mode == 'observe':
        result = observe(home, source, a.expected, live, Path(a.output).with_suffix('.raw.json'))
    elif a.mode == 'state':
        result = write_state(a.label)
    elif a.mode == 'verify':
        result = verify_state(read_json(a.records))
    elif a.mode == 'sqlite':
        result = identity()
        if WINDOWS:
            base_executable = getattr(sys, '_base_executable', None)
            assert isinstance(base_executable, str)
            result['base_executable'] = base_executable
            result['base_executable_resolved'] = str(Path(base_executable).resolve(strict=True))
        assert not result['vulnerable'], result
    elif a.mode == 'service-inventory':
        from clover_cli.gateway import find_windows_gateway_services
        found = find_windows_gateway_services()
        result = dict(services=[dict(name=s.name, service_pid=s.service_pid,
                                     gateway_pid=s.gateway_pid) for s in found])
        dump(a.output, result)
        # Neither shipped Startup nor the real Task fixture installs an SCM
        # service. Fail before updating if discovery would touch Schedule or
        # any other foreign service; never attempt a stop to test the fence.
        assert not found, result
    elif a.mode == 'service':
        if WINDOWS:
            from clover_cli import gateway_windows as g
            script=g._write_task_script()
            path=g._install_startup_entry(script)
            result=dict(path=str(path), script=str(script), label=g.get_task_name())
        else:
            from clover_cli.gateway import get_launchd_plist_path, get_launchd_label
            path=get_launchd_plist_path()
            result=dict(path=str(path),label=get_launchd_label())
    else:
        raise ValueError(a.mode)
    dump(a.output, result)


def main(a):
    native_guard()
    for value in [a.target, a.tree, a.old]:
        assert re.fullmatch('[0-9a-f]{40}', value)
    evidence = Path(a.evidence).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    scratch = Path.home() / ('owner-worker-' + os.environ['GITHUB_RUN_ID'])
    scratch.mkdir()
    source, home = scratch / 'source', (Path(os.environ['LOCALAPPDATA']) / 'clover' if WINDOWS else Path.home() / '.clover')
    live = source / 'venv' if WINDOWS else scratch / 'external environment'
    assert not home.exists(), 'refuse existing home'
    assert not (Path.home() / 'Library/LaunchAgents/ai.clover.gateway.plist').exists(), 'refuse existing service'
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k in {
        'PATH', 'HOME', 'TMPDIR', 'RUNNER_TEMP', 'GITHUB_ACTIONS', 'RUNNER_ENVIRONMENT',
        'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_SHA', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'USERDOMAIN', 'USERNAME', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PSModuleAnalysisCachePath'}}
    env.update(CLOVER_HOME=str(home), CLOVER_PROOF_HELPERS=str(HELPERS), UV_PYTHON_INSTALL_DIR=str(scratch / 'python'),
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
        rc = run([python(live), '-I', script, '--mode', mode, '--source', source,
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
    run([uv, 'sync', '--locked', '--extra', 'all', '--python', python(live)], 'old-install', cwd=source)
    env.pop('UV_PROJECT_ENVIRONMENT')
    # VIRTUAL_ENV/PATH are also persisted by the unmodified product installer.
    env['PATH'] = str(python(live).parent) + os.pathsep + env['PATH']
    call('sqlite', 'sqlite-before')
    run([python(live), HELPERS / 'proof.py', 'identity', '--repo', source,
        '--sha', a.old, '--tree', git('rev-parse', 'HEAD^{tree}'),
        '--output', evidence / 'installed-before.json'], 'old-source-identity')
    bin = binary(live)
    for key, value in [('model.provider', 'custom'), ('model.default', 'native-fixture'),
        ('model.base_url', 'http://127.0.0.1:18792/v1'), ('model.context_length', '131072'),
        ('terminal.backend', 'local'), ('terminal.cwd', str(scratch)),
        ('platform_toolsets.api_server', '[terminal]'), ('streaming.enabled', 'false'),
        ('auxiliary.free_only', 'true')]:
        run([bin, 'config', 'set', key, value], 'config-' + key)
    (home / '.env').write_text('API_SERVER_ENABLED=true\nAPI_SERVER_KEY=native-macos-fixture-only\nAPI_SERVER_PORT=18791\nAPI_SERVER_HOST=127.0.0.1\nOPENAI_API_KEY=fixture-not-a-secret\nIRC_SERVER=127.0.0.1\nIRC_PORT=16667\nIRC_USE_TLS=false\nIRC_CHANNEL=#clover\nIRC_NICKNAME=cloverbot\nIRC_ALLOWED_USERS=ant\n')
    effect, receiver = evidence / 'effects.txt', evidence / 'received.txt'
    token, reply = 'native-effect-' + os.environ['GITHUB_RUN_ID'], 'native-reply-' + os.environ['GITHUB_RUN_ID']
    provider = LocalProvider(evidence, effect, token, reply)
    irc = LocalIRC(evidence)
    records = [call('state', 'seed-state', '--label', 'old-before-start')]
    customization = home / 'skills' / 'private-native-fixture' / 'SKILL.md'
    customization.parent.mkdir(parents=True)
    customization.write_text('Private nonsecret native proof customization.\n', encoding='utf-8')
    customization_sha = sha(customization)
    receipt_dir = home / 'logs' / 'update_receipts'
    receipt_dir.mkdir(parents=True, exist_ok=True)
    stale_receipt = dict(schema=1, run_id='stale-native-proof-not-this-run', outcome='refused',
                         started_at='2000-01-01T00:00:01+00:00',
                         finished_at='2000-01-01T00:00:02+00:00',
                         argv=['clover', 'update'], pid=2147483644,
                         pre_update=dict(sha=a.old), post_update=dict(sha=a.old),
                         steps=[], skips=[], gateway_restart={}, fleet=[],
                         stop_reason='prior nonsecret refusal')
    dump(receipt_dir / 'latest.json', stale_receipt)
    dump(evidence / 'seeded-stale-receipt.json', stale_receipt)
    (home / 'fleet_restart_pending').write_text('prior native fixture restart intent\n')
    large_lane = os.environ.get('LARGE_ROLLBACK_LANE') == 'true'
    live_python = python(live)
    if large_lane:
        run([live_python, HELPERS/'legacy156_large_state.py', 'grow', '--home', home,
             '--evidence', evidence], 'large-state-grow', timeout=300)
    health_watch = None
    service = None
    trace_stop=threading.Event()
    trace=[]
    def tracer():
        while not trace_stop.is_set():
            try:
                state=read_json(home/'gateway_state.json')
                import psutil
                alive=psutil.pid_exists(state['pid'])
                trace.append(dict(at=time.time(),monotonic=time.monotonic(),state=state,pid_alive=alive,
                    disk_sha=git('rev-parse','HEAD'),beacon=(home/'.clover-update-heartbeat.json').exists()))
                beacon=home/'.clover-update-heartbeat.json'
                if beacon.exists():
                    trace[-1]['beacon_data']=read_json(beacon)
                    updater=trace[-1]['beacon_data'].get('updater_pid')
                    if updater:
                        trace[-1]['updater_alive']=psutil.pid_exists(updater)
                        if psutil.pid_exists(updater):
                            u=psutil.Process(updater)
                            trace[-1]['updater_process']=dict(pid=u.pid,ppid=u.ppid(),status=u.status(),argv=u.cmdline())
                for src,dst in [(home/'.update_output.txt',evidence/'live-product-update.log'),(home/'logs/gateway.log',evidence/'live-product-gateway.log'),(home/'logs/update_receipts/latest.json',evidence/'live-product-receipt.json')]:
                    if src.exists():
                        try: shutil.copy2(src,dst)
                        except OSError: pass
                dump(evidence/'raw-observer.json',trace)
            except Exception as exc:
                trace.append(dict(at=time.time(),error=repr(exc)))
            trace_stop.wait(0.5)
    try:
        if WINDOWS:
            service=call('service','service-definition')
            if a.setup == 'troy':
                run(['pwsh', '-NoProfile', '-File', Path(script).with_name('start_troy.ps1'),
                     '-Python', python(live).with_name('pythonw.exe'), '-Source', source],
                    'scheduled-task-launch', timeout=90)
                service['task_name'] = 'Clover_OwnerWorker_Proof'
            else:
                run(['wscript.exe','//B','//Nologo',service['path']],'startup-vbs-launch',timeout=30)
        else:
            run([bin, 'gateway', 'install'], 'service-install', timeout=180)
            service = call('service', 'service-definition')
        shutil.copy2(service['path'], evidence / 'installed-before.definition')
        def initial():
            return call('observe', 'gateway-before', '--expected', a.old, allow=True)
        before = wait_for(initial, 180)
        via_schedule = before['supervisor']['schedule_pid'] in [q['pid'] for q in before['supervisor']['ancestors']]
        assert via_schedule == (a.setup == 'troy'), dict(setup=a.setup, observation=before)
        # Preserve owner/worker evidence before an update may replace it.
        dump(evidence / 'before-owner-worker.json', dict(setup=a.setup, via_schedule=via_schedule,
            status_pid=before['state']['pid'], processes=before['owned'],
            status_start=before['state']['start_time'], supervisor=before['supervisor']))
        call('service-inventory', 'native-scm-discovery-before-update')
        threading.Thread(target=tracer,daemon=True).start()
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
        if a.turn_mode == 'completed':
            # Frozen Windows first-stop code runs BEFORE target source exists.
            # Finish the accepted turn before that unsupported first stop; do
            # not claim protection of an executing action on frozen callers.
            provider.release.set()
            thread.join(120)
            assert not thread.is_alive() and not errors and received == [reply]
            dump(evidence / 'completed-before-update.json',
                 witness.verify(pending, home, effect, receiver))
        records.append(call('state', 'latest-state', '--label', 'latest-at-barrier'))
        dump(evidence / 'records.json', records)
        update_paths = [evidence / 'terminal-update.log', home / 'logs/gateway.log',
                        home / '.update_output.txt', evidence / 'irc.log']
        update_offsets = {str(path): path.stat().st_size if path.exists() else 0
                          for path in update_paths}
        marker=home/'fleet_restart_pending'
        pending_before=marker.read_bytes() if marker.exists() else None
        began = time.time()
        if large_lane:
            health_log = (evidence/'large-state-health-watch.log').open('w')
            # The observer must not hold the product venv open: released
            # Windows preflight correctly rejects unrelated venv processes.
            # This driver's isolated uv interpreter already provides psutil.
            health_watch = subprocess.Popen([str(sys.executable), str(HELPERS/'legacy156_large_state.py'),
                'watch', '--home',str(home),'--evidence',str(evidence),'--target',a.target],
                env=env,stdout=health_log,stderr=subprocess.STDOUT)
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
            import psutil
            if not psutil.pid_exists(before['state']['pid']):
                dump(evidence/'stop-before-delivery.json',dict(time=time.time(),state=state,
                    original_pid=before['state']['pid'],disk_sha=git('rev-parse','HEAD'),
                    provider_held=not provider.release.is_set()))
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
                'update blocked', 'refusing to update', 'automatic update deferred',
                'update not completed:'])]
            if matches:
                dump(evidence / 'actual-drain-or-refusal.json', dict(time=time.time(), lines=matches))
                return True
            return False
        if a.turn_mode == 'inflight':
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
        # npm/dependency work is still real updater work. Use the same bounded
        # completion budget for both entry points, preserving nonzero results.
        rc = wait_update_exit(home, update, evidence)
        update_log.close()
        text='\n'.join(p.read_text(errors='replace') for p in update_paths if p.exists())
        refused='Automatic update deferred:' in text
        lost=(evidence/'stop-before-delivery.json').exists()
        dump(evidence/'first-outcome.json',dict(exit=rc,refused=refused,lost_original_while_held=lost,
            received=received,errors=errors,disk_sha=git('rev-parse','HEAD')))
        call('verify','state-after','--records',evidence/'records.json')
        assert sha(customization) == customization_sha, 'local customization changed'
        dump(evidence / 'customization-preserved.json', dict(path=str(customization), sha256=customization_sha))
        receipt=home/'logs/update_receipts/latest.json'
        if receipt.exists(): shutil.copy2(receipt,evidence/'update-receipt.json')
        if lost or errors or thread.is_alive():
            # This branch cannot be recovered into an automatic-update PASS.
            dump(evidence/'result.json',dict(status='UNPROTECTED_STOP_BEFORE_ACK',target=a.target,old=a.old,
                workflow_sha=os.environ['GITHUB_SHA'],entry=a.entry,exit=rc,refused=refused,
                original_pid=before['state']['pid'],driver_restart=False,real_telegram=False))
            raise RuntimeError('actual turn did not survive the updater; see result and receiver evidence')
        assert received == [reply]
        dump(evidence/'inflight-after.json',legacy.verify_relaxed(pending,home,effect,receiver,evidence,evidence/'irc.log'))
        if rc != 0 and git('rev-parse', 'HEAD') == a.old:
            dump(evidence / 'result.json', dict(status='UNSUPPORTED_FROZEN_PREPULL_OR_NATIVE_FAILURE',
                old=a.old, target=a.target, entry=a.entry, setup=a.setup, exit=rc,
                source_pulled=False, driver_restart=False, real_telegram=False,
                note='Target-side code did not run. Inspect preserved output; no retry or bypass.'))
            raise RuntimeError('released caller refused or failed before target pull; native acceptance NOT passed')
        if refused:
            assert rc==2
            if pending_before is not None:
                assert marker.read_bytes()==pending_before
            samples=[]
            end=time.monotonic()+155
            while time.monotonic()<end:
                current=call('observe','stable','--expected',a.old)
                assert current['state']['pid']==before['state']['pid']
                assert current['state']['start_time']==before['state']['start_time']
                samples.append(current)
                time.sleep(3)
            assert not (home/'.clover-update-heartbeat.json').exists()
            if git('rev-parse','HEAD') != a.old:
                assert (home/'fleet_restart_pending').exists(), list(home.glob('*pending*'))
            dump(evidence/'gateway-window.json',samples)
            call('verify','state-after','--records',evidence/'records.json')
            dump(evidence/'inflight-after-successor.json',legacy.verify_relaxed(pending,home,effect,receiver,evidence,evidence/'irc.log'))
            dump(evidence/'result.json',dict(status='NATIVE_UPDATE_REFUSED_NOT_ACCEPTED',target=a.target,old=a.old,
                entry=a.entry,setup=a.setup,exit=rc,workflow_sha=os.environ['GITHUB_SHA'],original_pid=before['state']['pid'],
                stability_seconds=155,driver_restart=False,real_telegram=False))
            raise RuntimeError('state survived, but refusal is not automatic-update acceptance')
        assert rc==0
        logical_receipt = wait_update_receipt(home, evidence, began, a.old, a.target)
        dump(evidence / 'logical-update-completion.json', dict(outcome=logical_receipt['outcome'],
            pre_sha=logical_receipt['pre_update']['sha'], post_sha=logical_receipt['post_update']['sha'],
            started_at=logical_receipt['started_at'], finished_at=logical_receipt['finished_at']))
        assert logical_receipt.get('run_id') != stale_receipt['run_id']
        if a.entry == 'gateway':
            def final_notice():
                path = evidence / 'irc.log'
                with path.open('rb') as handle:
                    handle.seek(update_offsets[str(path)])
                    text = handle.read().decode('utf-8', errors='replace')
                lines = [line for line in text.splitlines()
                         if '<< PRIVMSG' in line and 'Clover update finished' in line]
                return lines or None
            notices = wait_for(final_notice, 300)
            assert len(notices) == 1, notices
            dump(evidence / 'current-run-final-chat.json', dict(received=notices, real_telegram=False))
        first_transition = None
        if WINDOWS and a.old == '9535350bee1d57cf7e466672e6f416403886bec1':
            # Record what the released updater really does on its first transition:
            # it pauses/stops the gateway before the new code exists. This is
            # documented, not hidden; the pass criteria below still demand the
            # gateway comes back on the target with data and effects intact.
            died = legacy_stop_witness(trace, before['state']['pid'], a.old)
            first_transition = dict(original_stopped_before_pull=True, first_original_dead=died)
            assert git('rev-parse', 'HEAD') == a.target, 'target did not land after legacy stop'
            assert git('rev-parse', 'HEAD^{tree}') == a.tree
        retry=False
        def candidate():
            if git('rev-parse', 'HEAD') != a.target:
                return None
            observed = call('observe', 'gateway-after', '--expected', a.target, allow=True)
            if observed and observed['state']['pid'] != before['state']['pid']:
                return observed
            return None
        after = wait_for(candidate, 900)
        assert after['supervisor']['schedule_pid'] == before['supervisor']['schedule_pid'], 'foreign Schedule host identity changed'
        samples = [after]
        end = time.monotonic() + 90
        while time.monotonic() < end:
            current = call('observe', 'stable', '--expected', a.target)
            assert current['state']['pid'] == after['state']['pid']
            assert current['state']['start_time'] == after['state']['start_time']
            assert current['supervisor']['schedule_pid'] == before['supervisor']['schedule_pid']
            samples.append(current)
            time.sleep(3)
        final = call('observe', 'stable-final', '--expected', a.target)
        assert final['state']['pid'] == after['state']['pid']
        assert final['state']['start_time'] == after['state']['start_time']
        assert final['supervisor']['schedule_pid'] == before['supervisor']['schedule_pid']
        assert final['observed'] - after['observed'] >= 90
        samples.append(final)
        dump(evidence / 'gateway-window.json', samples)
        assert git('rev-parse', 'HEAD^{tree}') == a.tree and not git('diff', '--name-only', 'HEAD')
        run([python(live), HELPERS / 'proof.py', 'identity', '--repo', source,
            '--sha', a.target, '--tree', a.tree, '--output', evidence / 'installed-after.json'], 'source-identity')
        call('sqlite', 'sqlite-after')
        call('verify', 'state-after', '--records', evidence / 'records.json')
        dump(evidence / 'inflight-after-successor.json', witness.verify(pending, home, effect, receiver))
        receipt = home / 'logs/update_receipts/latest.json'
        shutil.copy2(receipt, evidence / 'update-receipt.json')
        import proof
        proof.check_receipt(read_json(receipt), began, a.old, a.target)
        shutil.copy2(service['path'], evidence / 'installed-after.definition')
        dump(evidence / 'state-db-check.json', legacy.state_db_check(home))
        dump(evidence / 'state-db-readable-by-1.5.6.json', legacy.readable_by_156(source, home, python(live), 'native-inflight', scratch))
        if large_lane:
            assert health_watch.wait(timeout=120) == 0, 'large state first health failed'
            run([live_python, HELPERS/'legacy156_large_state.py', 'rollback', '--source',source,
                 '--home',home,'--evidence',evidence,'--python',live_python,
                 '--old',a.old,'--target',a.target,'--supervisor','windows'],
                 'frozen-156-failure-rollback',timeout=1200)
            call('verify', 'rollback-seed-records-after', '--records', evidence/'records.json')
            legacy.verify_relaxed(pending,home,effect,receiver,evidence,evidence/'irc.log')
        dump(evidence / 'result.json', dict(status='PASS', first_transition=first_transition, target=a.target, old=a.old,
            workflow_sha=os.environ['GITHUB_SHA'], entry=a.entry, retry_after_busy_refusal=retry,
            actual_launchd=False, actual_windows_startup=a.setup == 'zyra', actual_scheduled_task=a.setup == 'troy',
            turn_mode=a.turn_mode, frozen_windows_first_stop_limit=a.old == '9535350bee1d57cf7e466672e6f416403886bec1',
            driver_restart=False, updater_owned_successor=True, stability_seconds=90,
            real_terminal_tool=True, local_deterministic_provider=True, real_external_llm=False,
            real_telegram=False, inflight_witness=True, latest_write_state=True,
            rollback='frozen-156-fault-stop-passed' if large_lane else 'NOT_EXERCISED',
            large_state_measured=large_lane, final_source_after_fault_injection=a.old if large_lane else a.target))
    finally:
        if health_watch is not None and health_watch.poll() is None:
            health_watch.terminate()
        provider.release.set()
        trace_stop.set()
        for name in ['fleet_restart_pending','.clover-update-heartbeat.json']:
            if (home/name).exists(): shutil.copy2(home/name,evidence/('product-'+name))
        # Cleanup is after acceptance or failure; it can never repair a result.
        for path in (home / 'logs').glob('*.log'):
            shutil.copy2(path, evidence / ('product-' + path.name))
        if service and not WINDOWS:
            for domain in [f'gui/{os.getuid()}', f'user/{os.getuid()}']:
                run(['launchctl', 'print', domain + '/' + service['label']], 'cleanup-before-' + domain.split('/')[0], allow=True, timeout=15)
                run(['launchctl', 'bootout', domain + '/' + service['label']], 'cleanup-' + domain.split('/')[0], allow=True, timeout=90)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', default='main')
    p.add_argument('--fixed-base', default='candidate')
    p.add_argument('--setup', choices=['zyra','troy'], default='zyra')
    p.add_argument('--turn-mode', choices=['completed','inflight'], default='completed')
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