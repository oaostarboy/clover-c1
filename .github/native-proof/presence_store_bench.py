"""Same-source native read-fence A/B; not a latency acceptance threshold."""
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time

source, output = map(Path, sys.argv[1:3])
os.environ['CLOVER_HOME'] = str(output.parent / 'store-benchmark-home')
os.environ['CLOVER_TEST_ISOLATION'] = os.environ['CLOVER_HOME']
sys.path.insert(0, str(source))
from cron.presence import ThoughtStore
from agent.presence_state import ContextKey
from agent.workstreams import Scope

store = ThoughtStore()
context = ContextKey(Scope(str(store.path.parent), '/synthetic-project', 'private:1', '1'), 'synthetic-root')
store.advance_generation()
def separate():
    return store.linked_key(context), store.generation(), store.initiative_status()
def combined():
    return store.runtime_state(context)
assert separate() == combined()
samples = {'separate': [], 'combined': []}
for round_index in range(3):
    for name, operation in (('separate', separate), ('combined', combined)):
        start = time.perf_counter()
        for _ in range(25):
            assert operation() == (context, 1, (0, False))
        samples[name].append((time.perf_counter() - start) / 25)
result = {'platform': platform.platform(), 'python': sys.version,
          'module': __import__('cron.presence', fromlist=['x']).__file__,
          'seconds_per_observation': samples,
          'median_seconds': {name: statistics.median(values) for name, values in samples.items()},
          'boundary': 'same-source microbenchmark only; no callback acceptance claim'}
output.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result['median_seconds']))
