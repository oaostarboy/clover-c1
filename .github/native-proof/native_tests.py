"""Run existing native feature contracts with canonical per-file isolation.

No providers/credentials, no live-system-guard exemptions, no automatic retry.
JUnit and raw exit codes are retained separately for every selected test file.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

SUITES = {
    'sqlite': ('tests/cron/test_workstream_state.py', ''),
    'report-only': ('tests/gateway/test_workstream_acceptance.py', ''),
    'stop': ('tests/cron/test_workstream_dm_runtime.py', 'test_pending_stop_uses_real_agent_interrupt_and_never_executes'),
    'restart': ('tests/cron/test_workstream_resume.py', 'test_linked_monitor_occurrence_survives_restart_and_has_one_claim'),
}


def validate_junit(path):
    cases = ET.parse(path).getroot().findall('.//testcase')
    if not cases or any(c.find(tag) is not None for c in cases for tag in ['failure', 'error', 'skipped']):
        raise ValueError(f'empty, failed or skipped native proof: {path}')
    return len(cases)


def dependency_command(repo, python, platform):
    uv = repo.parent / 'bin' / ('uv.exe' if platform == 'nt' else 'uv')
    if not uv.is_file():
        raise FileNotFoundError(f'installer-managed uv missing: {uv}')
    return [str(uv), 'pip', 'install', '--python', python, '-e', str(repo) + '[dev]']


def canonical_shell():
    if os.name != 'nt':
        return 'bash'
    # Bare bash on a Windows runner resolves to the WSL shim. Use only the
    # Git-for-Windows installation already required by the native installer.
    git = shutil.which('git')
    if not git:
        raise FileNotFoundError('Git for Windows is required for the canonical runner')
    parent = Path(git).parent
    if parent.name.lower() not in {'cmd', 'bin'}:
        raise FileNotFoundError('Cannot identify the Git for Windows shell root')
    root = parent.parent
    if root.name.lower() in {'mingw32', 'mingw64', 'ucrt64'}:
        root = root.parent
    bash = root / 'bin' / 'bash.exe'
    if not bash.is_file():
        raise FileNotFoundError(f'Git Bash missing: {bash}')
    return str(bash)


def suite_command(repo, file, expression, xml):
    command = [canonical_shell(), (repo / 'scripts/run_tests.sh').as_posix(), file, '-j', '1',
               '--file-retries', '0', '-o', 'addopts=', '--junitxml=' + xml.as_posix()]
    if expression:
        command += ['-k', expression]
    return command


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    args = p.parse_args()
    args.repo = args.repo.resolve()
    args.evidence.mkdir(parents=True, exist_ok=True)
    # Install into THIS disposable interpreter only. The workflow invokes this
    # with the just-installed venv python, never a developer or live venv.
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise SystemExit('native installer suites require a disposable GitHub-hosted runner')
    subprocess.run(dependency_command(args.repo, sys.executable, os.name), check=True)
    # Test-only nested shell commands deliberately retain the production
    # python/python3 grammar. Resolve bare python to this disposable venv.
    test_env = {**os.environ, 'CLOVER_TEST_FILE_RETRIES': '0',
                'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')}
    suites = dict(SUITES)
    if os.name == 'nt':
        suites['windows-secure-reader'] = ('tests/agent/test_windows_audit_native.py', '')
        suites['windows-receipt-handoff'] = ('tests/clover_cli/test_windows_update_receipt_handoff.py', '')
        suites['windows-handoff-clock'] = ('tests/clover_cli/test_windows_handoff_clock_native.py', '')
        suites['windows-runner-env'] = ('tests/test_runner_windows_environment.py', '')
    results = []
    failed = False
    for label, (file, expression) in suites.items():
        if not (args.repo / file).is_file():
            raise FileNotFoundError(file)
        xml = (args.evidence / (label + '.xml')).resolve()
        command = suite_command(args.repo, file, expression, xml)
        log = args.evidence / (label + '.log')
        with log.open('w') as stream:
            proc = subprocess.run(command, cwd=args.repo, stdout=stream, stderr=subprocess.STDOUT,
                                  timeout=600, env=test_env)
        result = dict(label=label, command=command, returncode=proc.returncode, log=str(log), junit=str(xml))
        try:
            result['cases'] = validate_junit(xml)
        except (OSError, ET.ParseError, ValueError) as exc:
            result['validation_error'] = str(exc)
        failed |= proc.returncode != 0 or 'validation_error' in result
        results.append(result)
        (args.evidence / 'native-suites.json').write_text(json.dumps(results, indent=2))
        print(json.dumps(result), flush=True)
    raise SystemExit(1 if failed else 0)


if __name__ == '__main__':
    main()
