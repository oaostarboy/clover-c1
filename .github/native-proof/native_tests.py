"""Run existing native feature contracts with canonical per-file isolation.

No providers/credentials, no live-system-guard exemptions, no automatic retry.
JUnit and raw exit codes are retained separately for every selected test file.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any
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
    if file == SUITES['report-only'][0]:
        command += ['-vv', '-o', 'faulthandler_timeout=30']
    return command


def run_report_only_batches(repo, evidence, test_env):
    """Original35 cases in disjoint parallel canonical calls, one300s deadline.

    Do not lengthen the old canonical file budget or the receipt deadline.
    Windows aggregate execution needs parallel, balanced cohorts, not a retry.
    """
    first = '(helper_prose or untrusted or zero_only or stale_report or failed_test)'
    second = '(skipped_test or empty_report or bad_report or report_totals or changed_artifact or changed_spec or changed_evidence or changed_revision or wrong_cwd or wrong_session or new_session or foreign_id)'
    expressions = ['not test_acceptance_fails_closed or ' + first,
                   'test_acceptance_fails_closed and ' + second,
                   'test_acceptance_fails_closed and not (' + first + ' or ' + second + ')']
    began = time.monotonic()
    deadline = began + 300

    def run_part(index, expression):
        xml = evidence / f'report-only-part-{index}.xml'
        log = evidence / f'report-only-part-{index}.log'
        command = suite_command(repo, SUITES['report-only'][0], expression, xml)
        remaining = deadline - time.monotonic()
        part = dict(part=index, command=command, junit=str(xml), log=str(log),
                    remaining_seconds=remaining, returncode=1)
        if remaining <= 0:
            part['validation_error'] = 'original shared300s budget expired before launch'
            return part
        started = time.monotonic()
        try:
            with log.open('w') as stream:
                proc = subprocess.run(command, cwd=repo, stdout=stream, stderr=subprocess.STDOUT,
                                      timeout=remaining, env=test_env)
            part['returncode'] = proc.returncode
            part['cases'] = validate_junit(xml)
        except (OSError, ET.ParseError, ValueError, subprocess.TimeoutExpired) as exc:
            part['returncode'] = 1
            part['validation_error'] = type(exc).__name__
        part['elapsed_seconds'] = time.monotonic() - started
        return part

    parts = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run_part, index, expression)
                   for index, expression in enumerate(expressions, 1)]
        for future in as_completed(futures):
            parts.append(future.result())
            (evidence / 'report-only-batches.json').write_text(json.dumps(parts, indent=2))
    parts.sort(key=lambda part: part['part'])
    result: dict[str, Any] = dict(label='report-only', parts=parts,
        commands=[part['command'] for part in parts], returncode=0,
        log=str(evidence / 'report-only.log'), junit=str(evidence / 'report-only.xml'))
    merged = ET.Element('testsuites')
    with (evidence / 'report-only.log').open('w') as stream:
        for part in parts:
            if part['returncode'] or 'validation_error' in part:
                result['returncode'] = 1
            log = Path(part['log'])
            if log.is_file():
                stream.write(log.read_text(encoding='utf-8', errors='replace'))
            try:
                root = ET.parse(part['junit']).getroot()
                for suite in ([root] if root.tag == 'testsuite' else root.findall('testsuite')):
                    merged.append(suite)
            except (OSError, ET.ParseError) as exc:
                result['validation_error'] = type(exc).__name__
                result['returncode'] = 1
    xml = evidence / 'report-only.xml'
    ET.ElementTree(merged).write(xml, encoding='utf-8', xml_declaration=True)
    try:
        result['cases'] = validate_junit(xml)
        cases = list(merged.iter('testcase'))
        ids = {(c.get('classname'), c.get('name')) for c in cases}
        if len(cases) != 35 or len(ids) != 35 or any(not all(i) for i in ids):
            raise ValueError('original35-case coverage is missing or duplicated')
    except (OSError, ET.ParseError, ValueError) as exc:
        result.setdefault('validation_error', str(exc))
        result['returncode'] = 1
    if time.monotonic() > deadline or len(parts) != len(expressions):
        result['returncode'] = 1
        result.setdefault('validation_error', 'original shared300s budget expired')
    result['elapsed_seconds'] = time.monotonic() - began
    return result


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
        if os.name == 'nt' and label == 'report-only':
            result = run_report_only_batches(args.repo, args.evidence, test_env)
            failed |= result['returncode'] != 0 or 'validation_error' in result
            results.append(result)
            (args.evidence / 'native-suites.json').write_text(json.dumps(results, indent=2))
            print(json.dumps(result), flush=True)
            continue
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
