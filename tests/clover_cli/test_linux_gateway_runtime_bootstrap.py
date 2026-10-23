"""Legacy PATH-selected updater must continue in its requesting gateway's venv.

Unit fixtures below stand in for /proc and a child updater; hosted tests exercise
actual old source, processes, metadata, public update admission and systemd.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest
from clover_cli import managed_uv as m, update_receipt as receipts

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux updater compatibility')


@pytest.fixture
def runtime_layout(tmp_path, monkeypatch):
    # These synthetic venv/process fixtures exercise runtime handoff after
    # compatibility admission; the guard's owner/capability cases live separately.
    from clover_cli import update_delivery_guard
    monkeypatch.setattr(update_delivery_guard, 'require_delivery_safe_update', lambda: None)
    root = tmp_path/'source'; root.mkdir()
    (root/'pyproject.toml').write_text('[project]\nname="clover-c1"\nversion="1.5.6"\n')
    local, external = root/'venv', tmp_path/'external'
    for live in (local, external):
        (live/'bin').mkdir(parents=True)
        (live/'pyvenv.cfg').write_text('home = /usr/bin\n')
        (live/'bin/python').symlink_to(sys.executable)
        dist = live/'lib/python3.11/site-packages/clover_c1-1.5.6.dist-info'
        dist.mkdir(parents=True)
        (dist/'METADATA').write_text('Name: clover-c1\nVersion: 1.5.6\n')
        (dist/'direct_url.json').write_text(json.dumps({'url':root.as_uri(),'dir_info':{'editable':True}}))
    home = tmp_path/'profile'; home.mkdir()
    uv = tmp_path/'uv'; uv.write_text('unit fixture')
    identity = dict(pid=56789, start_time=123, clover_home=str(home), kind='clover-gateway')
    proc_env = dict(CLOVER_HOME=str(home), VIRTUAL_ENV=str(external))
    process = SimpleNamespace(cmdline=lambda:[str(external/'bin/python'),'-m','clover_cli.main','gateway','run'], environ=lambda:dict(proc_env))
    monkeypatch.setenv('CLOVER_HOME', str(home))
    monkeypatch.setenv('VIRTUAL_ENV', str(external))
    monkeypatch.setattr(sys,'argv',[str(local/'bin/clover'),'update','--gateway'])
    monkeypatch.setattr(m,'_PROJECT_ROOT',root)
    monkeypatch.setattr(m,'_running_editable_venv',lambda root:local)
    monkeypatch.setattr(m,'resolve_uv',lambda:str(uv))
    monkeypatch.setattr(m,'_uv_self_update_is_fresh',lambda:True)
    repair = Mock(return_value=m.RuntimeRepairResult('safe',sqlite_before='3.53.1',sqlite_after='3.53.1'))
    monkeypatch.setattr(m,'repair_vulnerable_runtime',repair)
    monkeypatch.setattr('gateway.control_socket.identify_gateway',lambda h:identity)
    live = Mock(return_value=True)
    monkeypatch.setattr('gateway.status.runtime_status_pid_is_live',live)
    monkeypatch.setattr('gateway.status._get_process_start_time',lambda pid:123)
    monkeypatch.setattr('psutil.Process',lambda pid:process)
    parent = receipts.UpdateReceipt()
    parent.data.update(pre_update={'sha':'a'*40},plan={'original':'old release plan'},pre_pull_sha='a'*40)
    monkeypatch.setattr(receipts,'_current',parent)
    launches=[]
    class Child:
        pid=67890
        def __init__(self, argv, **kwargs):
            launches.append((argv, kwargs))
        def wait(self):
            directory=home/'logs/update_receipts';directory.mkdir(parents=True,exist_ok=True)
            (directory/'latest.json').write_text(json.dumps(dict(pid=self.pid,outcome='success',post_update={'sha':'b'*40},gateway_restart={'restarted_services':['user:clover-gateway.service']},linux_systemd_catchup={'version':1,'targets':[{'profile':'default','pid':56789}],'verified':True},fleet=[{'state':'current'}],steps=[{'name':'child_actual_restart','ok':True}],skips=[])))
            return 0
    monkeypatch.setattr(m.subprocess,'Popen',Child)
    return SimpleNamespace(root=root,local=local,external=external,home=home,identity=identity,proc_env=proc_env,process=process,live=live,parent=parent,repair=repair,launches=launches)


def test_legacy_public_uv_hook_continues_under_proved_requesting_gateway_runtime(runtime_layout):
    f=runtime_layout
    code=None
    try:
        m.update_managed_uv()
    except SystemExit as exc:
        code=exc.code
    assert len(f.launches)==1, 'the old PATH-selected updater never handed back to the external gateway runtime'
    argv,kw=f.launches[0]
    assert argv==[str(f.external/'bin/python'),'-m','clover_cli.main','update','--gateway']
    assert Path(kw['cwd'])==f.root
    assert kw['env']['VIRTUAL_ENV']==str(f.external)
    assert code==0
    assert f.parent.data['pre_update']=={'sha':'a'*40}
    assert f.parent.data['plan']=={'original':'old release plan'}
    assert f.parent.data['gateway_restart']['restarted_services']==['user:clover-gateway.service']
    assert f.parent.data['linux_gateway_runtime_handoff']['child_pid']==67890
    assert f.parent.data['linux_systemd_catchup']['targets']==[{'profile':'default','pid':56789}]
    f.repair.assert_not_called()


@pytest.mark.parametrize('mismatch',['foreign-source','other-home','stale-pid','missing-start-time','ambient-env','symlink','terminal','already-correct'])
def test_bootstrap_does_not_adopt_unproved_or_nonrequesting_runtime(runtime_layout, monkeypatch,mismatch):
    f=runtime_layout
    if mismatch=='foreign-source':
        dist=next(f.external.glob('lib/python*/site-packages/*.dist-info/direct_url.json'))
        dist.write_text(json.dumps({'url':f.local.as_uri(),'dir_info':{'editable':True}}))
    elif mismatch=='other-home':f.proc_env['CLOVER_HOME']=str(f.local)
    elif mismatch=='stale-pid':f.live.return_value=False
    elif mismatch=='missing-start-time':f.identity.pop('start_time')
    elif mismatch=='ambient-env':monkeypatch.setenv('VIRTUAL_ENV',str(f.local))
    elif mismatch=='symlink':
        link=f.external.parent/'external-alias';link.symlink_to(f.external,target_is_directory=True)
        f.process.cmdline=lambda:[str(link/'bin/python'),'-m','clover_cli.main','gateway','run']
        f.proc_env['VIRTUAL_ENV']=str(link);monkeypatch.setenv('VIRTUAL_ENV',str(link))
    elif mismatch=='terminal':monkeypatch.setattr(sys,'argv',['clover','update','--yes'])
    elif mismatch=='already-correct':monkeypatch.setattr(m,'_running_editable_venv',lambda root:f.external)
    m.update_managed_uv()
    assert f.launches==[]
    f.repair.assert_called_once()


@pytest.mark.parametrize('outcome,exit_code,receipt_pid,expected', [
    ('missing',0,67890,1), ('partial',0,67890,1),
    ('refused',0,67890,1), ('success',0,11111,1), ('failed',7,67890,7),
])
def test_child_result_cannot_turn_missing_or_failed_proof_green(runtime_layout,monkeypatch,outcome,exit_code,receipt_pid,expected):
    f=runtime_layout
    class Child:
        pid=67890
        def __init__(self,*a,**kw):pass
        def wait(self):
            if outcome!='missing':
                directory=f.home/'logs/update_receipts';directory.mkdir(parents=True,exist_ok=True)
                (directory/'latest.json').write_text(json.dumps(dict(pid=receipt_pid,outcome=outcome)))
            return exit_code
    monkeypatch.setattr(m.subprocess,'Popen',Child)
    with pytest.raises(SystemExit) as error:m.update_managed_uv()
    assert error.value.code==expected
    assert f.parent.data['pre_update']=={'sha':'a'*40}
    assert f.parent.data['linux_gateway_runtime_handoff']['exit_code']==expected
    f.repair.assert_not_called()
