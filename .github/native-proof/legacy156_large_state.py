"""Hosted-only large-store first-start and frozen 1.5.6 rollback probes.

Fault-stop/rollback is a SEPARATE phase after ordinary update acceptance. It is
not driver recovery of a failed happy-path row. No real credentials or homes.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
import time
from contextlib import closing


def guard(home):
    assert os.environ.get('GITHUB_ACTIONS') == 'true', 'hosted disposable runner only'
    assert Path(home).resolve() == Path(os.environ['CLOVER_HOME']).resolve()


def dump(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n')


def grow(home, evidence, size=1_880_000_000):
    guard(home)
    db = Path(home)/'state.db'
    db.parent.mkdir(parents=True, exist_ok=True)
    began = time.monotonic()
    with closing(sqlite3.connect(db)) as conn:
        # This is before the initial gateway start, with no concurrent openers.
        assert conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
        conn.execute('CREATE TABLE IF NOT EXISTS native_startup_padding(payload BLOB)')
        conn.commit()
        while db.stat().st_size < size:
            conn.execute('INSERT INTO native_startup_padding VALUES(zeroblob(?))',
                         (min(20_000_000, size-db.stat().st_size),))
            conn.commit()
        assert conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    result = dict(bytes=db.stat().st_size, minimum_bytes=size, journal='delete',
                  prepare_seconds=time.monotonic()-began, pre_initial_gateway_start=True)
    dump(Path(evidence)/'large-state-prepared.json', result)
    return result


def watch(home, evidence, target, timeout=3600):
    guard(home)
    import psutil
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        try:
            state=json.loads((Path(home)/'gateway_state.json').read_text())
            if state.get('code_sha') != target or state.get('gateway_state') != 'running':
                time.sleep(.25)
                continue
            process=psutil.Process(state['pid'])
            listeners=[c for c in process.net_connections(kind='inet')
                       if c.status==psutil.CONN_LISTEN and c.laddr.port==18791]
            if not listeners:
                time.sleep(.25)
                continue
            observed=time.time()
            age=observed-process.create_time()
            result=dict(target=target, pid=process.pid, create_time=process.create_time(),
                        first_healthy_observed=observed, first_start_upper_bound_seconds=age,
                        state=state, listener_owned_by_pid=True,
                        db_bytes=(Path(home)/'state.db').stat().st_size,
                        old_watcher_timeout_seconds=90, old_stability_seconds=20)
            dump(Path(evidence)/'large-state-first-health.json', result)
            assert 0 <= age < 90-20, result
            return
        except (FileNotFoundError, json.JSONDecodeError, psutil.NoSuchProcess):
            pass
        time.sleep(.25)
    raise TimeoutError('candidate first health not observed')


def rollback(source, home, evidence, python, old, target, supervisor):
    guard(home)
    source, home, evidence=Path(source), Path(home), Path(evidence)
    def git(*args):
        return subprocess.check_output(['git','-C',str(source),*args],text=True).strip()
    assert git('rev-parse','HEAD') == target
    state=json.loads((home/'gateway_state.json').read_text())
    assert state['code_sha']==target and state['active_agents']==0
    # Preserve newest committed rows and pending files independently of any old
    # pre-update snapshot. The watcher must NOT restore those older copies.
    def saved():
        with closing(sqlite3.connect((home/'state.db').as_uri()+'?mode=ro',uri=True)) as db:
            tables={r[0] for r in db.execute("select name from sqlite_master where type='table'")}
            rows={}
            for table in ['sessions','messages','tasks','async_delegations','delivery_obligations']:
                if table in tables:
                    rows[table]=[list(r) for r in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
        import hashlib
        pending={str(p.relative_to(home)):hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in (home/'pending_messages').glob('*.json')}
        return dict(rows=rows,pending_files=pending)
    from macos_external_env import write_state, verify_state
    newest=write_state('latest-before-fault')
    dump(evidence/'rollback-newest-product-record.json',newest)
    before=saved()
    dump(evidence/'rollback-newest-state-before.json',before)
    # Deliberate post-acceptance cold-start failure injection. This is not a
    # restart performed to rescue an unsuccessful ordinary updater row.
    with (evidence/'rollback-fault-stop.log').open('w') as log:
        subprocess.run([str(python),'-m','clover_cli.main','gateway','stop'],cwd=source,
                       stdout=log,stderr=subprocess.STDOUT,check=True,timeout=180)
    import psutil
    deadline=time.monotonic()+90
    while psutil.pid_exists(state['pid']):
        try:
            if psutil.Process(state['pid']).status()==psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        assert time.monotonic()<deadline, 'fault stop did not complete'
        time.sleep(.5)
    frozen=evidence/'frozen-156-watcher.py'
    frozen.write_text(git('show',old+':clover_cli/update_restart_watcher.py')+'\n')
    data=dict(repo=str(source),pre_pull_sha=old,venv_python=str(python),
              supervisor=supervisor if supervisor!='windows' else None,
              windows_services=[],gateway_argv=[str(python),'-m','clover_cli.main','gateway','run'],
              cwd=str(home),pre_update_snapshot_id=None)
    dump(evidence/'rollback-beacon-input.json',data)
    # A fresh process loads the actual released watcher, with its unmodified
    # 90-second failure probe and 20-second stable recovery health requirement.
    code=("import json,runpy,sys;from pathlib import Path;"
          "m=runpy.run_path(sys.argv[1],run_name='frozen_156_watcher');"
          "d=json.loads(Path(sys.argv[2]).read_text());"
          "r=m['verify_or_rollback'](d,Path(sys.argv[3])/'.clover-update-heartbeat.json');"
          "print('FROZEN_WATCHER_RESULT='+r);assert r=='rolled-back',r")
    with (evidence/'rollback-frozen-watcher.log').open('w') as log:
        subprocess.run([str(python),'-c',code,str(frozen),str(evidence/'rollback-beacon-input.json'),str(home)],
                       cwd=source,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=900)
    assert git('rev-parse','HEAD')==old
    after=saved()
    dump(evidence/'rollback-newest-state-after.json',after)
    # New recovery entries may be appended; existing committed data is never
    # replaced. Delivery bookkeeping may advance but must not lose obligations.
    for table, rows in before['rows'].items():
        if table!='messages':
            assert {r[0] for r in rows} <= {r[0] for r in after['rows'][table]}
        else:
            assert after['rows'][table][:len(rows)]==rows, table
    recovered=verify_state([newest])
    dump(evidence/'rollback-newest-record-after.json',recovered)
    with closing(sqlite3.connect(home/'state.db')) as db:
        assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    dump(evidence/'rollback-result.json',dict(status='PASS_FROZEN_156_FAULT_STOP_ROLLBACK',
        target=target,restored=old,newest_committed_rows_preserved=True,
        pending_content_retained_or_recovered=True,normal_update_pass_was_completed_first=True,
        failure_injection='post-acceptance gateway stop; real frozen health failure and rollback',
        corrupted_database_restore_tested=False))


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('mode',choices=['grow','watch','rollback'])
    for name in ['home','evidence','target','source','python','old','supervisor']:
        p.add_argument('--'+name)
    a=p.parse_args()
    if a.mode=='grow':grow(a.home,a.evidence)
    elif a.mode=='watch':watch(a.home,a.evidence,a.target)
    else:rollback(a.source,a.home,a.evidence,a.python,a.old,a.target,a.supervisor)
