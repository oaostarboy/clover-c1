"""Explicit native RED/GREEN experiments; original RED receipts never rewritten."""
import collections
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
HELPERS = ROOT/'helpers/.github/native-proof'
SOURCE = ROOT/'source'
DEST = ROOT/'evidence'
DEST.mkdir(exist_ok=True)
variant = os.environ['CONTROL_VARIANT']
variants = json.loads((HELPERS/'deadline-ab-manifest.json').read_text())
selection = variants[variant]
manifest = dict(source_sha=selection['source_sha'], tree=selection['tree'],
                files=selection['files'], tracked_test_paths=selection['files'],
                partition_files=[], file_timeout=240, file_retries=0)
(HELPERS/'presence-manifest.json').write_text(json.dumps(manifest, indent=2))
(DEST/'frozen-experiment-selection.json').write_text(json.dumps(dict(variant=variant, **selection), indent=2))
proc = subprocess.run([sys.executable, str(HELPERS/'joined_run.py')], stdin=subprocess.DEVNULL)
receipt = json.loads((DEST/'receipt.json').read_text())
by = collections.defaultdict(list)
xml = []
for folder in DEST.glob('file-*-cases-*'):
    for path in folder.glob('*.json'):
        data = json.loads(path.read_text())
        if isinstance(data, dict) and 'reports' in data:
            for report in data['reports']:
                by[report['nodeid']].append(report)
    for path in folder.glob('*.xml'):
        for case in ET.parse(path).iter('testcase'):
            outcome = ('failed' if case.find('failure') is not None or case.find('error') is not None
                       else 'skipped' if case.find('skipped') is not None else 'passed')
            xml.append((case.attrib['classname'], case.attrib['name'], outcome))
json_cases = []
failed = []
for node, reports in by.items():
    outcome = ('failed' if any(r['outcome']=='failed' for r in reports)
               else 'skipped' if any(r['outcome']=='skipped' for r in reports) else 'passed')
    if outcome == 'failed':
        failed.append(node)
    parts = node.split('::')
    classname = parts[0][:-3].replace('/', '.') + ''.join('.'+p for p in parts[1:-1])
    json_cases.append((classname, parts[-1], outcome))
expected = selection['expected_failed_ids']
issues = []
if receipt['sha'] != selection['source_sha'] or receipt['tree'] != selection['tree']:
    issues.append('source identity mismatch')
if receipt['status_before'] or receipt['status_after']:
    issues.append('tracked source mutation')
if sorted(failed) != sorted(expected):
    issues.append('unexpected failed case identity')
if collections.Counter(json_cases) != collections.Counter(xml):
    issues.append('XML/JSON identity or outcome mismatch')
if len(json_cases) != selection['expected_case_count'] or len(xml) != selection['expected_case_count']:
    issues.append('case count mismatch')
if any(count != 1 for count in collections.Counter(json_cases).values()):
    issues.append('duplicate case')
if any(r['when'] in ('setup','teardown','collection') and r['outcome'] != 'passed'
       for reports in by.values() for r in reports):
    issues.append('nonpassing setup/teardown/collection')
if any(x[2]=='skipped' for x in json_cases):
    issues.append('unexpected skip')
if any(f['missing_ids'] or f['duplicates'] for f in receipt['files']):
    issues.append('missing or duplicate selection')
if expected:
    if proc.returncode != 1 or receipt['accepted'] is not False:
        issues.append('RED control did not retain failed canonical receipt')
    for reports in by.values():
        for report in reports:
            if report['outcome'] == 'failed' and selection['failure_text'] not in (report.get('longrepr') or ''):
                issues.append('RED failed for wrong cause')
else:
    if proc.returncode != 0 or receipt['accepted'] is not True or receipt['issues']:
        issues.append('GREEN candidate canonical execution not accepted')
result = dict(variant=variant, source=receipt['sha'], tree=receipt['tree'],
              expected_failed_ids=expected, actual_failed_ids=sorted(failed),
              original_canonical_exit_code=proc.returncode,
              original_canonical_accepted=receipt['accepted'],
              cases=len(json_cases), outcomes=dict(collections.Counter(c[2] for c in json_cases)),
              exact_xml_json_identity_and_outcomes=collections.Counter(json_cases)==collections.Counter(xml),
              experiment_matches_expected_contract=not issues, issues=issues,
              note='Expected RED is not source acceptance. No original RED is cleared by this experiment.')
(DEST/'ab-experiment-receipt.json').write_text(json.dumps(result, indent=2))
print(json.dumps(result))
sys.exit(1 if issues else 0)
