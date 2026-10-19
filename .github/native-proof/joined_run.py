"""Source-pinned offline acceptance using canonical isolation, no retry.

Transport-only: never merged into product. Explicit partitions retain complete
collection receipts and unchanged 240-second per-file protection.
"""
import collections
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
SRC, DEST = ROOT / 'source', ROOT / 'evidence'
HELPERS = ROOT / 'helpers/.github/native-proof'
DEST.mkdir(exist_ok=True)
manifest = json.loads((HELPERS / 'presence-manifest.json').read_text())
def git(*args):
    return subprocess.check_output(['git', '-C', str(SRC), *args], text=True).strip()
sha, tree = git('rev-parse', 'HEAD'), git('rev-parse', 'HEAD^{tree}')
assert sha == manifest['source_sha'] and tree == manifest['tree']
python = os.environ.get('PROOF_PYTHON') or str(SRC / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python'))
env = {k: v for k, v in os.environ.items() if k not in ('VIRTUAL_ENV', 'PYTHONPATH', 'PYTHONHOME')}
env['HOME'] = str(HELPERS / 'home')
env['CLOVER_PYTHON'] = python
# Match nested bare-Python proof to the explicitly selected disposable venv.
if os.name == 'nt':
    env['PATH'] = str(Path(python).parent) + os.pathsep + env.get('PATH', '')
bash = str(Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/bin/bash.exe') if os.name == 'nt' else 'bash'
mode = env.get('MODE', 'acceptance')
slice_index, slices = int(env.get('SLICE_INDEX', '0')), int(env.get('SLICES', '1'))
files = manifest['files'][slice_index::slices]
if mode == 'preflight':
    files = manifest['preflight_files']
selected_files = files
receipt = dict(sha=sha, tree=tree, os=platform.platform(), mode=mode,
               slice_index=slice_index, slices=slices, python=python,
               selected_files=files, status_before=git('status', '--porcelain', '--untracked-files=no'),
               started=time.time(), invocations=[], files=[], issues=[])
def save():
    (DEST / 'receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
def invoke(file, label, extra):
    directory = DEST / label
    directory.mkdir()
    before = set(DEST.iterdir())
    cmd = [bash, 'scripts/run_tests.sh', file, '-j', '1', '--file-timeout', '240', '--file-retries', '0', '--', '-q', '-p', 'no:randomly', '--tb=short', '-ra', '-p', 'settlement_partition']
    cmd += ['-p', 'settlement_boundary_observer']
    cmd += extra
    start = time.monotonic()
    with (directory / 'canonical.log').open('w', encoding='utf-8') as log:
        proc = subprocess.run(cmd, cwd=SRC, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    for p in set(DEST.iterdir()) - before:
        if p.is_file():
            shutil.move(str(p), directory / p.name)
    collections_found = [json.loads(p.read_text()) for p in directory.glob('collection-*.json')]
    records = []
    for p in directory.glob('*.json'):
        data = json.loads(p.read_text())
        if 'reports' in data:
            records.append(data)
    counts = collections.Counter()
    for p in directory.glob('*.xml'):
        for case in ET.parse(p).iter('testcase'):
            counts['tests'] += 1
            for tag in ('failure', 'error', 'skipped'):
                counts[tag] += len(case.findall(tag))
    row = dict(file=file, label=label, command=cmd, exit_code=proc.returncode,
               seconds=time.monotonic()-start, collection=collections_found,
               records=[dict(file=r['file'], exitstatus=r.get('exitstatus'), collected=r.get('collected')) for r in records],
               xml_counts=dict(counts))
    receipt['invocations'].append(row)
    save()
    return row, records
save()
for index, file in enumerate(selected_files):
    prefix = f'file-{index:03d}'
    discovery, records = invoke(file, prefix+'-collection', ['--collect-only'])
    if discovery['exit_code'] not in (0, 1) or len(discovery['collection']) != 1 or len(records) != 1 or records[0].get('exitstatus') != 0:
        receipt['issues'].append(dict(file=file, kind='incomplete collection'))
        save()
        continue
    ids = discovery['collection'][0]['all_ids']
    assert ids and len(ids) == len(set(ids))
    size = 8 if os.name == 'nt' and file in manifest['partition_files'] else len(ids)
    ranges = [(start, min(start+size, len(ids))) for start in range(0, len(ids), size)]
    observed, failed, skipped = [], [], []
    expected = [node for a, b in ranges for node in ids[a:b]]
    for start, stop in ranges:
        row, records = invoke(file, prefix+f'-cases-{start}-{stop}', ['--case-start', str(start), '--case-stop', str(stop)])
        collection_ok = len(row['collection']) == 1 and row['collection'][0]['all_ids'] == ids and row['collection'][0]['selected_ids'] == ids[start:stop]
        phases = [p for r in records for p in r['reports']]
        actual = sorted({p['nodeid'] for p in phases if p.get('when') == 'call' or (p.get('when') == 'setup' and p.get('outcome') in ('failed', 'skipped'))})
        observed += actual
        failed += [dict(nodeid=p['nodeid'], when=p['when']) for p in phases if p.get('outcome') == 'failed']
        skipped += [dict(nodeid=p['nodeid'], reason=p.get('longrepr')) for p in phases if p.get('outcome') == 'skipped']
        if not collection_ok or len(records) != 1 or row['exit_code'] != 0 or records[0].get('exitstatus') != 0 or actual != sorted(ids[start:stop]) or row['xml_counts'].get('failure') or row['xml_counts'].get('error') or row['xml_counts'].get('tests') != len(actual):
            receipt['issues'].append(dict(file=file, label=row['label'], kind='failed/incomplete canonical execution'))
        save()
    receipt['files'].append(dict(file=file, all_ids=ids, expected_ids=expected, observed_ids=observed,
                                 missing_ids=sorted(set(expected)-set(observed)), duplicates=[n for n, count in collections.Counter(observed).items() if count > 1],
                                 failed_phases=failed, skipped=skipped))
    save()
receipt.update(ended=time.time(), status_after=git('status', '--porcelain', '--untracked-files=no'))
receipt['accepted'] = not (receipt['issues'] or receipt['status_before'] or receipt['status_after'] or len(receipt['files']) != len(files) or any(r['missing_ids'] or r['duplicates'] or r['failed_phases'] for r in receipt['files']))
receipt['exit_code'] = 0 if receipt['accepted'] else 1
save()
print(json.dumps({k: v for k, v in receipt.items() if k not in ('files', 'invocations')}))
sys.exit(receipt['exit_code'])
