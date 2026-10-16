"""Bounded native source A/B, NOT a replacement release acceptance gate.

One real Windows fresh-install fixture/interpreter/dependency set, two exact
source checkouts, three cold selectors per source, then the same selectors in
three independent concurrent processes. No retries or enlarged test deadlines.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from native_tests import canonical_shell, dependency_command

BASE = '32d3e6728c71d1f7c8684b7b41ac1785cb8117a9'
BASE_TREE = 'a600da1247dd260998dda62f865ff988cdf9eb42'
HEAD = '99ed285901fd0bce6548d6ea3436984b5ead7e27'
HEAD_TREE = '91ca2199b3b7ed3820a25a9a24915ed31f95c01e'
FILE = 'tests/gateway/test_workstream_acceptance.py'
SELECTORS = [
    'test_real_owner_checks_remain_unverified_supporting_evidence',
    'test_acceptance_fails_closed and skipped_test',
    'test_acceptance_fails_closed and foreign_scope',
]


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def source_identity(repo, sha, tree):
    actual = dict(path=str(repo), sha=git(repo, 'rev-parse', 'HEAD'),
                  tree=git(repo, 'rev-parse', 'HEAD^{tree}'),
                  tracked_diff=git(repo, 'status', '--porcelain', '--untracked-files=no'),
                  replacement_refs=git(repo, 'for-each-ref', '--format=%(refname)', 'refs/replace/'))
    assert actual['sha'] == sha and actual['tree'] == tree, actual
    assert not actual['tracked_diff'] and not actual['replacement_refs'], actual
    return actual


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    args = p.parse_args()
    if os.name != 'nt' or os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise SystemExit('Diagnostic requires a disposable native hosted Windows runner')
    repo = args.repo.resolve()
    evidence = args.evidence.resolve() / 'sqlite-ab'
    evidence.mkdir(parents=True, exist_ok=False)
    subprocess.run(dependency_command(repo, sys.executable, os.name), check=True)
    source_identity(repo, HEAD, HEAD_TREE)
    baseline = evidence.parent.parent / 'sqlite-baseline-source'
    subprocess.run(['git', 'clone', '--no-hardlinks', '--no-checkout', str(repo), str(baseline)], check=True)
    subprocess.run(['git', '-C', str(baseline), 'checkout', '--detach', BASE], check=True)
    sources = {'baseline': (baseline, BASE, BASE_TREE), 'candidate': (repo, HEAD, HEAD_TREE)}
    plugin = Path(__file__).with_name('windows_sqlite_diagnostic.py')
    plugin_sha = hashlib.sha256(plugin.read_bytes()).hexdigest()
    manifest = dict(schema='windows-sqlite-native-ab/v1', diagnostic_only=True,
        product_changes=[], selectors=SELECTORS, conditions=['serial-cold', 'parallel-three-cold'],
        attempts_per_condition=1, retries=0, turn_deadline_unchanged_seconds=20,
        shared_fixture_installed_candidate=HEAD,
        baseline_install_claim=False,
        baseline_boundary='Exact baseline source replay in candidate fresh-installed interpreter/dependencies, not separate installer acceptance',
        python=sys.version, executable=sys.executable, sqlite=sqlite3.sqlite_version,
        platform=platform.platform(), host=platform.node(), plugin_sha256=plugin_sha,
        sources={label:source_identity(*data) for label,data in sources.items()}, results=[])
    (evidence / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    for source, _, _ in sources.values():
        target = source / 'windows_sqlite_diagnostic.py'
        if target.exists():
            raise RuntimeError('Refuse to overwrite diagnostic plugin destination')
        shutil.copyfile(plugin, target)

    def run(label, condition, index):
        source, sha, tree = sources[label]
        name = f'{condition}-{label}-{index+1}'
        case_dir = evidence / name
        case_dir.mkdir()
        home = case_dir / 'os-home'
        home.mkdir()
        clover = home / '.clover'
        clover.mkdir()
        xml = case_dir / 'junit.xml'
        command = [canonical_shell(), (source / 'scripts/run_tests.sh').as_posix(), FILE,
            '-j', '1', '--file-retries', '0', '-o', 'addopts=',
            '-p', 'windows_sqlite_diagnostic', '--sqlite-diag-output=' + (case_dir / 'sqlite.jsonl').as_posix(),
            '-k', SELECTORS[index],
            '--junitxml=' + xml.as_posix(), '-vv', '-s', '-o', 'faulthandler_timeout=30']
        env = {**os.environ, 'HOME': str(home), 'USERPROFILE': str(home),
               'CLOVER_HOME': str(clover), 'CLOVER_PYTHON': Path(sys.executable).as_posix(),
               'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')}
        result = dict(label=label, condition=condition, selector=SELECTORS[index], command=command,
            source_sha=sha, source_tree=tree, home=str(home), python=sys.executable,
            before=source_identity(source, sha, tree))
        began = time.monotonic()
        try:
            with (case_dir / 'canonical.log').open('w', encoding='utf-8') as out:
                proc = subprocess.run(command, cwd=source, env=env, stdout=out,
                                      stderr=subprocess.STDOUT, timeout=360)
            result['returncode'] = proc.returncode
        except subprocess.TimeoutExpired:
            result['returncode'] = None
            result['terminal'] = 'outer diagnostic bound expired; no retry'
        result['elapsed'] = time.monotonic()-began
        if xml.exists():
            cases = ET.parse(xml).getroot().findall('.//testcase')
            result['cases'] = [dict(classname=c.get('classname'), name=c.get('name'),
                time=c.get('time'), outcome=next((t for t in ('failure','error','skipped') if c.find(t) is not None), 'passed')) for c in cases]
            result['exactly_one_case'] = len(cases) == 1
        else:
            result['cases'] = []
            result['exactly_one_case'] = False
        result['after'] = source_identity(source, sha, tree)
        (case_dir / 'result.json').write_text(json.dumps(result, indent=2))
        print('AB_RESULT ' + json.dumps(result), flush=True)
        return result

    try:
        # Alternate baseline/candidate for serial cases to limit ordering bias.
        for index in range(3):
            for label in ('baseline', 'candidate'):
                manifest['results'].append(run(label, 'serial-cold', index))
                (evidence / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        # Reproduce the historical three-process fixture concurrency, not retries.
        for label in ('baseline', 'candidate'):
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda i: run(label, 'parallel-three-cold', i), range(3)))
            manifest['results'].extend(results)
            (evidence / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    finally:
        for source, _, _ in sources.values():
            target = source / 'windows_sqlite_diagnostic.py'
            if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == plugin_sha:
                target.unlink()
        manifest['final_sources'] = {label:source_identity(*data) for label,data in sources.items()}
        (evidence / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    # Diagnostic completion is not acceptance; preserve all red cases honestly.
    assert len(manifest['results']) == 12
    raise SystemExit(0 if all(r['returncode'] == 0 and r['exactly_one_case'] and
                            all(c['outcome'] == 'passed' for c in r['cases'])
                            for r in manifest['results']) else 1)


if __name__ == '__main__':
    main()
