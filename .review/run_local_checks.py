"""Local-only canonical portable checks. No native dispatch or Windows account use."""
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.review' / 'canonical'
OUT.mkdir(exist_ok=True)
PRODUCT = ROOT / '.venv' / '.proof' / 'product-candidate'
PYTHON = ROOT / '.venv' / 'bin' / 'python'
PWSH = ROOT / '.venv' / '.tools' / 'powershell' / 'pwsh'
ENV = dict(os.environ, PATH=str(PWSH.parent) + os.pathsep + os.environ['PATH'],
           CLOVER_PYTHON=str(PYTHON), TMPDIR=str(ROOT / '.venv' / '.proof' / 'tmp'))
EXPECTED_SHA = '1a606496182ef48eafd78889eea8878165b829ae'
EXPECTED_TREE = '43825538944a3d836e8a28ad509f39b0eca7710e'
for ref, expected in [('HEAD', EXPECTED_SHA), ('HEAD^{tree}', EXPECTED_TREE)]:
    assert subprocess.check_output(['git', '-C', str(PRODUCT), 'rev-parse', ref], text=True).strip() == expected
subprocess.run(['git', '-C', str(PRODUCT), 'diff', '--exit-code'], check=True, capture_output=True)
files = [
    (ROOT, '.github/owner-worker-proof/test_bootstrap_resolution.py', 11),
    (ROOT, '.github/owner-worker-proof/test_contract.py', 20),
    (ROOT, '.github/owner-worker-proof/test_disposable_account.py', 1),
    (ROOT, '.github/owner-worker-proof/test_restricted_token.py', 6),
    (ROOT, '.github/owner-worker-proof/test_token_observation.py', 6),
    (PRODUCT, 'tests/clover_cli/test_update_delivery_guard.py', 19),
    (PRODUCT, 'tests/clover_cli/test_update_delivery_guard_wrappers.py', 26),
    (PRODUCT, 'tests/gateway/test_status_lock_security.py', 17),
    (PRODUCT, 'tests/gateway/test_status_update_owner.py', 23),
]
results = []
for repo, path, count in files:
    label = ('product-' if repo == PRODUCT else 'fixture-') + Path(path).stem
    log, xml = OUT / (label + '.log'), OUT / (label + '.xml')
    command = ['bash', 'scripts/run_tests.sh', path, '-j', '1', '--file-retries', '0', '--junitxml=' + str(xml)]
    with log.open('w') as stream:
        result = subprocess.run(command, cwd=repo, env=ENV, stdout=stream, stderr=subprocess.STDOUT)
    assert result.returncode == 0, log
    tree = ET.parse(xml)
    cases = tree.findall('.//testcase')
    assert len(cases) == count, (path, len(cases), count)
    assert not tree.findall('.//skipped'), path
    assert not tree.findall('.//failure') and not tree.findall('.//error'), path
    results.append(dict(file=path, tests=len(cases), failures=0, errors=0, skipped=0,
                        command=command, cwd=str(repo), log=str(log.relative_to(ROOT)), xml=str(xml.relative_to(ROOT))))
report = dict(product_sha=EXPECTED_SHA, product_tree=EXPECTED_TREE,
              fixture_tests=sum(x['tests'] for x in results if x['file'].startswith('.github/')),
              product_tests=sum(x['tests'] for x in results if x['file'].startswith('tests/')),
              skipped=0, failures=0, native_runs=0, update_cases=0, cases=results)
(OUT / 'verified.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({key: value for key, value in report.items() if key != 'cases'}, indent=2))
