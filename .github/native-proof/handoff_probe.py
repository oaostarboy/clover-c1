#!/usr/bin/env python3
"""Bounded, read-only Windows handoff diagnostics for disposable hosted runners."""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time


def _read(path):
    try:
        with path.open('r', encoding='utf-8-sig') as handle:
            text = handle.read(2_000_001)
        if len(text) > 2_000_000:
            return {}, 'Oversized'
        data = json.loads(text)
        return (data, None) if isinstance(data, dict) else ({}, 'NotObject')
    except (OSError, ValueError) as exc:
        return {}, type(exc).__name__


def _pid(value):
    return value if type(value) is int and 0 < value <= 0xFFFFFFFF else None


def _number(value):
    return value if type(value) in (int, float) and 0 <= value <= 10**12 and math.isfinite(value) else None


def _sha(value):
    return value if isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value) else None


def _stamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        dt.datetime.fromisoformat(value)
        return value
    except ValueError:
        return None


def sample(home, repo):
    """Never copy command arguments, arbitrary fields, config or secret files."""
    beacon, beacon_error = _read(home / '.clover-update-heartbeat.json')
    receipt, receipt_error = _read(home / 'logs/update_receipts/latest.json')
    beacon_repo = beacon.get('repo')
    same_repo = isinstance(beacon_repo, str) and os.path.normcase(os.path.abspath(beacon_repo)) == os.path.normcase(os.path.abspath(repo))
    snapshot = beacon.get('pre_update_snapshot_id')
    before = receipt.get('pre_update')
    after = receipt.get('post_update')
    outcome = receipt.get('outcome')
    return {
        'beacon': {
            'updater_pid': _pid(beacon.get('updater_pid')),
            'updater_create_time': _number(beacon.get('updater_create_time')),
            'refreshed_at': _number(beacon.get('refreshed_at')),
            'pre_pull_sha': _sha(beacon.get('pre_pull_sha')),
            'repo_matches_install': same_repo,
            'snapshot': snapshot if isinstance(snapshot, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,180}', snapshot) else None,
            'ready_for_probe': beacon.get('ready_for_probe') if type(beacon.get('ready_for_probe')) is bool else None,
            'read_error': beacon_error,
        },
        'receipt': {
            'pid': _pid(receipt.get('pid')),
            'started_at': _stamp(receipt.get('started_at')),
            'finished_at': _stamp(receipt.get('finished_at')),
            'outcome': outcome if outcome in ('running', 'success', 'failed', 'refused', 'cancelled') else None,
            'pre_sha': _sha(before.get('sha')) if isinstance(before, dict) else None,
            'post_sha': _sha(after.get('sha')) if isinstance(after, dict) else None,
            'pre_pull_sha': _sha(receipt.get('pre_pull_sha')),
            'read_error': receipt_error,
        },
    }


def record(history, observation, limit=128):
    """Keep the initial and most recent observations, never grow without bound."""
    if limit < 2:
        raise ValueError('History limit must retain initial and latest observations')
    if history and all(history[-1].get(key) == observation.get(key) for key in ('beacon', 'receipt')):
        return False
    history.append(observation)
    if len(history) > limit:
        del history[1]
    return True


def processes(owner_pid):
    """CIM facts only; raw command lines never cross the diagnostic boundary."""
    if _pid(owner_pid) is None:
        return []
    command = r'''
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$ownerPid = OWNER_PID
$items = @(Get-CimInstance Win32_Process | Where-Object {
  $_.ProcessId -eq $ownerPid -or $_.ParentProcessId -eq $ownerPid -or
  $_.CommandLine -match '(clover_cli[.]main|clover[.]exe)"?\s+update'
} | ForEach-Object {
  [pscustomobject]@{ pid=[int]$_.ProcessId; parent_pid=[int]$_.ParentProcessId;
    executable=$_.ExecutablePath; created_utc=$_.CreationDate.ToUniversalTime().ToString('o');
    updater_command=[bool]($_.CommandLine -match '(clover_cli[.]main|clover[.]exe)"?\s+update') }
})
ConvertTo-Json -InputObject $items -Depth 4 -Compress
'''.replace('OWNER_PID', str(owner_pid))
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, text=True, encoding='utf-8-sig', errors='replace', timeout=8, check=True)
        data = json.loads(result.stdout)
        return data if isinstance(data, list) else []
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return [{'probe_error': type(exc).__name__}]


def main():
    parser = argparse.ArgumentParser()
    for name in ('home', 'repo', 'output', 'stop'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=1800)
    args = parser.parse_args()
    temporary = Path(os.environ.get('RUNNER_TEMP', '.')).resolve()
    if (os.name != 'nt' or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'
            or args.home.resolve() != Path(os.environ.get('CLOVER_HOME', '.')).resolve()
            or args.repo.resolve() != Path(os.environ.get('INSTALL_DIR', '.')).resolve()
            or not args.output.resolve().is_relative_to(temporary)
            or not args.stop.resolve().is_relative_to(temporary)
            or args.output.resolve().is_relative_to(args.home.resolve())
            or not 0 < args.seconds <= 1800):
        raise SystemExit('Disposable hosted diagnostic scope required')
    history = []
    deadline = time.monotonic() + args.seconds
    destination = args.output.with_suffix('.tmp')
    while True:
        observation = sample(args.home, args.repo)
        observation['observed_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
        changed = record(history, observation)
        if changed:
            observation['processes'] = processes(observation['beacon']['updater_pid'])
        stopped = args.stop.exists()
        complete = stopped or time.monotonic() >= deadline
        if changed or complete:
            destination.write_text(json.dumps({'complete': complete, 'termination': 'stop-marker' if stopped else 'bounded-timeout' if complete else None,
                                               'limit': 128, 'observations': history},
                                              indent=2, allow_nan=False) + '\n', encoding='utf-8')
            destination.replace(args.output)
        if complete:
            return
        time.sleep(0.2)


if __name__ == '__main__':
    main()
