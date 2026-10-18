"""Ingress weakref cleanup must remain safe during optional policy allocation."""
import os
import subprocess
import sys


def test_policy_gc_can_release_an_older_ingress_without_deadlock(tmp_path):
    script = r'''
import gc
from types import SimpleNamespace
from agent import workstream_origin as origin
from agent import presence_lifecycle
class Event:
    internal = False
    source = SimpleNamespace(chat_type='dm')
gc.disable()
old = Event()
old.cycle = old
assert origin.mint_human_ingress(old, platform='synthetic')
old_key = id(old)
del old
def policy(profile):
    gc.collect()
    assert old_key not in origin._ISSUED
    return -1
presence_lifecycle.note_human_input = policy
new = Event()
assert origin.mint_human_ingress(new, platform='telegram')
assert origin._ISSUED[id(new)].presence_generation == -1
assert not origin.mint_human_ingress(new, platform='telegram')
'''
    env = {**os.environ, 'CLOVER_HOME': str(tmp_path), 'HOME': str(tmp_path)}
    result = subprocess.run([sys.executable, '-c', script], env=env,
                            text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
