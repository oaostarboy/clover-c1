"""Run existing native feature contracts with canonical per-file isolation.

No providers/credentials, no live-system-guard exemptions, no automatic retry.
JUnit and raw exit codes are retained separately for every selected test file.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
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
    results = []
    failed = False
    for label, (file, expression) in SUITES.items():
        if not (args.repo / file).is_file():
            raise FileNotFoundError(file)
        xml = (args.evidence / (label + '.xml')).resolve()
        command = ['bash', str(args.repo / 'scripts/run_tests.sh'), file, '-j', '1',
                   '--file-retries', '0', '-o', 'addopts=', '--junitxml', str(xml)]
        if expression:
            command += ['-k', expression]
        log = args.evidence / (label + '.log')
        with log.open('w') as stream:
            proc = subprocess.run(command, cwd=args.repo, stdout=stream, stderr=subprocess.STDOUT,
                                  timeout=600, env={**os.environ, 'CLOVER_TEST_FILE_RETRIES': '0'})
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
