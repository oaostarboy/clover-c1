"""Execute immutable released install.ps1 bytes, never an emulated uv install.

Only install() has native side effects, and only on disposable hosted Windows.
The remaining helpers support portable provenance/command/refusal contracts.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

RELEASED = {
    '9535350bee1d57cf7e466672e6f416403886bec1',
    '0c8f3cd0e0ba6caca6948e13f9de92ba519c2029',
    '3b54f413376eb99f8b83bfd4a0a340d01825014d',
}
# Independently read from all three immutable release Git objects; identical bytes.
INSTALLER_SHA256 = '53a81a8029ba670aed376ac9bc13906094cf12b63794993bb30fe6b60799c732'
CANDIDATE_INSTALLER_REF = '3b54f413376eb99f8b83bfd4a0a340d01825014d'
STAGES = ('uv', 'python', 'git', 'node', 'system-packages', 'repository', 'venv',
          'dependencies', 'node-deps', 'path', 'config-templates', 'platform-sdks',
          'bootstrap-marker', 'configure', 'gateway')


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def materialize(repo, caller, candidate, destination):
    """Bind the exact caller to a known released installer, before execution."""
    for ref in (caller, candidate):
        if not re.fullmatch('[0-9a-f]{40}', ref):
            raise ValueError('caller/candidate must be literal full commit SHAs')
    if caller not in RELEASED and caller != candidate:
        raise ValueError('unknown frozen caller; not the explicitly supplied candidate')
    if git(repo, 'replace', '-l'):
        raise ValueError('replacement refs prohibited')
    if git(repo, 'rev-parse', caller + '^{commit}') != caller:
        raise ValueError('caller commit identity mismatch')
    installer_ref = caller if caller in RELEASED else CANDIDATE_INSTALLER_REF
    data = subprocess.check_output(['git', '-C', str(repo), 'show', installer_ref + ':scripts/install.ps1'])
    digest = hashlib.sha256(data).hexdigest()
    if digest != INSTALLER_SHA256:
        raise ValueError('released installer SHA256 mismatch')
    destination = Path(destination)
    # Binary write is intentional: no newline normalization, templating or patching.
    with destination.open('xb') as out:
        out.write(data)
    return dict(caller_sha=caller, caller_tree=git(repo, 'rev-parse', caller + '^{tree}'),
                installer_ref=installer_ref, installer_path='scripts/install.ps1',
                installer_blob=git(repo, 'rev-parse', installer_ref + ':scripts/install.ps1'),
                installer_sha256=digest, executed_path=str(destination.resolve()),
                candidate_installed_by_released_installer=caller not in RELEASED)


def command(path, provenance, *, manifest=False):
    """Require the pinned bytes at the exact -File path on every invocation."""
    path = Path(path).resolve(strict=True)
    if str(path) != provenance['executed_path']:
        raise ValueError('installer path differs from materialized released blob')
    if hashlib.sha256(path.read_bytes()).hexdigest() != provenance['installer_sha256']:
        raise ValueError('materialized installer SHA256 mismatch')
    if provenance['installer_sha256'] != INSTALLER_SHA256:
        raise ValueError('unapproved released installer hash')
    argv = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', str(path), '-Commit', provenance['caller_sha'], '-ForceCommit',
            '-NonInteractive', '-SkipSetup', '-SkipComputerUse', '-Json']
    # No path overrides: prove %LOCALAPPDATA%/clover/clover-c1 and its own venv.
    if manifest:
        argv.append('-Manifest')
    return argv


def frames(path):
    """Extract actual JSON protocol lines; keep the full original log separately."""
    result = []
    for line in Path(path).read_text(encoding='utf-8-sig', errors='replace').splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def validate_manifest(values):
    if len(values) != 1 or values[0].get('protocol_version') != 1:
        raise ValueError('missing or ambiguous installer manifest')
    names = [s['name'] for s in values[0]['stages']]
    if names != list(STAGES):
        raise ValueError('released installer stage manifest changed')
    return values[0]


def validate_completion(values):
    stages = [v for v in values if 'stage' in v]
    if [v['stage'] for v in stages] != list(STAGES):
        raise ValueError('installer did not execute every manifest stage in order')
    if any(v.get('ok') is not True or v.get('skipped') is not False for v in stages):
        raise ValueError('installer stage failed or skipped a required capability')
    if values[-1] != dict(ok=True, protocol_version=1):
        raise ValueError('missing final installer completion frame')
    return stages


def origin_rewrites(mirror):
    return dict(GIT_CONFIG_COUNT='3', GIT_CONFIG_KEY_0='core.autocrlf', GIT_CONFIG_VALUE_0='false',
                GIT_CONFIG_KEY_1='url.' + Path(mirror).resolve().as_uri() + '.insteadOf',
                GIT_CONFIG_VALUE_1='git@github.com:oaostarboy/clover-c1.git',
                GIT_CONFIG_KEY_2='url.' + Path(mirror).resolve().as_uri() + '.insteadOf',
                GIT_CONFIG_VALUE_2='https://github.com/oaostarboy/clover-c1.git')


def install(repo, caller, candidate, evidence, scratch, source, home, uv, env, run):
    """Run the real full installer and preserve failures without any uv fallback."""
    if (sys.platform != 'win32' or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'):
        raise RuntimeError('released installer execution requires disposable hosted Windows')
    if source.exists() or (source / 'venv').exists():
        raise ValueError('refuse pre-created source/venv')
    installer = evidence / 'released-install.ps1'
    provenance = materialize(repo, caller, candidate, installer)
    write_json(evidence / 'released-installer-provenance.json', provenance)
    # Private bare origin: same commits/trees, only download location changes.
    # Both hardcoded released GitHub URLs resolve here; no GitHub credentials.
    mirror = scratch / 'installer-origin.git'
    run(['git', 'clone', '--mirror', '--no-hardlinks', repo, mirror], 'installer-origin-clone')
    run(['git', '-C', mirror, 'update-ref', 'refs/heads/main', caller], 'installer-origin-main')
    if git(mirror, 'rev-parse', 'refs/heads/main^{tree}') != provenance['caller_tree']:
        raise ValueError('installer mirror changed caller tree')
    rewrite = origin_rewrites(mirror)
    write_json(evidence / 'installer-origin-provenance.json',
               dict(source_checkout=str(Path(repo).resolve()), source_head=git(repo, 'rev-parse', 'HEAD'),
                    mirror=str(mirror), main_sha=caller, main_tree=provenance['caller_tree'],
                    rewrite=rewrite, commit_replacement=False, network_clone_certified=False))
    # Provisioned tool, not a claim that install.ps1's bootstrap download ran.
    # Do NOT pre-create Python, a venv, dependencies, templates or launchers.
    managed = home / 'bin' / 'uv.exe'
    managed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(uv, managed)
    if hashlib.sha256(managed.read_bytes()).hexdigest() != hashlib.sha256(Path(uv).read_bytes()).hexdigest():
        raise ValueError('preinstalled managed uv differs from verified bootstrap tool')
    write_json(evidence / 'installer-preinstalled-tools.json',
               dict(uv_source=uv, managed_uv=str(managed), uv_sha256=hashlib.sha256(managed.read_bytes()).hexdigest(),
                    installer_uv_download='NOT_EXERCISED', other_prereqs='hosted image or installer; see raw log',
                    python_selection='released default 3.11; managed-only discovery',
                    bootstrap_download_certified=False))
    # Avoid the hosted image's unrelated system Python: let the shipped python
    # stage provision its own requested interpreter using the pinned safe catalog.
    env['UV_PYTHON_PREFERENCE'] = 'only-managed'
    saved = {key: env.get(key) for key in rewrite}
    env.update(rewrite)
    try:
        run(command(installer, provenance, manifest=True), 'released-installer-manifest', timeout=60)
        manifest = validate_manifest(frames(evidence / 'released-installer-manifest.log'))
        write_json(evidence / 'released-installer-manifest.json', manifest)
        try:
            run(command(installer, provenance), 'released-installer', timeout=2400)
        finally:
            # Partial/failing frames and bytes remain evidence too.
            log = evidence / 'released-installer.log'
            actual = frames(log) if log.exists() else []
            write_json(evidence / 'released-installer-frames.json', actual)
        validate_completion(actual)
    finally:
        for key, value in saved.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
    installed = dict(sha=git(source, 'rev-parse', 'HEAD'),
                     tree=git(source, 'rev-parse', 'HEAD^{tree}'),
                     tracked_changes=git(source, 'diff', '--name-only', 'HEAD').splitlines())
    write_json(evidence / 'released-installer-source.json', installed)
    marker = json.loads((source / '.clover-bootstrap-complete').read_text(encoding='utf-8-sig'))
    if marker.get('schemaVersion') != 1 or marker.get('pinnedCommit') != caller:
        raise ValueError('installer bootstrap marker not bound to exact caller')
    for path in [source / 'venv/Scripts/python.exe', source / 'venv/Scripts/clover.exe',
                 home / 'config.yaml', home / '.env']:
        if not path.is_file():
            raise ValueError('installer did not create required artifact: ' + str(path))
    if not any((home / 'bin' / name).is_file() for name in ('clover.exe', 'clover.cmd')):
        raise ValueError('installer command launcher missing')
    if git(source, 'rev-parse', 'HEAD') != caller or git(source, 'rev-parse', 'HEAD^{tree}') != provenance['caller_tree']:
        raise ValueError('installed caller identity mismatch')
    if git(source, 'diff', '--name-only', 'HEAD'):
        raise ValueError('released installer changed tracked source; preserve failure, never restore it')
    run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
         "[ordered]@{home=[Environment]::GetEnvironmentVariable('CLOVER_HOME','User');"
         "path=[Environment]::GetEnvironmentVariable('Path','User')} | ConvertTo-Json -Compress"],
        'released-installer-registration', timeout=30)
    registration = frames(evidence / 'released-installer-registration.log')
    write_json(evidence / 'released-installer-registration.json', registration)
    if (len(registration) != 1 or Path(registration[0]['home']).resolve() != home.resolve()
            or home / 'bin' not in [Path(p.strip('"')) for p in registration[0]['path'].split(';') if p]):
        raise ValueError('installer did not persist the default home/command registration')
    write_json(evidence / 'released-installer-complete.json',
               dict(**provenance, home=str(home), source=str(source), live=str(source / 'venv'),
                    marker=marker, stages=list(STAGES), configure='SkipSetup/NonInteractive; fixture config follows',
                    gateway='NonInteractive; supported CLI registration follows', uv_download_certified=False))
    return provenance
