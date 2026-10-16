"""Exact-target native proof utilities. No product imports until installed probe.

The isolated bare origin has main pointing at REAL candidate objects. It is a
fixture download source, not GitHub/main or an identity replacement. Nothing
writes remote refs. All commands fail closed and retain their actual status.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

MARKER = '.github/win-e2e/NEXT_MARKER.txt'


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def full_sha(value):
    if not re.fullmatch('[0-9a-f]{40}', value):
        raise ValueError('expected literal lowercase full commit/tree SHA')
    return value


def plan(repo, old_ref, target_ref, target_tree, next_ref):
    full_sha(target_ref)
    full_sha(target_tree)
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', old_ref):
        raise ValueError('old_ref must be an explicit release tag')
    if not next_ref.startswith('ci/native-next/'):
        raise ValueError('next_ref must be a unique ci/native-next/<run-name> branch')
    git(repo, 'check-ref-format', 'refs/heads/' + next_ref)
    if git(repo, 'replace', '-l'):
        raise ValueError('replacement refs are prohibited')
    target = git(repo, 'rev-parse', target_ref + '^{commit}')
    if target != target_ref or git(repo, 'rev-parse', target + '^{tree}') != target_tree:
        raise ValueError('candidate SHA/tree pin mismatch')
    old = git(repo, 'rev-parse', 'refs/tags/' + old_ref + '^{commit}')
    git(repo, 'merge-base', '--is-ancestor', old, target)
    try:
        nxt = git(repo, 'rev-parse', '--verify', '--quiet', 'refs/remotes/origin/' + next_ref + '^{commit}')
    except subprocess.CalledProcessError:
        nxt = git(repo, 'rev-parse', 'refs/heads/' + next_ref + '^{commit}')
    if git(repo, 'show', '-s', '--format=%P', nxt).split() != [target]:
        raise ValueError('next must have exactly one parent: candidate')
    if git(repo, 'diff', '--name-only', target, nxt).splitlines() != [MARKER]:
        raise ValueError('next must change only the dedicated marker')
    return dict(old_sha=old, target_sha=target, target_tree=target_tree,
                next_sha=nxt, next_tree=git(repo, 'rev-parse', nxt + '^{tree}'),
                old_ref=old_ref, next_ref=next_ref)


def fixture_origin(repo, mirror, target):
    full_sha(target)
    mirror = Path(mirror).resolve()
    if mirror.exists():
        raise ValueError('fixture origin must be new for this run')
    if git(repo, 'replace', '-l'):
        raise ValueError('replacement refs are prohibited')
    target_tree = git(repo, 'rev-parse', '--verify', target + '^{tree}')
    subprocess.run(['git', 'init', '--bare', str(mirror)], check=True)
    # Installer checkouts are shallow. Accept their real shallow boundary in
    # this disposable bare download source; do not rewrite/unshallow the install.
    subprocess.run(['git', '-C', str(mirror), 'fetch', '--no-tags', '--update-shallow',
                    str(Path(repo).resolve()), target + ':refs/heads/main'], check=True)
    if (git(mirror, 'rev-parse', 'refs/heads/main^{commit}') != target
            or git(mirror, 'rev-parse', 'refs/heads/main^{tree}') != target_tree):
        raise ValueError('fixture origin commit/tree mismatch')
    git(repo, 'remote', 'set-url', 'origin', str(mirror))
    git(repo, 'config', 'remote.origin.fetch', '+refs/heads/main:refs/remotes/origin/main')
    git(repo, 'config', 'remote.upstream.url', str(mirror))
    git(repo, 'config', 'remote.upstream.fetch', '+refs/heads/main:refs/remotes/upstream/main')
    git(repo, 'fetch', 'origin')
    git(repo, 'fetch', 'upstream')
    git(repo, 'checkout', '-B', 'main', 'HEAD')
    git(repo, 'branch', '--set-upstream-to=origin/main', 'main')
    if git(repo, 'ls-remote', 'origin', 'refs/heads/main').split()[0] != target:
        raise ValueError('fixture origin pin mismatch')


def check_receipt(receipt, began, old, target):
    from datetime import datetime, timezone
    try:
        start = datetime.fromisoformat(receipt['started_at']).timestamp()
        end = datetime.fromisoformat(receipt['finished_at']).timestamp()
        now = datetime.now(timezone.utc).timestamp()
        valid = (began <= start <= end <= now and receipt['outcome'] == 'success'
                 and receipt['pre_update']['sha'] == old
                 and receipt['post_update']['sha'] == target)
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError('missing, stale, provisional or wrong-source update receipt')


def check_gateway(state, sha, roots, listeners):
    pid = state.get('pid')
    if (not isinstance(pid, int) or not state.get('start_time')
            or state.get('code_sha') != sha or state.get('gateway_state') != 'running'
            or state.get('restart_requested') is not False
            or roots != [pid] or listeners != [pid]):
        raise ValueError(f'gateway identity/port/count mismatch: {state}; roots={roots}; listeners={listeners}')


def _installed_byte_observation(data):
    """Describe bytes without publishing source text or credential contents."""
    import hashlib
    return dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                crlf=data.count(b'\r\n'), lf=data.count(b'\n'),
                bare_cr=data.count(b'\r') - data.count(b'\r\n'))


def _installed_source_diagnostics(repo, sha, tree, replacement_refs):
    """Read-only evidence, never a normalization rule or a source repair.

    All blob/HEAD readbacks bypass replacements. Git's optional index refresh
    is disabled; no source/config contents, remote URLs, or filter commands
    are emitted. Byte comparisons are observations only: rejection is final.
    """
    import hashlib
    import stat
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1',
               GIT_LITERAL_PATHSPECS='1')

    def capture(*args, include_stdout=True):
        record: dict = dict(command=list(args))
        try:
            result = subprocess.run(['git', '-C', str(repo), *args], env=env,
                                    capture_output=True, timeout=15)
            record.update(returncode=result.returncode,
                          stderr_bytes=len(result.stderr),
                          stderr_sha256=hashlib.sha256(result.stderr).hexdigest())
            if include_stdout:
                record['stdout'] = os.fsdecode(result.stdout)
            return record, result.stdout if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired) as exc:
            record['error'] = type(exc).__name__
            return record, None

    data: dict = dict(schema='native-installed-source-rejection/v1', accepted=False,
                repo=str(repo), declared=dict(sha=sha, tree=tree),
                replacement_refs=replacement_refs, git={}, paths=[])
    commands = {
        'head': ('rev-parse', 'HEAD'),
        'tree': ('rev-parse', 'HEAD^{tree}'),
        'replace_refs': ('for-each-ref', '--format=%(refname) %(objectname)', 'refs/replace/'),
        'status': ('status', '--porcelain=v1', '-z', '--untracked-files=no'),
        'diff_raw': ('diff', '--no-ext-diff', '--no-textconv', '--raw', '--no-abbrev', '-z', 'HEAD'),
        'diff_name_only': ('diff', '--no-ext-diff', '--no-textconv', '--name-only', '-z', 'HEAD'),
    }
    for name, args in commands.items():
        data['git'][name], _ = capture(*args)
    data['observed'] = dict(sha=data['git']['head'].get('stdout', '').strip(),
                            tree=data['git']['tree'].get('stdout', '').strip())
    data['git']['config'] = {}
    for key in ['core.autocrlf', 'core.eol', 'core.safecrlf', 'core.filemode']:
        data['git']['config'][key], _ = capture('config', '--get', key)
    paths = data['git']['diff_name_only'].get('stdout', '').split('\0')
    data['changed_paths'] = [path for path in paths if path]
    for path in data['changed_paths']:
        entry: dict = dict(path=path)
        data['paths'].append(entry)
        entry['attributes'], _ = capture('check-attr', '-z', 'text', 'eol',
                                          'working-tree-encoding', 'filter', 'ident', '--', path)
        entry['index_attributes'], _ = capture('check-attr', '--cached', '-z', 'text', 'eol',
                                                'working-tree-encoding', 'filter', 'ident', '--', path)
        entry['index_entry'], _ = capture('ls-files', '--stage', '-z', '--', path)
        entry['eol'], _ = capture('ls-files', '--eol', '-z', '--', path)
        entry['declared_entry'], listing = capture('ls-tree', '-z', sha, '--', path)
        declared = None
        if listing:
            mode, kind, oid = listing.split(b'\t', 1)[0].decode('ascii').split()
            entry.update(declared_mode=mode, declared_kind=kind, declared_oid=oid)
            if kind == 'blob':
                entry['blob_read'], declared = capture('cat-file', 'blob', oid, include_stdout=False)
                if declared is not None:
                    entry['declared_blob'] = _installed_byte_observation(declared)
        candidate = repo / path
        checkout = None
        try:
            # Refuse to follow a changed path or parent outside the checkout.
            if not candidate.resolve().is_relative_to(repo):
                entry['checkout'] = dict(error='outside_checkout')
            elif stat.S_ISREG(candidate.lstat().st_mode):
                checkout = candidate.read_bytes()
                entry['checkout'] = dict(kind='regular', **_installed_byte_observation(checkout))
            else:
                entry['checkout'] = dict(kind='non_regular')
        except OSError as exc:
            entry['checkout'] = dict(error=type(exc).__name__)
        if checkout is not None and declared is not None:
            entry['raw_bytes_equal_declared'] = checkout == declared
            entry['crlf_to_lf_equal_declared'] = checkout.replace(b'\r\n', b'\n') == declared
    return data


def installed(repo, sha, tree):
    import importlib
    import sysconfig
    from urllib.parse import unquote, urlparse
    repo = Path(repo).resolve()
    if Path.cwd().resolve().is_relative_to(repo) or os.environ.get('PYTHONPATH'):
        raise ValueError('installed probe must run outside source, with no PYTHONPATH')
    if git(repo, 'rev-parse', 'HEAD') != sha or git(repo, 'rev-parse', 'HEAD^{tree}') != tree:
        raise ValueError('installed HEAD/tree mismatch')
    replacement_refs = git(repo, 'replace', '-l')
    changed_source = '' if replacement_refs else git(repo, 'diff', '--name-only', 'HEAD')
    if replacement_refs or changed_source:
        import sys
        try:
            diagnostic = _installed_source_diagnostics(repo, sha, tree, replacement_refs)
        except Exception as exc:
            # Diagnostic errors cannot turn rejection into acceptance or mask it.
            diagnostic = dict(schema='native-installed-source-rejection/v1', accepted=False,
                              declared=dict(sha=sha, tree=tree), replacement_refs=replacement_refs,
                              diagnostic_error=type(exc).__name__)
        if os.environ.get('RUNNER_TEMP'):
            try:
                destination = (Path(os.environ['RUNNER_TEMP']) / 'evidence' /
                               'installed-source-rejection.json').resolve()
                if destination.is_relative_to(repo):
                    raise ValueError('diagnostic destination must be outside source')
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(json.dumps(diagnostic, indent=2) + '\n', encoding='utf-8')
            except (OSError, ValueError) as exc:
                diagnostic['artifact_error'] = type(exc).__name__
        print('INSTALLED_SOURCE_REJECTION ' + json.dumps(diagnostic), file=sys.stderr, flush=True)
        raise ValueError('replacement refs or modified tracked source')
    origins = {}
    # The released old version predates workstreams; probe shared package
    # origins here. Candidate feature imports execute in the native suites.
    for name in ['clover_cli', 'agent', 'cron.notepad', 'gateway']:
        module = importlib.import_module(name)
        origin = Path(module.__file__).resolve()
        if not origin.is_relative_to(repo):
            raise ValueError(f'foreign import: {name} at {origin}')
        origins[name] = str(origin)
    metadata = list(Path(sysconfig.get_path('purelib')).glob('clover_c1-*.dist-info/direct_url.json'))
    if len(metadata) != 1:
        raise ValueError(f'expected one explicit installed direct_url.json, found {metadata}')
    direct = json.loads(metadata[0].read_text())
    parsed = urlparse(direct['url'])
    path = unquote(parsed.path)
    if os.name == 'nt' and re.match(r'^/[A-Za-z]:', path):
        path = path[1:]
    if parsed.scheme != 'file' or Path(path).resolve() != repo:
        raise ValueError('editable installation points outside installed checkout')
    return dict(head=sha, tree=tree, origins=origins, direct_url=direct,
                metadata_path=str(metadata[0]), origin=git(repo, 'remote', 'get-url', 'origin'))


def observe_gateway(repo, home, sha, port):
    import psutil
    import sys
    import urllib.request
    home = Path(home).resolve()
    repo = Path(repo).resolve()
    state = json.loads((home / 'gateway_state.json').read_text())
    processes = []
    for proc in psutil.process_iter(['cmdline']):
        candidate = False
        try:
            argv = proc.info['cmdline'] or []
            candidate = ('gateway' in argv and 'run' in argv and '-c' not in argv
                         and str(repo) in ' '.join(argv))
            if candidate and proc.environ().get('CLOVER_HOME') == str(home):
                processes.append(proc.pid)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            # Ignore unrelated host processes, never an unobservable owned PID
            # or possible second gateway from this disposable install.
            if candidate or proc.pid == state.get('pid'):
                raise
    proc = None
    if sys.platform == 'darwin':
        if sorted(processes) != [state.get('pid')]:
            raise ValueError(f'gateway process count/ownership mismatch: {processes}')
        proc = psutil.Process(state['pid'])
        # macOS's system-wide scan walks protected host processes. The native
        # per-process API can inspect our own gateway without root. A denied
        # or empty observation is NOT ownership evidence and must fail closed.
        listeners = sorted({proc.pid for c in proc.net_connections(kind='inet')
                            if c.status == psutil.CONN_LISTEN and c.laddr.port == port})
    else:
        listeners = sorted({c.pid for c in psutil.net_connections(kind='inet')
                            if c.status == psutil.CONN_LISTEN and c.laddr.port == port})
    check_gateway(state, sha, sorted(processes), listeners)
    if proc is None:
        proc = psutil.Process(state['pid'])
    if proc.status() == psutil.STATUS_ZOMBIE:
        raise ValueError('gateway is a zombie')
    recorded = str(state['start_time'])
    stat = Path('/proc') / str(proc.pid) / 'stat'
    if stat.exists():
        actual = stat.read_text().rsplit(')', 1)[1].split()[19]
        if actual != recorded:
            raise ValueError('live process start ticks mismatch')
    elif abs(proc.create_time() * 100 - float(recorded)) >= 1:
        raise ValueError('live process start time mismatch')
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3) as response:
        if response.status != 200 or json.load(response).get('status') != 'ok':
            raise ValueError('gateway health endpoint failed')
    return state


def gateway_window(args):
    import time
    previous = json.loads(Path(args.previous).read_text(encoding='utf-8')) if args.previous else None
    deadline = time.monotonic() + 180
    while True:
        try:
            first = observe_gateway(args.repo, args.home, args.sha, args.port)
            if previous and (first['pid'] == previous['pid'] or first['start_time'] == previous['start_time']):
                raise ValueError('updater has not restarted the gateway')
            break
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise
            print(f'waiting for gateway: {type(exc).__name__}: {exc}', flush=True)
            time.sleep(2)
    observations = [first]
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        current = observe_gateway(args.repo, args.home, args.sha, args.port)
        if any(current[k] != first[k] for k in ['pid', 'start_time', 'code_sha']):
            raise ValueError('gateway changed during stability window')
        observations.append(current)
        time.sleep(min(3, max(0, end - time.monotonic())))
    Path(args.output).write_text(json.dumps(first, indent=2), encoding='utf-8')
    Path(args.output + '.window.json').write_text(json.dumps(observations, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan')
    for name in ['repo', 'old-ref', 'target-ref', 'target-tree', 'next-ref']:
        p.add_argument('--' + name, required=True)
    p.add_argument('--output', required=True)
    p = sub.add_parser('origin')
    for name in ['repo', 'mirror', 'target']:
        p.add_argument('--' + name, required=True)
    p = sub.add_parser('identity')
    for name in ['repo', 'sha', 'tree', 'output']:
        p.add_argument('--' + name, required=True)
    p = sub.add_parser('gateway')
    for name in ['repo', 'home', 'sha', 'output']:
        p.add_argument('--' + name, required=True)
    p.add_argument('--port', type=int, required=True)
    p.add_argument('--seconds', type=int, required=True, choices=[0, 90])
    p.add_argument('--previous')
    p = sub.add_parser('begin')
    p.add_argument('--output', required=True)
    p = sub.add_parser('receipt')
    for name in ['home', 'begin', 'old', 'target', 'output']:
        p.add_argument('--' + name, required=True)
    p = sub.add_parser('preflight')
    p.add_argument('--repo', required=True)
    p.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == 'preflight':
        data = plan(args.repo, os.environ['OLD_REF'], os.environ['TARGET_REF'],
                    os.environ['TARGET_TREE'], os.environ['NEXT_REF'])
        if (data['next_sha'] != os.environ['NEXT_SHA'] or data['next_tree'] != os.environ['NEXT_TREE']
                or data['old_sha'] != os.environ['OLD_SHA']
                or git(args.repo, 'rev-parse', 'HEAD') != data['target_sha']):
            raise ValueError('source refs moved since plan job; dispatch with new reviewed pins')
        Path(args.output).write_text(json.dumps(data, indent=2), encoding='utf-8')
    elif args.command == 'identity':
        Path(args.output).write_text(json.dumps(installed(args.repo, args.sha, args.tree), indent=2), encoding='utf-8')
    elif args.command == 'gateway':
        gateway_window(args)
    elif args.command == 'begin':
        import time
        Path(args.output).write_text(json.dumps(dict(began=time.time(), run_id=os.environ.get('GITHUB_RUN_ID'),
            attempt=os.environ.get('GITHUB_RUN_ATTEMPT'), workflow_sha=os.environ.get('GITHUB_SHA'))), encoding='utf-8')
    elif args.command == 'receipt':
        receipt = json.loads((Path(args.home) / 'logs/update_receipts/latest.json').read_text(encoding='utf-8'))
        # Preserve the actual receipt even if validation rejects it.
        Path(args.output).write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        check_receipt(receipt, json.loads(Path(args.begin).read_text(encoding='utf-8'))['began'], args.old, args.target)
    elif args.command == 'plan':
        data = plan(args.repo, args.old_ref, args.target_ref, args.target_tree, args.next_ref)
        Path(args.output).write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as fh:
                for key, value in data.items():
                    fh.write(f'{key}={value}\n')
        print(json.dumps(data))
    elif args.command == 'origin':
        fixture_origin(args.repo, args.mirror, args.target)


if __name__ == '__main__':
    main()
