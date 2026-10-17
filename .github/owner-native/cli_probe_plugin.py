"""Fixture-only observer. Never logs proof material, env values or output."""
import json
from pathlib import Path
import subprocess
import threading
import time

import pytest


def pytest_addoption(parser):
    parser.addoption('--cli-observation-out', required=True)


@pytest.fixture(autouse=True)
def observe_cli_run(request, monkeypatch):
    if request.node.name != 'test_real_cli_process_loads_exact_profile_and_confirms_saved_owner':
        yield
        return
    out = Path(request.config.getoption('--cli-observation-out'))
    out.mkdir(parents=True, exist_ok=True)
    real_run, real_popen = subprocess.run, subprocess.Popen
    children = []

    def emit(event, **fields):
        with (out / 'parent.jsonl').open('a', encoding='utf-8') as file:
            file.write(json.dumps({'event': event, 'monotonic': time.monotonic(), **fields}) + '\n')

    def observed_popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        emit('popen', pid=child.pid, creationflags=kwargs.get('creationflags', 0), stdin_pipe=kwargs.get('stdin') == subprocess.PIPE, stdout_pipe=kwargs.get('stdout') == subprocess.PIPE, stderr_pipe=kwargs.get('stderr') == subprocess.PIPE)
        return child

    def observed_run(command, *args, **kwargs):
        if not isinstance(command, (list, tuple)) or list(command[1:]) != ['-m', 'clover_cli.main', 'pairing', 'owner-confirm', 'telegram']:
            return real_run(command, *args, **kwargs)
        env = kwargs['env']
        emit('run', command=command, timeout=kwargs.get('timeout'), input_supplied=isinstance(kwargs.get('input'), str), text=kwargs.get('text'), capture_output=kwargs.get('capture_output'), env_keys=sorted(env), pythonpath=env.get('PYTHONPATH'), home_equals_userprofile=env.get('HOME') == env.get('USERPROFILE'), exact_clover_home=env.get('CLOVER_HOME') == str(request.getfixturevalue('profile')))
        started = time.monotonic()
        monkeypatch.setattr(subprocess, 'Popen', observed_popen)
        try:
            launch_command = command
            if 'wrapped-command' in out.name:
                launch_command = [command[0], '-c', "import io, runpy, sys; sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding=sys.stdin.encoding, errors=sys.stdin.errors); sys.argv = ['clover_cli.main', 'pairing', 'owner-confirm', 'telegram']; runpy.run_module('clover_cli.main', run_name='__main__')"]
                emit('command_experiment', change='Python stdin wrapper plus runpy module entry; proof remains on stdin')
            result = real_run(launch_command, *args, **kwargs)
            proof = kwargs['input'].strip()
            emit('completed', returncode=result.returncode, seconds=time.monotonic()-started, saved_message='verified and saved' in result.stdout.lower(), proof_not_echoed=proof not in result.stdout + result.stderr, getpass_warning='GetPassWarning' in result.stderr)
            return result
        except subprocess.TimeoutExpired:
            emit('timeout', seconds=time.monotonic()-started)
            raise
        finally:
            monkeypatch.setattr(subprocess, 'Popen', real_popen)
            emit('run_finally', child_status=[{'pid': p.pid, 'poll': p.poll()} for p in children], threads=[{'name': t.name, 'daemon': t.daemon, 'alive': t.is_alive()} for t in threading.enumerate()])
    monkeypatch.setattr(subprocess, 'run', observed_run)
    yield
