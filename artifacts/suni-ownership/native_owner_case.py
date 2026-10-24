"""Disposable native framework-Python ownership case; NEVER run on Suni.

Requires hosted macOS runner. Linux --portable is a labelled fixture check only.
No credentials, install/pip, product home initialization, service replacement, or
product edits. Creates one private stdlib venv, synthetic gateway child, and (on
macOS only) one unique launchd label; all targets live under --evidence.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from types import ModuleType
import unittest.mock
import uuid
import venv
import plistlib


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-ref', required=True, help='immutable 40-char commit, or WORKTREE for explicitly labelled diagnostics')
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--portable', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    evidence = args.evidence.resolve()
    if not evidence.is_relative_to(root):
        parser.error('evidence must remain inside this isolated checkout')
    native = sys.platform == 'darwin'
    if native:
        if args.portable or os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
            parser.error('macOS execution requires a disposable GitHub-hosted runner; never a live host')
    elif not (args.portable and sys.platform.startswith('linux')):
        parser.error('non-macOS execution requires Linux --portable and is not native proof')
    if args.source_ref != 'WORKTREE':
        if len(args.source_ref) != 40 or any(c not in '0123456789abcdef' for c in args.source_ref):
            parser.error('source-ref must be an exact immutable lowercase commit')
        os.environ['SUNI_SOURCE_REF'] = args.source_ref
    else:
        os.environ.pop('SUNI_SOURCE_REF', None)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location('suni_replay', root / 'tests/clover_cli/test_suni_macos_wrapper_acceptance.py')
    assert spec is not None and spec.loader is not None
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    import psutil  # test-tool dependency only; never import the product package

    evidence.mkdir(parents=True, exist_ok=True)
    report = {'scope': 'native-macos-launchd-process-seam' if native else 'LINUX-PORTABLE-FIXTURE-NOT-MACOS-PROOF', 'source_ref': args.source_ref, 'python': sys.executable, 'native_update_acceptance': 'NOT EXERCISED'}
    report['source_tree'] = subprocess.check_output(['git', 'rev-parse', f'{args.source_ref if args.source_ref != "WORKTREE" else "HEAD"}^{{tree}}'], cwd=root, text=True).strip()
    with tempfile.TemporaryDirectory(prefix='private-fixture-', dir=evidence) as temp:
        tmp = Path(temp)
        home = tmp / 'home'
        home.mkdir()
        fixture = tmp / 'fixture'
        package = fixture / 'clover_cli'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        (package / 'stderr_timestamp.py').write_text(replay.source('clover_cli/stderr_timestamp.py'))
        status_source = replay.source('gateway/status.py')
        nodes = [n for n in ast.parse(status_source).body if isinstance(n, ast.FunctionDef) and n.name in {'_gateway_command_subcommand', 'looks_like_gateway_command_line'}]
        (fixture / 'gateway').mkdir()
        (fixture / 'gateway/__init__.py').write_text('')
        (fixture / 'gateway/status.py').write_text('import shlex\n' + '\n\n'.join(ast.get_source_segment(status_source, n) or '' for n in nodes))
        (package / 'main.py').write_text('''import json,os,pathlib,socket,time
h=pathlib.Path(os.environ['CLOVER_HOME'])
s=socket.socket();s.bind(('127.0.0.1',0));s.listen()
(h/'ready.json').write_text(json.dumps({'pid':os.getpid(),'port':s.getsockname()[1]}))
while not (h/'finish').exists():time.sleep(0.05)
s.close()
''')
        runtime = Path(sys.prefix) if native else tmp / 'venv'
        if native:
            if not (runtime / 'pyvenv.cfg').is_file():
                raise RuntimeError('native evaluator and launchd must use the same private framework venv')
        else:
            venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime)
        py = runtime / 'bin/python'
        command = [str(py), '-m', 'clover_cli.stderr_timestamp', '--error-log', str(home / 'logs/gateway.error.log'), '--', str(py), '-m', 'clover_cli.main', 'gateway', 'run', '--external-supervisor']
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(home), 'CLOVER_HOME': str(home), 'PYTHONPATH': str(fixture), 'VIRTUAL_ENV': str(runtime), 'PYTHONDONTWRITEBYTECODE': '1'}
        child = None
        wrapper = None
        loaded = False
        label = 'ai.clover.acceptance.suni.' + uuid.uuid4().hex
        target = f'gui/{os.getuid()}/{label}'
        try:
            if native:
                # New label only; no reading/replacing any existing service.
                absent = subprocess.run(['launchctl', 'print', target], capture_output=True)
                if absent.returncode == 0:
                    raise RuntimeError('random fixture label already exists; refusing adoption')
                plist = tmp / f'{label}.plist'
                plist.write_bytes(plistlib.dumps({'Label': label, 'ProgramArguments': command, 'EnvironmentVariables': env, 'WorkingDirectory': str(fixture), 'RunAtLoad': True, 'KeepAlive': False, 'StandardOutPath': str(tmp / 'stdout.log'), 'StandardErrorPath': str(tmp / 'stderr.log')}))
                subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(plist)], check=True, capture_output=True)
                loaded = True
            else:
                child = subprocess.Popen(command, cwd=fixture, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 15
            while not (home / 'ready.json').exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError('private synthetic worker did not become ready')
                time.sleep(0.05)
            worker = json.loads((home / 'ready.json').read_text())
            wp = psutil.Process(worker['pid'])
            wrapper = psutil.Process(wp.ppid())
            if child is not None and wrapper.pid != child.pid:
                raise AssertionError('private wrapper no longer owns synthetic worker')
            report['observed'] = {'worker_pid': wp.pid, 'wrapper_pid': wrapper.pid, 'worker_ppid': wp.ppid(), 'worker_argv': wp.cmdline(), 'wrapper_argv': wrapper.cmdline(), 'worker_exe': wp.exe(), 'wrapper_exe': wrapper.exe(), 'venv_python': str(py), 'venv_python_resolved': str(py.resolve()), 'port': worker['port'], 'launchd_label': label if native else None}
            if native:
                query = subprocess.run(['launchctl', 'print', target], capture_output=True, text=True, check=True)
                report['launchd_selected'] = [l.strip() for l in query.stdout.splitlines() if l.strip().startswith(('pid =', 'program =', 'state ='))]
                if f'pid = {wrapper.pid}' not in report['launchd_selected']:
                    raise AssertionError('launchd does not own observed wrapper')
            status = ModuleType('gateway.status')
            status.__dict__.update(Path=Path, shlex=shlex)
            replay.functions('gateway/status.py', {'_get_process_start_time', '_gateway_command_subcommand', 'looks_like_gateway_command_line'}, status.__dict__)
            stderr = ModuleType('clover_cli.stderr_timestamp')
            stderr.__dict__.update(os=os, EXTERNAL_SUPERVISOR_FLAG='--external-supervisor')
            replay.functions('clover_cli/stderr_timestamp.py', {'_is_clover_gateway_run_argv', '_is_launchd_supervised', '_with_external_supervisor_flag', '_prepare_child_command'}, stderr.__dict__)
            guard = {'Path': Path, 'json': json}
            names = {'_proved_stderr_wrapper'}
            if 'def _macos_framework_wrapper(' in replay.source('clover_cli/update_delivery_guard.py'):
                names.add('_macos_framework_wrapper')
            replay.functions('clover_cli/update_delivery_guard.py', names, guard)
            import hashlib
            report['guard_sha256'] = hashlib.sha256(replay.source('clover_cli/update_delivery_guard.py').encode()).hexdigest()
            report['false_return_lines'] = []
            def trace(frame, event, value):
                if event == 'return' and value is False and frame.f_code.co_filename == 'clover_cli/update_delivery_guard.py':
                    report['false_return_lines'].append({'function': frame.f_code.co_name, 'line': frame.f_lineno})
                return trace
            with unittest.mock.patch.dict(sys.modules, {'gateway': ModuleType('gateway'), 'gateway.status': status, 'clover_cli': ModuleType('clover_cli'), 'clover_cli.stderr_timestamp': stderr}):
                start = status._get_process_start_time(wp.pid)
                wrapper_start = status._get_process_start_time(wrapper.pid)
                admitted = {wp.pid: (home, start)}
                report['worker_start'] = start
                report['wrapper_start'] = wrapper_start
                sys.settrace(trace)
                try:
                    report['owner_admission'] = guard['_proved_stderr_wrapper'](wrapper.pid, admitted)
                finally:
                    sys.settrace(None)
                report['stale_worker_rejected'] = not guard['_proved_stderr_wrapper'](wrapper.pid, {wp.pid: (home, start + 1)})
                report['wrong_home_rejected'] = not guard['_proved_stderr_wrapper'](wrapper.pid, {wp.pid: (home / 'foreign', start)})
                report['unadmitted_rejected'] = not guard['_proved_stderr_wrapper'](wrapper.pid, {})
                report['identity_stable'] = start == status._get_process_start_time(wp.pid) and wrapper_start == status._get_process_start_time(wrapper.pid) and wp.ppid() == wrapper.pid
            report['framework_alias_exercised'] = wrapper.cmdline()[0] != str(py) and wp.cmdline()[0] != str(py)
            report['passed'] = all(report[k] for k in ('owner_admission', 'stale_worker_rejected', 'wrong_home_rejected', 'unadmitted_rejected', 'identity_stable')) and (not native or report['framework_alias_exercised'])
        except Exception as exc:
            report['passed'] = False
            report['error'] = f'{type(exc).__name__}: {exc}'
        finally:
            # Natural completion first, no signal to any real gateway.
            (home / 'finish').touch()
            if child is not None:
                child.wait(timeout=15)
            if loaded and wrapper is not None:
                # Wait for the synthetic worker AND wrapper to finish naturally.
                # Never use bootout as a way to force a passing cleanup result.
                alive = True
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        alive = wrapper.is_running() and wrapper.status() != psutil.STATUS_ZOMBIE
                    except psutil.NoSuchProcess:
                        alive = False
                    if not alive:
                        break
                    time.sleep(0.05)
                if alive:
                    report['passed'] = False
                    report['cleanup_error'] = 'synthetic wrapper did not finish naturally; no forced kill attempted'
                else:
                    subprocess.run(['launchctl', 'bootout', target], check=True, capture_output=True)
                    report['fixture_label_absent_after_cleanup'] = subprocess.run(['launchctl', 'print', target], capture_output=True).returncode != 0
                    report['passed'] = report['passed'] and report['fixture_label_absent_after_cleanup']
    (evidence / 'native-owner-case.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'scope': report['scope'], 'evidence': str(evidence / 'native-owner-case.json')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
