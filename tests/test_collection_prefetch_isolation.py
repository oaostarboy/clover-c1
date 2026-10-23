"""Collection imports cannot start the unrelated release-status network worker."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_collection_isolates_update_prefetch_and_restores_runtime_entry():
    # Cold process: no server import may have consumed the side effect already.
    script = '''
import json, sys
import clover_cli.banner as banner
import tests.conftest as suite
calls = []
def prefetch():
    calls.append('started')
banner.prefetch_update_check = prefetch
hook = getattr(suite, 'pytest_collection', None)
collection = hook() if hook else None
if collection is not None:
    next(collection)
try:
    import tui_gateway.server
finally:
    if collection is not None:
        try:
            next(collection)
        except StopIteration:
            pass
print(json.dumps({'collection_calls': calls, 'restored': banner.prefetch_update_check is prefetch}), file=sys.__stdout__, flush=True)
'''
    result = subprocess.run(
        [sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ), stdin=subprocess.DEVNULL, capture_output=True,
        text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        'collection_calls': [], 'restored': True,
    }
