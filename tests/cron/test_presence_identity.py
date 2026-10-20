import os
import subprocess
import sys

from cron.presence import ThoughtStore


def test_shared_generation_survives_other_process_and_store_instance(monkeypatch, tmp_path):
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    store = ThoughtStore()
    assert store.generation() == 0
    assert store.advance_generation() == 1
    assert ThoughtStore().generation() == 1
    child = subprocess.run([sys.executable, '-c',
        'from cron.presence import ThoughtStore; print(ThoughtStore().advance_generation())'],
        env=dict(os.environ, CLOVER_HOME=str(tmp_path)), capture_output=True, text=True, check=True)
    assert child.stdout.strip() == '2'
    assert store.generation() == 2


def test_two_process_claim_winner_survives_crash_boundary_without_replay(monkeypatch, tmp_path):
    import json
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    store = ThoughtStore()
    generation = store.advance_generation()
    assert store.reserve_assessment(event_id='first-event', thought_id='carry-1', revision=1,
        day='2026-01-01', max_calls=3, tokens=128, generation=generation)
    assert store.propose('carry-1', 1, 'first-event', 0, {}, generation=generation)
    code = """import json,sys
from cron.presence import ThoughtStore
s=ThoughtStore()
print('ready',flush=True)
sys.stdin.readline()
won=s.claim('carry-1',1,sys.argv[1],0,1)
attempted=s.attempt('carry-1',1,sys.argv[1],0,1) if won else False
print(json.dumps({'won':won,'attempted':attempted}),flush=True)
# Process ends after durable attempt, before any ACK; do not recover by replay.
"""
    env = dict(os.environ, CLOVER_HOME=str(tmp_path))
    children = [subprocess.Popen([sys.executable, '-c', code, owner], env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for owner in ('synthetic-owner-a','synthetic-owner-b')]
    try:
        for child in children:
            assert child.stdout.readline().strip() == 'ready'
        for child in children:
            child.stdin.write('go\n')
            child.stdin.flush()
        receipts = []
        for child in children:
            output, error = child.communicate(timeout=15)
            assert child.returncode == 0, error
            receipts.append(json.loads(output.strip()))
        assert sum(r['won'] for r in receipts) == 1
        assert sum(r['attempted'] for r in receipts) == 1
        assert store.publications()[0]['state'] == 'attempting'
        assert not ThoughtStore().claim('carry-1',1,'replacement-owner',0,1)
        assert not ThoughtStore().reserve_assessment(event_id='later-event', thought_id='carry-1',
            revision=1, day='2026-01-02', max_calls=3, tokens=128, generation=1)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
