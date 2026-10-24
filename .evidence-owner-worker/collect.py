"""Read back this exact hosted proof; never turn absent/failed rows into passes."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

REPO = 'oaostarboy/clover-c1'
RUN = '40049337485'
WORKFLOW_SHA = '0cb58a1d8df2c2d6ec3114af48e383eaf40040d5'
PRODUCT_SHA = 'bdad7a7b6b6698f347e06fd305a2bc6c3dc9055e'
EXPECTED = {(caller, setup, entry)
            for caller in ['1.5.6-suni', '1.5.8', '1.5.9', 'candidate']
            for setup in ['zyra', 'troy'] for entry in ['terminal', 'gateway']}
ROOT = Path(__file__).resolve().parent


def gh(*args):
    return subprocess.check_output(['gh', *args], text=True)


def snapshot(final=False):
    run = json.loads(gh('run', 'view', RUN, '--repo', REPO,
                       '--json', 'status,conclusion,headSha,jobs,url'))
    assert run['headSha'] == WORKFLOW_SHA, run['headSha']
    (ROOT / 'candidate-run-readback.json').write_text(json.dumps(run, indent=2) + '\n')
    rows = []
    keys = set()
    for job in run['jobs']:
        match = re.fullmatch(r'Windows / (1\.5\.6-suni|1\.5\.8|1\.5\.9|candidate) / (zyra|troy) / (terminal|gateway)', job['name'])
        if not match:
            continue
        key = match.groups()
        assert key not in keys, 'duplicate row'
        keys.add(key)
        rows.append(dict(caller=key[0], setup=key[1], entry=key[2],
            job_id=job['databaseId'], job_status=job['status'], job_conclusion=job['conclusion']))
    assert keys == EXPECTED, dict(expected=len(EXPECTED), actual=len(keys), missing=sorted(EXPECTED-keys))
    result = dict(run=RUN, url=run['url'], workflow_sha=WORKFLOW_SHA, product_sha=PRODUCT_SHA,
                  status=run['status'], conclusion=run['conclusion'],
                  update_job_counts=dict(Counter(r['job_conclusion'] or r['job_status'] for r in rows)),
                  updates=sorted(rows, key=lambda x: (x['caller'], x['setup'], x['entry'])),
                  acceptance_passed=False, real_telegram=False, driver_restart=False)
    if final:
        assert run['status'] == 'completed', 'not terminal; do not collect a final result'
        metadata = json.loads(gh('api', f'repos/{REPO}/actions/runs/{RUN}/artifacts?per_page=100'))
        (ROOT / 'artifact-metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
        assert metadata['total_count'] == len(metadata['artifacts'])
        expected_names = {f'owner-worker-{c}-{s}-{e}-{RUN}' for c,s,e in EXPECTED}
        expected_names |= {f'owner-worker-plan-{RUN}', f'owner-worker-native-safety-{RUN}'}
        assert {a['name'] for a in metadata['artifacts']} == expected_names
        assert not any(a['expired'] for a in metadata['artifacts'])
        result['declared_artifacts'] = metadata['total_count']
        artifacts = ROOT / 'candidate-artifacts'
        if not artifacts.exists():
            subprocess.run(['gh', 'run', 'download', RUN, '--repo', REPO, '--dir', str(artifacts)], check=True)
        assert {p.name for p in artifacts.iterdir() if p.is_dir()} == expected_names
        source_plan = json.loads((artifacts / f'owner-worker-plan-{RUN}' / 'plan.json').read_text(encoding='utf-8'))
        assert source_plan['target'] == PRODUCT_SHA
        planned_keys = {(r['caller'], r['setup'], r['entry']) for r in source_plan['matrix']['include']}
        assert planned_keys == EXPECTED, 'artifact plan and collected rows differ'
        result['plan'] = source_plan
        for row in rows:
            name = f"owner-worker-{row['caller']}-{row['setup']}-{row['entry']}-{RUN}"
            directory = artifacts / name
            row['artifact_exists'] = directory.is_dir()
            row['artifact_files'] = len(list(directory.rglob('*'))) if directory.exists() else 0
            for file in ['result.json', 'failure.json', 'first-outcome.json', 'pins.json', 'before-owner-worker.json']:
                path = directory / file
                if path.exists():
                    row[file] = json.loads(path.read_text(encoding='utf-8'))
            if row.get('result.json', {}).get('status') == 'PASS':
                assert row['job_conclusion'] == 'success', row
            # A failed job can never be accepted because an intermediate result
            # exists. Missing final result remains unexercised/unknown.
            row['accepted'] = row['job_conclusion'] == 'success' and row.get('result.json', {}).get('status') == 'PASS'
        safety = artifacts / f'owner-worker-native-safety-{RUN}' / 'native-safety.xml'
        if safety.exists():
            cases = ET.parse(safety).findall('.//testcase')
            result['safety_junit'] = dict(cases=len(cases),
                failures=[c.attrib for c in cases if c.find('failure') is not None or c.find('error') is not None],
                skipped=[c.attrib for c in cases if c.find('skipped') is not None],
                passed=sum(c.find('failure') is None and c.find('error') is None and c.find('skipped') is None for c in cases))
        result['accepted_update_rows'] = sum(row['accepted'] for row in rows)
        result['acceptance_passed'] = (len(EXPECTED) == 16 and source_plan.get('full_matrix', True)
            and result['accepted_update_rows'] == len(EXPECTED) and run['conclusion'] == 'success')
        zip_path = ROOT / 'candidate-raw-logs.zip'
        if not zip_path.exists():
            with zip_path.open('wb') as output:
                completed = subprocess.run(['gh', 'api', '--allow-escape-sequences',
                    f'repos/{REPO}/actions/runs/{RUN}/logs'], stdout=output)
            assert completed.returncode == 0
        assert zip_path.stat().st_size > 0
        import zipfile
        with zipfile.ZipFile(zip_path) as archive:
            assert archive.testzip() is None
        result['raw_logs'] = dict(path=zip_path.name, bytes=zip_path.stat().st_size,
            sha256=hashlib.sha256(zip_path.read_bytes()).hexdigest())
    (ROOT / ('FINAL-NATIVE.json' if final else 'CURRENT-NATIVE.json')).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ['updates', 'plan']}, indent=2))
    for row in result['updates']:
        failure = row.get('failure.json', {}).get('error', '').splitlines()
        print(json.dumps(dict(caller=row['caller'], setup=row['setup'], entry=row['entry'],
            status=row['job_conclusion'] or row['job_status'],
            result=row.get('result.json', {}).get('status'),
            failure=failure[-1][:500] if failure else None)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--final', action='store_true')
    parser.add_argument('--run', default=RUN)
    parser.add_argument('--workflow-sha', default=WORKFLOW_SHA)
    parser.add_argument('--product-sha', default=PRODUCT_SHA)
    parser.add_argument('--scope', choices=['full', 'troy'], default='full')
    args = parser.parse_args()
    RUN, WORKFLOW_SHA, PRODUCT_SHA = args.run, args.workflow_sha, args.product_sha
    if args.scope == 'troy':
        EXPECTED = {key for key in EXPECTED if key[1] == 'troy'}
    ROOT = ROOT / RUN
    ROOT.mkdir(parents=True, exist_ok=True)
    snapshot(args.final)
