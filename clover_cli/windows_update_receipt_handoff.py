"""Owned Windows shim receipt continuation; no gateway or service actions.

Uses a separate system PowerShell CIM query rather than importing psutil into
an updater that is about to replace venv extension modules. Missing ownership
is explicitly unverified. Only the current, proven handed-off logical run may
be continued; ordinary historical success receipts are never adopted.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import re
import subprocess
import time

TAG = 'windows_dependency_handoff'


class Unverified(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise Unverified(reason)


def _pid(value):
    return type(value) is int and 0 < value <= 0xFFFFFFFF


def _sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value) is not None


def _key(value):
    _require(isinstance(value, (str, Path)) and str(value), 'missing executable path')
    return ntpath.normcase(ntpath.normpath(str(value)))


def _stamp(value):
    _require(isinstance(value, str) and len(value) <= 80, 'invalid timestamp')
    result = datetime.fromisoformat(value)
    _require(result.tzinfo is not None, 'timestamp lacks timezone')
    return result.timestamp()


def _read(path):
    _require(path.is_file() and not path.is_symlink(), 'missing or symlinked handoff record')
    raw = path.read_bytes()
    _require(len(raw) <= 2_000_000, 'oversized handoff record')
    result = json.loads(raw)
    _require(isinstance(result, dict), 'handoff record is not an object')
    return result, raw


def _atomic(path, raw):
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _json(value):
    return (json.dumps(value, indent=2, allow_nan=False) + '\n').encode('utf-8')


def process_chain(pid, *, timeout=5):
    """Read current ancestry, executable and creation time without loading pyds."""
    _require(_pid(pid), 'invalid process identity')
    command = r'''
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$ErrorActionPreference = 'Stop'
$all = @(Get-CimInstance Win32_Process)
$cursor = CURRENT_PID
$seen = @{}
$chain = @()
for ($i=0; $i -lt 8 -and $cursor -gt 0; $i++) {
  if ($seen.ContainsKey($cursor)) { throw 'Cyclic process ancestry' }
  $seen[$cursor] = $true
  $item = @($all | Where-Object { $_.ProcessId -eq $cursor })
  if ($item.Count -ne 1) { break }
  $item = $item[0]
  $chain += [pscustomobject]@{ pid=[int]$item.ProcessId; parent_pid=[int]$item.ParentProcessId;
    executable=$item.ExecutablePath; created_utc=$item.CreationDate.ToUniversalTime().ToString('o') }
  $cursor = [int]$item.ParentProcessId
}
ConvertTo-Json -InputObject $chain -Depth 4 -Compress
'''.replace('CURRENT_PID', str(pid))
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', command],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding='utf-8-sig', errors='strict',
                            timeout=timeout, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    chain = json.loads(result.stdout)
    _require(isinstance(chain, list) and 0 < len(chain) <= 8, 'missing process ancestry')
    return chain


def _witness(chain, current, parent, project_root, interpreter, now):
    _require(isinstance(chain, list) and chain and chain[0].get('pid') == current['pid'], 'wrong current process')
    _require(_key(interpreter['prefix']) == _key(project_root / 'venv'), 'foreign interpreter prefix')
    venv = project_root / 'venv/Scripts'
    venv_paths = {_key(venv / 'python.exe'), _key(venv / 'pythonw.exe')}
    _require(_key(interpreter['executable']) in venv_paths, 'foreign venv interpreter')
    allowed = venv_paths | {_key(interpreter['base_executable'])}
    root_pid = parent['pid']
    seen = set()
    owned = []
    for item in chain:
        _require(isinstance(item, dict) and _pid(item.get('pid')) and _pid(item.get('parent_pid')), 'invalid process row')
        _require(item['pid'] not in seen, 'cyclic process witness')
        seen.add(item['pid'])
        _require(_key(item.get('executable')) in allowed, 'foreign continuation executable')
        created = _stamp(item.get('created_utc'))
        _require(_stamp(parent['started_at']) <= created <= now() + 1, 'process outside current run')
        if owned:
            _require(owned[-1]['parent_pid'] == item['pid'] and created <= owned[-1]['created'], 'broken ancestry')
        owned.append({**item, 'created': created})
        if item['parent_pid'] == root_pid:
            break
    else:
        raise Unverified('no original parent edge')
    _require(_stamp(current['started_at']) >= owned[0]['created'], 'receipt predates continuation')
    # A still-live original producer may overwrite latest after we return.
    for item in chain[len(owned):]:
        if item.get('pid') == root_pid and _stamp(item.get('created_utc')) <= _stamp(parent['started_at']):
            raise Unverified('original producer still alive')
    return owned


def _validate_parent(parent, current, project_root, interpreter):
    _require(type(parent.get('schema')) is int and parent['schema'] == 1, 'invalid receipt schema')
    _require(_pid(parent.get('pid')) and parent['pid'] != current['pid'], 'invalid original pid')
    pre = parent.get('pre_update'); post = parent.get('post_update')
    _require(isinstance(pre, dict) and _sha(pre.get('sha')), 'missing original source')
    _require(isinstance(post, dict) and post.get('sha') == current['pre_update'].get('sha') and _sha(post.get('sha')), 'wrong handoff target')
    _require(parent.get('pre_pull_sha', pre['sha']) == pre['sha'], 'original source disagreement')
    argv = parent.get('argv')
    _require(isinstance(argv, list) and all(isinstance(a, str) for a in argv) and len(argv) > 1, 'invalid original argv')
    # distlib's console stub removes .exe from sys.argv[0]; the immutable old
    # producer records that exact extensionless spelling. Accept only these
    # two full paths in this install, never a basename or another environment.
    launchers = {_key(project_root / 'venv/Scripts' / name) for name in ('clover', 'clover.exe')}
    _require(_key(argv[0]) in launchers, 'foreign original launcher')
    _require(argv[1:] == interpreter['argv'][1:], 'different update invocation')
    _require(isinstance(parent.get('steps'), list) and isinstance(parent.get('skips'), list), 'invalid receipt collections')
    _require(isinstance(parent.get('plan'), dict), 'missing original runtime plan')
    _stamp(parent.get('started_at'))


def _beacon(directory, parent, owned, project_root, now, *, legacy_completed_at=None):
    path = directory.parent.parent / '.clover-update-heartbeat.json'
    if not path.exists():
        return False
    beacon, _ = _read(path)
    _require(_pid(beacon.get('updater_pid')), 'invalid beacon owner')
    owners = [item for item in owned if item['pid'] == beacon['updater_pid']]
    _require(len(owners) == 1, 'beacon belongs to another process')
    created = beacon.get('updater_create_time')
    _require(type(created) in (int, float) and 0 < created < 10**12 and math.isfinite(created), 'unproved beacon creation time')
    _require(abs(created - owners[0]['created']) <= .001, 'reused beacon pid')
    # The immutable old pre-pause producer legitimately omits rollback metadata.
    # Lineage, literal launcher/argv, process birth and parent phase boundaries
    # above independently bind that shape. Present metadata must still agree.
    if beacon.get('repo') is not None:
        _require(_key(beacon['repo']) == _key(project_root), 'foreign_beacon_repository')
    if beacon.get('pre_pull_sha') is not None:
        _require(beacon['pre_pull_sha'] == parent['pre_update']['sha'], 'foreign_beacon_source')
    if beacon.get('pre_update_snapshot_id') is not None:
        _require(beacon['pre_update_snapshot_id'] == parent.get('pre_update_snapshot_id'), 'foreign_beacon_snapshot')
    refreshed = beacon.get('refreshed_at')
    lower, upper = owners[0]['created'], now() + 1
    if legacy_completed_at is not None:
        # The immutable legacy producer used a coarse Windows wall clock for
        # refresh, independently of GetProcessTimes' child birth timestamp.
        # Bind that observation to its proven original producer phase instead:
        # same PID+birth/chain/source above, refresh inside parent begin/exit.
        # No clock tolerance or identity waiver; modern transfers stay strict.
        lower = _stamp(parent['started_at'])
        upper = min(upper, legacy_completed_at)
    _require(type(refreshed) in (int, float) and lower <= refreshed <= upper, 'stale beacon')
    _require(beacon.get('ready_for_probe') is not True, 'completed beacon is not a handoff')
    return True


def transfer(parent, child_pid, directory, *, transferred_at):
    """Persist a running handoff using the actual successful Popen PID.

    Do not query processes or import psutil in the exiting producer: delaying
    its exit keeps venv DLLs locked while its child begins dependency repair.
    The child independently measures and validates the complete process chain.
    """
    _require(_pid(child_pid), 'invalid transferred launcher')
    _require(_sha(parent.get('post_update', {}).get('sha')), 'missing transfer target')
    _require(_stamp(parent['started_at']) <= _stamp(transferred_at), 'transfer predates run')
    parent[TAG] = {'state': 'transferred', 'launcher_pid': child_pid,
                   'root_pid': parent['pid'], 'root_started_at': parent['started_at'],
                   'transferred_at': transferred_at, 'target_sha': parent['post_update']['sha']}
    parent['outcome'] = 'running'
    parent['finished_at'] = None
    parent.pop('exit_code', None)
    parent.pop('stop_reason', None)
    directory.mkdir(parents=True, exist_ok=True)
    _atomic(directory / 'latest.json', _json(parent))


def _run_id(parent):
    identity = [parent['pid'], parent['started_at'], parent['pre_update']['sha'], parent['post_update']['sha']]
    return hashlib.sha256(_json(identity)).hexdigest()


def _same_run(left, right):
    keys = ('schema', 'pid', 'started_at', 'argv', 'pre_update', 'plan',
            'pre_pull_sha', 'pre_update_snapshot_id')
    return (type(left.get('schema')) is int and type(left.get('pid')) is int
            and all(left.get(key) == right.get(key) for key in keys)
            and left.get('post_update', {}).get('sha') == right['post_update']['sha'])


def _resume_existing(parent, current, directory, owned, *, latest_raw=None):
    tag = parent[TAG]
    _require(tag.get('continuation_pid') == current['pid']
             and tag.get('continuation_created_at') == owned[0]['created'], 'reused continuation owner')
    name = tag.get('canonical_name')
    _require(isinstance(name, str) and re.fullmatch(
        rf"update_[0-9]{{8}}_[0-9]{{6}}_{parent['pid']}[.]json", name), 'invalid claimed canonical name')
    canonical = directory / name
    existing, canonical_raw = _read(canonical)
    if existing != parent:
        _require(_same_run(existing, parent), 'canonical belongs to another run')
        latest, observed_raw = _read(directory / 'latest.json')
        _require(latest == parent, 'latest changed during recovery')
        latest_raw = observed_raw
        parent = existing
        tag = parent.get(TAG)
        _require(isinstance(tag, dict) and tag.get('canonical_name') == name, 'canonical claim changed')
    _require(tag.get('continuation_pid') == current['pid']
             and tag.get('continuation_created_at') == owned[0]['created']
             and tag.get('anchor_pid') == owned[-1]['pid']
             and tag.get('anchor_created_at') == owned[-1]['created'], 'reused publication owner')
    phases = directory / 'handoff_phases'
    _require(not phases.is_symlink(), 'symlinked phase archive')
    phase = directory / tag['legacy_phase_file']
    _require(phase.parent.resolve() == phases.resolve(), 'foreign phase archive')
    _, raw = _read(phase)
    _require(hashlib.sha256(raw).hexdigest() == tag.get('legacy_phase_sha256'), 'phase archive changed')
    claim, _ = _read(phases / ('claim-' + _run_id(parent) + '.json'))
    _require(claim == {**tag, 'state': 'adopted'}, 'claim belongs to another continuation')
    terminal = parent.get('outcome') != 'running'
    if terminal:
        _require(_stamp(parent.get('finished_at')) >= owned[0]['created'], 'unfinished terminal claim')
    else:
        _require(tag.get('state') == 'adopted', 'invalid running continuation')
    if latest_raw is not None:
        _require((directory / 'latest.json').read_bytes() == latest_raw, 'latest changed before recovery')
        _atomic(directory / 'latest.json', canonical_raw)
    if terminal:
        return canonical, 'duplicate'
    current.clear(); current.update(parent)
    return canonical, 'adopted'


def _recover_publication(parent, current, directory, owned, latest_raw):
    """Re-publish only this live owner's already-claimed canonical record."""
    phases = directory / 'handoff_phases'
    _require(not phases.is_symlink(), 'symlinked publication claims')
    claim_path = phases / ('claim-' + _run_id(parent) + '.json')
    if not claim_path.exists():
        return None
    claim, _ = _read(claim_path)
    name = claim.get('canonical_name')
    _require(isinstance(name, str) and re.fullmatch(
        rf"update_[0-9]{{8}}_[0-9]{{6}}_{parent['pid']}[.]json", name), 'foreign publication name')
    canonical = directory / name
    if not canonical.exists():
        return None
    published, _ = _read(canonical)
    if not isinstance(published.get(TAG), dict):
        return None
    _require(_same_run(published, parent), 'publication belongs to another run')
    return _resume_existing(published, current, directory, owned, latest_raw=latest_raw)


