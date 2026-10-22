"""Catch-up restart must honor the same live-turn drain as the first update."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from clover_cli import update_cmd as u, gateway as g

pytestmark=pytest.mark.skipif(not u.sys.platform.startswith('linux'),reason='Linux systemd catch-up')

@pytest.mark.parametrize('drained',[True,False])
def test_catchup_drains_owned_gateway_before_systemd_can_stop_it(tmp_path,monkeypatch,drained):
    command=tmp_path/'cmdline'
    command.write_bytes(b'/owned/bin/python\0-m\0clover_cli.main\0gateway\0run\0')
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        if 'list-units' in argv:
            text='clover-gateway.service loaded active running fixture' if '--user' in argv else ''
        elif '--property=MainPID' in argv:text='4321\n'
        else:text=''
        return SimpleNamespace(returncode=0,stdout=text)
    monkeypatch.setattr(u.subprocess,'run',run)
    monkeypatch.setattr(u,'_systemd_unit_owned_by_install',lambda *a:True)
    monkeypatch.setattr(u,'_gateway_pid_cmdline_path',lambda pid:command)
    monkeypatch.setattr(u,'_gateway_pid_install_verdict',lambda pid:'own')
    graceful=Mock(return_value=drained)
    monkeypatch.setattr(g,'_graceful_restart_via_sigusr1',graceful)
    monkeypatch.setattr(g,'_get_restart_exit_wait_budget',lambda:180)
    monkeypatch.setattr(u,'_wait_for_service_active',lambda *a,**kw:True)
    monkeypatch.setattr(u,'_service_restart_sec',lambda *a,**kw:5)
    failed=[]
    u._restart_systemd_gateway_units_best_effort(failed)
    graceful.assert_called_once_with(4321,drain_timeout=180)
    assert not any('restart' in call for call in calls), 'a direct systemctl restart amputates the active turn'
    assert failed==([] if drained else ['clover-gateway'])


def test_failed_catchup_drain_is_not_killed_by_later_survivor_sweep(monkeypatch):
    monkeypatch.setattr(u,'_m',lambda:SimpleNamespace(_purge_stale_clover_modules=lambda:None))
    monkeypatch.setattr(u,'_own_install_gateway_pids',lambda pids:list(pids))
    monkeypatch.setattr(g,'find_gateway_pids',lambda **kw:[4321])
    monkeypatch.setattr(g,'supports_systemd_services',lambda:True)
    monkeypatch.setattr(g,'is_macos',lambda:False)
    monkeypatch.setattr(g,'is_windows',lambda:False)
    monkeypatch.setattr(g,'_wait_for_gateway_exit',lambda **kw:None)
    monkeypatch.setattr(u,'_restart_systemd_gateway_units_best_effort',lambda failed:failed.append('clover-gateway'))
    killed=[]
    monkeypatch.setattr('gateway.status.terminate_pid',killed.append)
    assert u._run_pending_fleet_restart() is False
    assert killed==[], 'unverified drain must not become permission for a hard survivor kill'
