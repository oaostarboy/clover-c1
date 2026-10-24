"""Frozen-caller replay of Suni's native framework-Python argv.

Standalone, stdlib only; no product imports, live processes or home writes.
  SUNI_SOURCE_REF=3b54f413376eb99f8b83bfd4a0a340d01825014d \
    python -B tests/clover_cli/test_suni_macos_wrapper_acceptance.py
Expected published result: two positive-contract FAILs, all negatives PASS.
For candidate: omit SUNI_SOURCE_REF (working tree), or pin its immutable SHA.
This replay is NOT a macOS native acceptance result.
"""
from __future__ import annotations

import ast
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
PUBLISHED = '3b54f413376eb99f8b83bfd4a0a340d01825014d'
FRAMEWORK = '/opt/homebrew/Cellar/python@3.12/3.12.13/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python'
VENV = '/Users/ant/agents/clover-c1/venv/bin/python'
TAIL = ['-m', 'clover_cli.main', 'gateway', 'run', '--external-supervisor']


def source(path: str) -> str:
    ref = os.environ.get('SUNI_SOURCE_REF')
    if ref:
        return subprocess.check_output(['git', 'show', f'{ref}:{path}'], cwd=ROOT, text=True)
    return (ROOT / path).read_text()


def functions(path: str, names: set[str], namespace: dict) -> None:
    tree = ast.parse(source(path), filename=path)
    selected: list[ast.stmt] = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    if {n.name for n in selected} != names:
        raise AssertionError(f'{path}: frozen seam changed; fixture needs explicit review')
    exec(compile(ast.Module(body=selected, type_ignores=[]), path, 'exec'), namespace)


def module(name: str, **attrs) -> ModuleType:
    m = ModuleType(name)
    m.__dict__.update(attrs)
    return m


class SuniWrapperAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'artifacts/suni-ownership')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.start = {40563: 179226685419, 40564: 179226685419}
        self.rows: dict[int, dict[str, Any]] = {
            40563: dict(argv=[FRAMEWORK, '-m', 'clover_cli.stderr_timestamp', '--error-log', str(self.home / 'logs/gateway.error.log'), '--', VENV, *TAIL], parent=1),
            40564: dict(argv=[FRAMEWORK, *TAIL], parent=40563),
        }
        self.admitted = {40564: (self.home, self.start[40564])}
        self.row = dict(pid=40564, kind='clover-gateway', start_time=self.start[40564], clover_home=str(self.home), code_version='1.5.6', updated_at='2026-10-21T20:17:50.248529+00:00', active_agents=0)
        self.status = module('gateway.status', _get_process_start_time=lambda pid: self.start.get(pid), runtime_status_pid_is_live=lambda row: row['start_time'] == self.start.get(row['pid']))
        self.status.__dict__['shlex'] = shlex
        functions('gateway/status.py', {'_gateway_command_subcommand', 'looks_like_gateway_command_line'}, self.status.__dict__)
        self.stderr = module('clover_cli.stderr_timestamp', os=os, EXTERNAL_SUPERVISOR_FLAG='--external-supervisor')
        functions('clover_cli/stderr_timestamp.py', {'_is_clover_gateway_run_argv', '_is_launchd_supervised', '_with_external_supervisor_flag', '_prepare_child_command'}, self.stderr.__dict__)
        outer = self

        class Process:
            def __init__(self, pid):
                self.pid = pid
                self.data = outer.rows[pid]
            def cmdline(self):
                return list(self.data['argv'])
            def ppid(self):
                return self.data['parent']
            def environ(self):
                return {'XPC_SERVICE_NAME': 'ai.clover.gateway' if self.pid == 40563 else '0', 'VIRTUAL_ENV': '/Users/ant/agents/clover-c1/venv'}
            def exe(self):
                return self.data['argv'][0]
            def cwd(self):
                return str(outer.home)
            def create_time(self):
                return outer.start[self.pid] / 100
            def uids(self):
                return SimpleNamespace(real=501, effective=501, saved=501)

        self.modules = {
            'psutil': module('psutil', Process=Process),
            'gateway': module('gateway'),
            'gateway.status': self.status,
            'clover_cli': module('clover_cli'),
            'clover_cli.stderr_timestamp': self.stderr,
            'clover_cli.gateway': module('clover_cli.gateway', find_gateway_pids=lambda **kw: [40563, 40564], find_profile_gateway_processes=lambda **kw: [SimpleNamespace(pid=40564, path=self.home, profile='default')]),
            'clover_cli.update_cmd': module('clover_cli.update_cmd', _gateway_pid_install_verdict=lambda pid: 'unknown'),
        }
        self.ctx = ExitStack()
        self.addCleanup(self.ctx.close)
        self.ctx.enter_context(patch.dict(sys.modules, self.modules))
        self.guard = {'Path': Path, 'json': json, 'CAPABILITY': 'reply-carriers-through-ack-v1'}
        functions('clover_cli/update_delivery_guard.py', {'_proved_stderr_wrapper', '_unsafe_gateways'}, self.guard)

    def proved(self):
        return self.guard['_proved_stderr_wrapper'](40563, self.admitted)

    def test_actual_suni_framework_shape_is_owned(self):
        self.assertTrue(self.proved(), 'real owned wrapper has different outer/inner argv[0] and normalized child argv[0]')

    def test_only_outer_alias_correction_is_insufficient(self):
        # Remove only the first lexical mismatch. Child still has native argv[0].
        self.rows[40563]['argv'][0] = VENV
        self.assertTrue(self.proved(), 'worker OS argv normalization must also preserve verified ownership')

    def test_lexically_equal_positive_control(self):
        self.rows[40563]['argv'][0] = VENV
        self.rows[40564]['argv'][0] = VENV
        self.assertTrue(self.proved())

    def test_foreign_supervisor_rejected(self):
        self.rows[40564]['parent'] = 77777
        self.assertFalse(self.proved())

    def test_stale_worker_fingerprint_rejected(self):
        self.start[40564] += 1
        self.assertFalse(self.proved())

    def test_wrong_profile_log_rejected(self):
        self.rows[40563]['argv'][4] = str(self.home / 'foreign/logs/gateway.error.log')
        self.assertFalse(self.proved())

    def test_unadmitted_worker_rejected(self):
        self.admitted.clear()
        self.assertFalse(self.proved())

    def test_other_venv_on_same_base_interpreter_rejected(self):
        # Same framework executable is not proof of shared venv ownership.
        self.rows[40563]['argv'][6] = '/Users/ant/foreign/venv/bin/python'
        self.assertFalse(self.proved())

    def test_unknown_process_start_rejected(self):
        self.start.pop(40563)
        self.assertFalse(self.proved())

    def test_foreign_worker_command_rejected(self):
        self.rows[40564]['argv'] = [FRAMEWORK, '-m', 'unrelated', 'gateway', 'run']
        self.assertFalse(self.proved())

    def test_frozen_caller_defers_without_forced_restart_and_keeps_recovery(self):
        if os.environ.get('SUNI_SOURCE_REF') != PUBLISHED:
            self.skipTest('exact historical-policy replay requires published source pin')
        (self.home / 'gateway_state.json').write_text(json.dumps(self.row))
        pending = self.home / 'fleet_restart_pending'
        pending.write_text('expected_sha=' + PUBLISHED)
        snapshot = self.home / 'saved-snapshot-sentinel'
        snapshot.write_text('preserve')
        beacon = self.home / 'private-updater-beacon.json'
        beacon.write_text(json.dumps({'updater_pid': os.getpid(), 'updater_create_time': 42}))
        receipts = []
        receipt = module('clover_cli.update_receipt', _current=SimpleNamespace(data={}), finalize_update_receipt=lambda outcome, **kw: receipts.append((outcome, kw)))
        watcher = module('clover_cli.update_restart_watcher', beacon_path=lambda: beacon, _updater_identity_alive=lambda row: True)
        self.ctx.enter_context(patch.dict(sys.modules, {'clover_cli.update_receipt': receipt, 'clover_cli.update_restart_watcher': watcher}))
        functions('clover_cli/update_delivery_guard.py', {'require_delivery_safe_update', 'defer_unsafe_restart', 'disarm_owned_restart_watcher'}, self.guard)
        effects = []
        with self.assertRaises(SystemExit) as caught:
            try:
                self.guard['require_delivery_safe_update']()
            except Exception:
                effects.append('frozen-caller-forced-restart')
            effects.append('downstream-runtime-mutation')
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(effects, [])
        self.assertFalse(beacon.exists())
        self.assertEqual(pending.read_text(), 'expected_sha=' + PUBLISHED)
        self.assertEqual(snapshot.read_text(), 'preserve')
        self.assertEqual(receipts, [('failed', {'stop_reason': 'PID 40563: no owner-bound gateway status'})])
        self.assertTrue(receipt._current.data['delivery_restart_guard']['owned_watcher_disarmed'])

    def test_full_discovery_only_identifies_legacy_worker_not_wrapper(self):
        # This is the published incident replay, not a candidate acceptance assertion.
        if os.environ.get('SUNI_SOURCE_REF') != PUBLISHED:
            self.skipTest('exact historical-policy replay requires published source pin')
        (self.home / 'gateway_state.json').write_text(json.dumps(self.row))
        reasons = self.guard['_unsafe_gateways']()
        self.assertEqual(set(reasons), {
            'legacy: default (PID 40564): identified legacy runtime',
            'PID 40563: no owner-bound gateway status',
        })


if __name__ == '__main__':
    unittest.main(verbosity=2)