def resume(current, directory, project_root, *, process_chain, interpreter, now, wait_seconds=30):
    """Adopt only an owned parent within a bounded cold-CIM startup budget.

    A five-second total budget allowed only one cold PowerShell/CIM timeout
    on native Windows. Give each cold query at most fifteen seconds and allow
    another measured query before declaring the receipt unverified. All
    source, process-birth, ancestry and publication predicates remain strict.
    """
    began = time.monotonic()
    deadline = began + wait_seconds
    probe = {'elapsed_seconds': 0.0, 'attempt_count': 0, 'attempts': []}
    reason = 'handoff unavailable'
    while True:
        probe_phase = 'parent_receipt'
        process_timeout = None
        try:
            parent, latest_raw = _read(directory / 'latest.json')
            _validate_parent(parent, current, project_root, interpreter)
            probe_phase = 'process_chain'
            process_timeout = max(.1, min(15, deadline - time.monotonic()))
            chain = process_chain(current['pid'], timeout=process_timeout)
            probe_phase = 'process_witness'
            owned = _witness(chain, current, parent, project_root, interpreter, now)
            handoff = parent.get(TAG)
            if isinstance(handoff, dict) and handoff.get('state') in ('adopted', 'completed', 'incomplete'):
                return _resume_existing(parent, current, directory, owned)
            probe_phase = 'parent_completion'
            modern = isinstance(handoff, dict) and handoff.get('state') == 'transferred'
            if modern:
                _require(parent.get('outcome') == 'running' and parent.get('finished_at') is None, 'transfer is not pending')
                _require(handoff.get('root_pid') == parent['pid'] and handoff.get('root_started_at') == parent['started_at'], 'transfer root mismatch')
                _require(handoff.get('launcher_pid') == owned[-1]['pid'] and handoff.get('target_sha') == parent['post_update']['sha'], 'transfer owner mismatch')
                finished = _stamp(handoff.get('transferred_at'))
            else:
                _require(handoff is None, 'unsupported parent handoff protocol')
                _require(parent.get('outcome') == 'success' and parent.get('stop_reason') == 'sys.exit(0)' and type(parent.get('exit_code')) is int and parent['exit_code'] == 0, 'parent has not completed legacy handoff')
                finished = _stamp(parent.get('finished_at'))
            _require(owned[-1]['created'] <= finished <= now() + 1, 'stale original completion')
            probe_phase = 'beacon'
            resume_expected = _beacon(directory, parent, owned, project_root, now,
                                      legacy_completed_at=None if modern else finished)
            probe_phase = 'publication'
            recovered = _recover_publication(parent, current, directory, owned, latest_raw)
            if recovered is not None:
                return recovered
            if modern:
                stamp = datetime.fromisoformat(parent['started_at']).strftime('%Y%m%d_%H%M%S')
                canonical = directory / f"update_{stamp}_{parent['pid']}.json"
                _require(not canonical.exists(), 'transfer canonical archive already exists')
                raw = latest_raw
            else:
                matching = []
                for path in directory.glob(f"update_*_{parent['pid']}.json"):
                    data, raw = _read(path)
                    if data == parent:
                        matching.append((path, raw))
                _require(len(matching) == 1, 'ambiguous or missing original archive')
                canonical, raw = matching[0]
            _require(re.fullmatch(r'update_[0-9]{8}_[0-9]{6}_[0-9]+[.]json', canonical.name), 'invalid canonical filename')
            phases = directory / 'handoff_phases'
            phases.mkdir(exist_ok=True)
            _require(not phases.is_symlink(), 'symlinked phase archive')
            phase = phases / (('transfer-' if modern else 'legacy-') + canonical.name)
            if phase.exists():
                _require(phase.read_bytes() == raw and not phase.is_symlink(), 'legacy phase already differs')
            else:
                with phase.open('xb') as handle:
                    handle.write(raw)
            probe['elapsed_seconds'] = time.monotonic() - began
            tag = {'state': 'adopted', 'root_pid': parent['pid'], 'root_started_at': parent['started_at'],
                   'continuation_pid': current['pid'], 'continuation_created_at': owned[0]['created'],
                   'anchor_pid': owned[-1]['pid'], 'anchor_created_at': owned[-1]['created'],
                   'canonical_name': canonical.name, 'resume_expected': resume_expected,
                   'legacy_phase_file': str(phase.relative_to(directory)),
                   'legacy_phase_sha256': hashlib.sha256(raw).hexdigest(),
                   'phase_kind': 'pending-transfer' if modern else 'legacy-provisional'}
            claim = phases / ('claim-' + _run_id(parent) + '.json')
            if claim.exists():
                existing, _ = _read(claim)
                _require(existing == tag, 'continuation already claimed')
            else:
                with claim.open('xb') as handle:
                    handle.write(_json(tag))
            _require((not canonical.exists()) if modern else canonical.read_bytes() == raw, 'producer archive changed during adoption')
            _require((directory / 'latest.json').read_bytes() == latest_raw, 'latest changed during adoption')
            parent['outcome'] = 'running'; parent['finished_at'] = None
            parent.pop('exit_code', None); parent.pop('stop_reason', None)
            parent[TAG] = tag
            parent[TAG + '_probe'] = probe
            payload = _json(parent)
            _atomic(canonical, payload)
            _atomic(directory / 'latest.json', payload)
            current.clear(); current.update(parent)
            return canonical, 'adopted'
        except Exception as exc:
            reason = str(exc) if isinstance(exc, Unverified) else type(exc).__name__
            probe['elapsed_seconds'] = time.monotonic() - began
            probe['attempt_count'] += 1
            probe['attempts'].append({'phase': probe_phase, 'elapsed_seconds': probe['elapsed_seconds'],
                                      'process_timeout_seconds': process_timeout, 'reason': reason})
            # Retain the first failure and recent predicates without logging
            # subprocess commands, exception payloads, config or unbounded data.
            if len(probe['attempts']) > 16:
                del probe['attempts'][1]
            if time.monotonic() >= deadline:
                current[TAG] = {'state': 'unverified', 'reason': reason, 'probe': probe}
                return None, 'unverified'
            time.sleep(min(.05, max(0, deadline - time.monotonic())))
