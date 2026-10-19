"""Exhaustive bounded case partitions, not skips or retries."""
import json
import os
from pathlib import Path
import pytest

DEST = Path(__file__).resolve().parents[5] / 'evidence'

def pytest_addoption(parser):
    parser.addoption('--case-start', type=int, default=0)
    parser.addoption('--case-stop', type=int, default=-1)

@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    all_ids = [item.nodeid for item in items]
    start = config.getoption('--case-start')
    stop = config.getoption('--case-stop')
    if stop < 0:
        stop = len(items)
    assert 0 <= start <= stop <= len(items)
    selected = items[start:stop]
    omitted = items[:start] + items[stop:]
    if omitted:
        config.hook.pytest_deselected(items=omitted)
    items[:] = selected
    (DEST / ('collection-'+str(os.getpid())+'.json')).write_text(json.dumps({
        'all_ids': all_ids, 'selected_ids': [item.nodeid for item in items],
        'start': start, 'stop': stop, 'collectonly': bool(config.option.collectonly),
    }, indent=2), encoding='utf-8')
