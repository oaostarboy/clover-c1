"""Native measurement only: no product import, network or deadline mutation."""
import json
from pathlib import Path
import platform
import time

rows = []
for index in range(12):
    mono, perf, cpu = time.monotonic(), time.perf_counter(), time.process_time()
    time.sleep(1.05)
    rows.append(dict(index=index, requested_sleep=1.05,
                     monotonic_elapsed=time.monotonic()-mono,
                     perf_counter_elapsed=time.perf_counter()-perf,
                     process_cpu_elapsed=time.process_time()-cpu))
result = dict(platform=platform.platform(), clocks={name:vars(time.get_clock_info(name))
              for name in ('monotonic','perf_counter','process_time')}, samples=rows)
Path('evidence').mkdir(exist_ok=True)
Path('evidence/clock-probe.json').write_text(json.dumps(result, indent=2))
print(json.dumps(result))
