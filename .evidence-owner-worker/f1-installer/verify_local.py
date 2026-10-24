"""Record local fixture-only checks; never execute a Windows installer here."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
FIXTURE = ROOT / '.github/owner-worker-proof'
BASE = '8883828f78049272e7c98b590e142a8ec56d8359'
TARGET = '1a606496182ef48eafd78889eea8878165b829ae'
TREE = '43825538944a3d836e8a28ad509f39b0eca7710e'


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args], text=True).strip()


pins = json.loads((FIXTURE / 'pins.json').read_text(encoding='utf-8'))
assert (pins['mode'], pins['scope'], pins['target'], pins['target_tree']) == ('candidate', 'full', TARGET, TREE)
assert json.loads((OUT / 'pr150-readback.json').read_text(encoding='utf-8'))['headRefOid'] == TARGET
assert git('rev-parse', TARGET + '^{tree}') == TREE
subprocess.run(['git', '-C', str(ROOT), 'merge-base', '--is-ancestor',
                '27f507acb4f12c30d237471095f137007952c312', TARGET], check=True)
plan = json.loads((OUT / 'plan.json').read_text(encoding='utf-8'))
rows = plan['matrix']['include']
assert len(rows) == len({(r['caller'], r['setup'], r['entry']) for r in rows}) == 16
junit = ET.parse(OUT / 'portable.xml').getroot()
suites = list(junit.iter('testsuite'))
assert all(int(s.get(k, '0')) == 0 for s in suites for k in ('failures', 'errors', 'skipped'))
old = subprocess.check_output(['git', '-C', str(ROOT), 'show', BASE + ':.github/owner-worker-proof/windows_update.py']).decode()
new = (FIXTURE / 'windows_update.py').read_text(encoding='utf-8')
retained = ('native_guard', 'observe', 'wait_update_exit', 'wait_update_receipt', 'legacy_stop_witness')
def functions(text):
    return {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(text).body if isinstance(n, ast.FunctionDef)}
a, b = functions(old), functions(new)
assert all(a[name] == b[name] for name in retained)
original_workflow = subprocess.check_output(['git', '-C', str(ROOT), 'show', BASE + ':.github/workflows/windows-owner-worker-proof.yml']).decode()
workflow = (ROOT / '.github/workflows/windows-owner-worker-proof.yml').read_text(encoding='utf-8')
assert original_workflow.split('  native-safety:', 1)[1].split('  updates:', 1)[0] == workflow.split('  native-safety:', 1)[1].split('  updates:', 1)[0]
paths = ['README.md', 'pins.json', 'test_contract.py', 'windows_update.py', 'released_installer.py', 'register_startup.ps1']
files = {'.github/owner-worker-proof/' + p: hashlib.sha256((FIXTURE / p).read_bytes()).hexdigest() for p in paths}
files['.github/workflows/windows-owner-worker-proof.yml'] = hashlib.sha256(workflow.encode()).hexdigest()
result = dict(status='LOCAL_FIXTURE_CHECKS_PASS_NOT_NATIVE_CERTIFICATION', reviewed_base=BASE,
              product_target=TARGET, product_tree=TREE, fixture_branch=git('branch', '--show-current'),
              portable_tests=20, portable_subtests=21,
              junit_entries=sum(int(s.get('tests', '0')) for s in suites),
              junit_failures=0, junit_errors=0, junit_skips=0,
              matrix_rows=len(rows), retained_native_functions=list(retained),
              native_safety_job_byte_identical=True, native_negative_postpull_cases=16,
              validation=['pytest', 'compileall', 'scoped Ruff', 'git diff --check', 'ancestry',
                          'real local Git rewrites with file-only transport', 'released Git blob/hash/command contracts'],
              windows_installer_execution='NOT_EXERCISED: held for parent independent delta review',
              powershell_execution='NOT_EXERCISED on Linux; no pwsh present',
              bootstrap_uv_download='NOT_EXERCISED: intentionally preinstalled verified uv',
              final_gate_dispatched=False, pushed=False, ci_reviewed_added=False,
              preserved_fixture_controls='portable contract compares reviewed checkout bytes, including existing .ps1 CRLF attributes',
              changed_file_sha256=files)
(OUT / 'local-result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print(json.dumps({k: result[k] for k in ['status', 'portable_tests', 'portable_subtests', 'junit_entries', 'matrix_rows']}))
