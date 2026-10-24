"""Aggregate preserved native cases; never certify new source from old runs."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parent
OLD = 'bdad7a7b6b6698f347e06fd305a2bc6c3dc9055e'
LOCK = '27f507acb4f12c30d237471095f137007952c312'


def junit(path):
    cases = ET.parse(path).findall('.//testcase')
    failed = [c for c in cases if c.find('failure') is not None or c.find('error') is not None]
    skipped = [c for c in cases if c.find('skipped') is not None]
    return dict(cases=len(cases), passed=len(cases)-len(failed)-len(skipped),
                failures=[c.attrib for c in failed], skipped=[c.attrib for c in skipped])


def verify_row(run, item):
    directory = ROOT / run / 'candidate-artifacts' / f"owner-worker-{item['caller']}-{item['setup']}-{item['entry']}-{run}"
    result = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
    receipt = json.loads((directory / 'update-receipt.json').read_text(encoding='utf-8'))
    target = json.loads((directory / 'target.json').read_text(encoding='utf-8'))
    before = json.loads((directory / 'before-owner-worker.json').read_text(encoding='utf-8'))
    window = json.loads((directory / 'gateway-window.json').read_text(encoding='utf-8'))
    first, last = window[0], window[-1]
    assert result['status'] == 'PASS' and receipt['outcome'] == 'success'
    assert target['candidate'] == OLD
    assert receipt['pre_update']['sha'] == result['old']
    assert receipt['post_update']['sha'] == result['target'] == target['target']
    assert all(x['state']['pid'] == first['state']['pid'] and x['state']['start_time'] == first['state']['start_time'] for x in window)
    assert all(int(round(x['create_time'] * 100)) == x['state']['start_time'] for x in window)
    elapsed = last['observed'] - first['observed']
    assert elapsed >= 90
    assert len((directory / 'effects.txt').read_text(encoding='utf-8').splitlines()) == 1
    assert len((directory / 'received.txt').read_text(encoding='utf-8').splitlines()) == 1
    assert before['via_schedule'] == (item['setup'] == 'troy')
    assert all(x['supervisor']['schedule_status'] == 'running' for x in window)
    assert (directory / 'customization-preserved.json').is_file()
    assert (directory / 'state-after.json').is_file()
    assert result['driver_restart'] is False
    row = dict(caller=item['caller'], setup=item['setup'], entry=item['entry'],
               source=result['old'], target=result['target'], target_tree=target['target_tree'],
               result='PASS_DIAGNOSTIC_OLD_SOURCE', stable_seconds=elapsed,
               owner_pid=first['state']['pid'], owner_start=first['state']['start_time'],
               effect_count=1, reply_count=1, turn_mode=result['turn_mode'],
               via_schedule_before=before['via_schedule'], driver_restart=False,
               run=run, artifact=directory.name)
    if item['entry'] == 'gateway':
        notice = json.loads((directory / 'current-run-final-chat.json').read_text(encoding='utf-8'))
        assert len(notice['received']) == 1
        row['fresh_final_update_notices'] = 1
    return row


baseline = junit(ROOT / 'baseline/artifact/native-safety.xml')
safety = junit(ROOT / 'candidate-safety/native-safety.xml')
assert (ROOT / 'candidate-safety/source-sha.txt').read_text(encoding='utf-8-sig').strip() == OLD
initial = json.loads((ROOT / '40049740582/FINAL-NATIVE.json').read_text(encoding='utf-8'))
correction = json.loads((ROOT / '40050690083/FINAL-NATIVE.json').read_text(encoding='utf-8'))
assert initial['accepted_update_rows'] == 8 and initial['conclusion'] == 'failure'
assert correction['accepted_update_rows'] == 8 and correction['conclusion'] == 'success'
assert correction['plan']['full_matrix'] is False
rows = []
for run, data in [('40049740582', initial), ('40050690083', correction)]:
    for item in data['updates']:
        if item['accepted']:
            rows.append(verify_row(run, item))
keys = {(r['caller'], r['setup'], r['entry']) for r in rows}
assert len(rows) == len(keys) == 16
assert {r['setup'] for r in rows} == {'zyra', 'troy'}
security = json.loads((ROOT / '40052775499/NATIVE-SECURITY-RESULT.json').read_text(encoding='utf-8'))
assert security['product_sha'] == LOCK and not security['failed']
assert len(security['native_security_cases']) == 7
security_root = ROOT / '40052775499'
xmls = list(security_root.rglob('native-safety.xml'))
sources = list(security_root.rglob('source-sha.txt'))
archives = list(security_root.glob('*.zip'))
assert len(xmls) == len(sources) == len(archives) == 1
assert sources[0].read_text(encoding='utf-8-sig').strip() == LOCK
native_security = junit(xmls[0])
assert native_security['passed'] == security['passed'] and not native_security['failures']
assert len(native_security['skipped']) == len(security['skipped'])
security_names = {c['name'] for c in security['native_security_cases']}
selected = [c for c in ET.parse(xmls[0]).findall('.//testcase') if c.attrib['name'] in security_names]
assert len(selected) == len(security_names) == 7
assert all(c.find('skipped') is None and c.find('failure') is None and c.find('error') is None for c in selected)
assert hashlib.sha256(archives[0].read_bytes()).hexdigest() == security['raw_logs_sha256']
with zipfile.ZipFile(archives[0]) as archive:
    assert archive.testzip() is None
result = dict(recorded_at=datetime.now(timezone.utc).isoformat(),
    old_diagnostic_product=OLD, required_new_lock_source=LOCK,
    baseline=baseline, first_candidate_native_safety=safety,
    initial_full_gate=dict(run='40049740582', workflow_sha=initial['workflow_sha'],
        success=8, failed_troy_observer=8, acceptance_passed=False),
    corrected_task_cohort=dict(run='40050690083', workflow_sha=correction['workflow_sha'],
        success=8, partial_scope=True, acceptance_passed=False),
    verified_old_source_diagnostic_cases=rows,
    not_one_green_full_gate='Cases come from two fixture revisions; do not certify the combined product from this aggregate.',
    security_test_introduction_commit='f93170470f60c8fbbd6b67167d9daebb53f1ee6b',
    native_security_result=security,
    security_native_executed_on_reviewed_27f507=True,
    final_combined_source=None, final_combined_acceptance=False,
    pending_combined_cases=[dict(caller=r['caller'], setup=r['setup'], entry=r['entry'],
        result='NOT_EXERCISED_FINAL_COMBINED_SOURCE') for r in sorted(rows, key=lambda r:(r['caller'], r['setup'], r['entry']))],
    staged_native_old_call_seams='16 original-call/handler seams; NOT NATIVE-EXERCISED yet',
    staged_bootstrap_and_scm_update_corrections='NOT NATIVE-EXERCISED yet',
    ordinary_ci_recheck='40052777927 remains RED; local fixture corrections verified, hosted recheck pending',
    pending=['parent exact combined SHA/tree including27f507 and the Mac guard repair',
             'one final full16-row update plus native-safety gate on that exact combined source',
             'parent fixture review before ci-reviewed'],
    not_claimed=['Windows ACL/cross-UID isolation', 'live Telegram',
                 'busy frozen Windows first-stop protection', 'release/publication/live activation'])
(ROOT / 'DIAGNOSTIC-LEDGER.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print(json.dumps(dict(verified_old_source_diagnostic_cases=len(rows),
    startup=sum(r['setup']=='zyra' for r in rows), task=sum(r['setup']=='troy' for r in rows),
    minimum_stable_seconds=min(r['stable_seconds'] for r in rows),
    baseline_cases=baseline['cases'], baseline_passed=baseline['passed'], baseline_failed=len(baseline['failures']),
    native_candidate_passed=safety['passed'], native_candidate_skipped=len(safety['skipped']),
    final_combined_acceptance=False), indent=2))
