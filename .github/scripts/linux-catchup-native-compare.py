"""Fail closed on collection errors or any new Windows regression."""
from pathlib import Path
import json
import re
import sys

def read_run(prefix):
    text = Path(prefix + '-results.txt').read_text(encoding='utf-8-sig')
    code = int(Path(prefix + '-exit.txt').read_text(encoding='utf-8-sig').strip())
    failures = sorted(set(re.findall(r'^FAILED\s+(\S+)', text, re.M)))
    errors = sorted(set(re.findall(r'^ERROR\s+(\S+)', text, re.M)))
    assert code in (0, 1), (prefix, code, 'runner/collection error')
    assert not errors, (prefix, errors)
    passed = re.search(r'\b(\d+) passed\b', text)
    assert passed and int(passed[1]) > 0, (prefix, 'no passed tests')
    count = re.search(r'\b(\d+) failed\b', text)
    assert len(failures) == (int(count[1]) if count else 0), (prefix, 'failure totals mismatch')
    return {'exit_code': code, 'passed': int(passed[1]), 'failures': failures}

base = read_run('baseline')
head = read_run('candidate')
new = sorted(set(head['failures']) - set(base['failures']))
report = {'platform': sys.platform, 'baseline': base, 'candidate': head, 'new_failures': new}
Path('native-comparison.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
assert sys.platform == 'win32', 'must execute natively on Windows'
assert not new, ('new native-Windows failures', new)
assert head['passed'] > base['passed'], 'candidate must exercise the added guard regressions'
