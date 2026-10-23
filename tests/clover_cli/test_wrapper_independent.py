"""Independent admission counterexamples: all PIDs and OS reads deterministic."""
import json
from types import SimpleNamespace
import pytest

@pytest.fixture
def world(tmp_path, monkeypatch):
    import psutil
    from clover_cli import gateway, update_cmd, update_delivery_guard as guard
    import gateway.status as status
    home=tmp_path/'same-install/profile'
    home.mkdir(parents=True)
    child, parent=28002,28001
    py=str(tmp_path/'external environment/bin/python')
    cmd=[py,'-m','clover_cli.main','gateway','run','--external-supervisor']
    commands={child:cmd,parent:[py,'-m','clover_cli.stderr_timestamp','--error-log',str(home/'logs/gateway.error.log'),'--',*cmd]}
    starts={child:200,parent:190}
    parents={child:parent}
    processes=[SimpleNamespace(pid=child,path=home,profile='owned',create_time=200)]
    discovered=[parent,child]
    row=dict(pid=child,kind='clover-gateway',start_time=200,clover_home=str(home),update_restart_capability=guard.CAPABILITY)
    def save(): (home/'gateway_state.json').write_text(json.dumps(row),encoding='utf-8')
    save()
    def proc(pid):
        if pid not in commands: raise psutil.NoSuchProcess(pid)
        return SimpleNamespace(cmdline=lambda:list(commands[pid]),ppid=lambda:parents.get(pid,1),environ=lambda:{})
    monkeypatch.setattr(psutil,'Process',proc)
    monkeypatch.setattr(status,'_get_process_start_time',lambda pid:starts.get(pid))
    monkeypatch.setattr(status,'_pid_exists',lambda pid:pid in commands)
    monkeypatch.setattr(gateway,'find_profile_gateway_processes',lambda **kw:processes)
    monkeypatch.setattr(gateway,'find_gateway_pids',lambda **kw:discovered)
    monkeypatch.setattr(update_cmd,'_gateway_pid_install_verdict',lambda pid:'unknown')
    return SimpleNamespace(**locals())


def test_exact_safe_wrapper_positive(world):
    assert world.guard._unsafe_gateways()==[]

@pytest.mark.parametrize('mutation',[
    'wrapper-module-spoof','worker-pid-changed','worker-missing-live-identity',
    'wrapper-missing-live-identity','wrapper-reparented-midcheck','worker-reused-midcheck',
    'wrapper-reused-midcheck','same-profile-unproved-other-pid','cross-profile-log',
    'unrelated-parent-exact-argv','wrapper-access-denied','row-start-spoof','unknown-extra-pid',
])
def test_fail_closed_independent(world,monkeypatch,mutation):
    import psutil
    import gateway.status as status
    w=world
    if mutation=='wrapper-module-spoof': w.commands[w.parent][2]='clover_cli.stderr_timestamp_evil'
    elif mutation=='worker-pid-changed':
        w.row['pid']=w.child+1; w.save()
    elif mutation=='worker-missing-live-identity': w.starts[w.child]=None
    elif mutation=='wrapper-missing-live-identity': w.starts[w.parent]=None
    elif mutation in ['worker-reused-midcheck','wrapper-reused-midcheck']:
        target=w.child if mutation.startswith('worker') else w.parent
        calls=[]
        def start(pid):
            if pid==target:
                calls.append(pid)
                threshold=2 if target==w.child else 1
                return w.starts[pid] if len(calls)<=threshold else w.starts[pid]+1
            return w.starts.get(pid)
        monkeypatch.setattr(status,'_get_process_start_time',start)
    elif mutation=='wrapper-reparented-midcheck':
        checks=[]
        def process(pid):
            p=w.proc(pid)
            if pid==w.child:
                def ppid():
                    checks.append(pid)
                    return w.parent if len(checks)==1 else 1
                p.ppid=ppid
            return p
        monkeypatch.setattr(psutil,'Process',process)
    elif mutation=='same-profile-unproved-other-pid':
        w.processes.append(SimpleNamespace(pid=28003,path=w.home,profile='same-profile',create_time=201))
        w.discovered.append(28003)
        w.commands[28003]=w.cmd
        w.starts[28003]=201
    elif mutation=='cross-profile-log': w.commands[w.parent][4]=str(w.tmp_path/'foreign/logs/gateway.error.log')
    elif mutation=='unrelated-parent-exact-argv': w.parents[w.child]=28004
    elif mutation=='wrapper-access-denied':
        def process(pid):
            if pid==w.parent: raise psutil.AccessDenied(pid)
            return w.proc(pid)
        monkeypatch.setattr(psutil,'Process',process)
    elif mutation=='row-start-spoof': w.row['start_time']=199; w.save()
    elif mutation=='unknown-extra-pid': w.discovered.append(28005)
    assert w.guard._unsafe_gateways(),mutation


def test_second_owned_profile_still_admitted_independently(world):
    w=world
    home=w.tmp_path/'second-profile'
    home.mkdir()
    other=28006
    w.starts[other]=220
    w.commands[other]=list(w.cmd)
    w.processes.append(SimpleNamespace(pid=other,path=home,profile='second',create_time=220))
    w.discovered.append(other)
    (home/'gateway_state.json').write_text(json.dumps(dict(w.row,pid=other,start_time=220,clover_home=str(home))),encoding='utf-8')
    assert w.guard._unsafe_gateways()==[]
