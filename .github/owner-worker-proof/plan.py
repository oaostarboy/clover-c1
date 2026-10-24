"""Validate immutable pins and enumerate a bounded Windows acceptance matrix."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SHA = re.compile(r'[0-9a-f]{40}')
CALLERS = {
    '1.5.6-suni': '9535350bee1d57cf7e466672e6f416403886bec1',
    '1.5.8': '0c8f3cd0e0ba6caca6948e13f9de92ba519c2029',
    '1.5.9': '3b54f413376eb99f8b83bfd4a0a340d01825014d',
}
HELPERS = '93cf13e9f66be56428993402c1e80422d9005d85'


def plan(pins):
    assert pins['mode'] in ['baseline', 'native-safety', 'candidate']
    assert SHA.fullmatch(pins['target'])
    assert pins['historical_helpers'] == HELPERS
    assert pins['old_callers'] == CALLERS
    if pins['mode'] in ['candidate', 'native-safety']:
        assert SHA.fullmatch(pins['target_tree'])
        assert pins['target'] not in CALLERS.values(), 'released baseline is not a repaired candidate'
    scope = pins.get('scope', 'full')
    assert scope in ['full', 'troy-verifier-correction']
    setups = ['zyra', 'troy']
    if scope == 'troy-verifier-correction':
        # Do not duplicate still-running Startup chat rows. This is the failed
        # task cohort only, NEVER a full or future-combined-source clearance.
        assert pins['target'] == 'bdad7a7b6b6698f347e06fd305a2bc6c3dc9055e'
        setups = ['troy']
    rows = []
    for name, old in {**CALLERS, 'candidate': pins['target']}.items():
        for setup in setups:
            for entry in ['terminal', 'gateway']:
                rows.append(dict(caller=name, old=old, setup=setup, entry=entry,
                    turn_mode='inflight' if name == 'candidate' else 'completed'))
    expected = 4 * len(setups) * 2
    assert len(rows) == expected
    assert len({(r['caller'], r['setup'], r['entry']) for r in rows}) == expected
    return dict(mode=pins['mode'], scope=scope,
                full_matrix=scope == 'full' and pins['mode'] == 'candidate', target=pins['target'],
                target_tree=pins.get('target_tree', ''),
                historical_helpers=HELPERS, matrix={'include': rows},
                not_certified=['live Telegram', 'busy frozen Windows first stop',
                               'frozen pre-pull refusal repaired by target code'])


if __name__ == '__main__':
    result = plan(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))
    target = Path(sys.argv[2])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    if 'GITHUB_OUTPUT' in os.environ:
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as out:
            for key in ['mode', 'target', 'target_tree', 'historical_helpers']:
                print(f'{key}={result[key]}', file=out)
            print('matrix=' + json.dumps(result['matrix'], separators=(',', ':')), file=out)
    print(json.dumps(result, indent=2))
