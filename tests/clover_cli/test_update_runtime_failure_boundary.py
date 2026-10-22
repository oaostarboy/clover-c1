"""The long-standing updater hook must not turn an unsafe repair failure green."""
from pathlib import Path
from unittest.mock import patch
import pytest
from clover_cli import managed_uv as m


@pytest.mark.parametrize('status', ['failed', 'skipped'])
def test_public_update_hook_refuses_known_vulnerable_unrepaired_runtime(tmp_path, status):
    uv=tmp_path/'uv'
    uv.write_text('synthetic',encoding='utf-8')
    repair=m.RuntimeRepairResult(status,'provisioning unavailable',sqlite_before='3.50.4')
    observed=[]
    with (patch.object(m,'resolve_uv',return_value=str(uv)),
          patch.object(m,'_uv_self_update_is_fresh',return_value=True),
          patch.object(m,'repair_vulnerable_runtime',return_value=repair)):
        with pytest.raises(SystemExit) as error:
            m.update_managed_uv(repair_observer=observed.append)
    assert error.value.code == 1
    assert observed == [repair]


@pytest.mark.parametrize('gateway_mode', [False, True])
def test_normal_command_boundary_records_runtime_failure_and_releases_lock(tmp_path, monkeypatch, gateway_mode):
    import json
    from types import SimpleNamespace
    from clover_cli import main, update_receipt as receipts
    from clover_constants import get_clover_home
    from clover_cli.update_lock import UpdateLock

    uv=tmp_path/'uv'
    uv.write_text('synthetic',encoding='utf-8')
    repair=m.RuntimeRepairResult('failed','provisioning unavailable',sqlite_before='3.50.4')
    monkeypatch.setattr('clover_cli.config.is_managed', lambda: False)
    monkeypatch.setattr('clover_cli.update_contract.evaluate_update_admission', lambda root: None)
    monkeypatch.setattr('clover_cli.update_preflight.gate_cli_update', lambda *a,**kw: None)
    monkeypatch.setattr(main,'_install_hangup_protection',lambda **kw: None)
    monkeypatch.setattr(main,'_finalize_update_output',lambda state: None)
    monkeypatch.setattr('clover_cli.update_restart_watcher.wait_for_cli_verdict',lambda: True)
    monkeypatch.setattr(m,'resolve_uv',lambda: str(uv))
    monkeypatch.setattr(m,'_uv_self_update_is_fresh',lambda: True)
    monkeypatch.setattr(m,'repair_vulnerable_runtime',lambda uv: repair)
    def apply(args,gateway_mode):
        receipts.begin_update_receipt()
        m.update_managed_uv()
        pytest.fail('a failed runtime must not reach restart/completion')
    monkeypatch.setattr(main,'_cmd_update_impl',apply)
    with pytest.raises(SystemExit) as error:
        main.cmd_update(SimpleNamespace(gateway=gateway_mode))
    assert error.value.code == 1
    receipt=json.loads((get_clover_home()/'logs/update_receipts/latest.json').read_text(encoding='utf-8'))
    assert receipt['outcome'] == 'failed'
    assert any(step['name'] == 'sqlite_runtime' and step['ok'] is False for step in receipt['steps'])
    lock=UpdateLock()
    assert lock.acquire()
    lock.release()


def test_failed_bootstrap_reports_runtime_failure_once(tmp_path, capsys):
    uv=tmp_path/'uv'
    uv.write_text('synthetic',encoding='utf-8')
    repair=m.RuntimeRepairResult('failed','probe unavailable')
    from types import SimpleNamespace
    with (patch.object(m,'resolve_uv',side_effect=[None,None,str(uv)]),
          patch.object(m,'managed_uv_path',return_value=uv),
          patch.object(m,'_install_uv'),
          patch.object(m,'repair_vulnerable_runtime',return_value=repair),
          patch.object(m.subprocess,'run',return_value=SimpleNamespace(stdout='uv fixture'))):
        assert m.update_managed_uv() == str(uv)
    assert capsys.readouterr().out.count('Managed Python runtime was not replaced') == 1


def test_unproved_runtime_ownership_refuses_update_without_executing_foreign_python(tmp_path):
    uv=tmp_path/'uv';uv.write_text('fixture',encoding='utf-8')
    repair=m.RuntimeRepairResult('refused','virtual environment ownership could not be established')
    with (patch.object(m,'resolve_uv',return_value=str(uv)),
          patch.object(m,'_uv_self_update_is_fresh',return_value=True),
          patch.object(m,'repair_vulnerable_runtime',return_value=repair),
          patch.object(m,'probe_sqlite_runtime',side_effect=AssertionError('do not execute an unowned interpreter'))):
        with pytest.raises(SystemExit) as error:
            m.update_managed_uv()
    assert error.value.code == 1


def test_public_update_hook_keeps_unknown_separate_from_known_unsafe(tmp_path):
    uv=tmp_path/'uv'
    uv.write_text('synthetic',encoding='utf-8')
    repair=m.RuntimeRepairResult('skipped','probe unavailable')
    with (patch.object(m,'resolve_uv',return_value=str(uv)),
          patch.object(m,'_uv_self_update_is_fresh',return_value=True),
          patch.object(m,'repair_vulnerable_runtime',return_value=repair)):
        assert m.update_managed_uv() == str(uv)


def test_public_update_bootstrap_cannot_hide_unrepaired_runtime(tmp_path):
    uv=tmp_path/'uv'
    repair=m.RuntimeRepairResult('failed','provisioning unavailable',sqlite_before='3.50.4')
    def bootstrap(**kwargs):
        observer=kwargs.get('repair_observer')
        assert observer is not None
        observer(repair)
        return str(uv)
    with (patch.object(m,'resolve_uv',return_value=None),
          patch.object(m,'ensure_uv',side_effect=bootstrap)):
        with pytest.raises(SystemExit) as error:
            m.update_managed_uv()
    assert error.value.code == 1


@pytest.mark.parametrize('failure', ['exception', 'no-uv'])
def test_unexpected_repair_failure_rechecks_actual_target_before_success(tmp_path, failure):
    from tests.clover_cli.test_managed_uv import _runtime_info
    uv=tmp_path/'uv'
    uv.write_text('synthetic',encoding='utf-8')
    info=_runtime_info(Path('target-python'), (3,50,4))
    with (patch.object(m,'resolve_uv',return_value=None if failure == 'no-uv' else str(uv)),
          patch.object(m,'_uv_self_update_is_fresh',return_value=True),
          patch.object(m,'repair_vulnerable_runtime',side_effect=OSError('synthetic failure')),
          patch.object(m,'ensure_uv',return_value=None),
          patch.object(m,'probe_sqlite_runtime',return_value=info)):
        with pytest.raises(SystemExit) as error:
            m.update_managed_uv()
    assert error.value.code == 1


def test_unrecognized_layout_does_not_hide_known_vulnerable_interpreter(tmp_path):
    from tests.clover_cli.test_managed_uv import _runtime_info
    uv=tmp_path/'uv'
    uv.write_text('synthetic',encoding='utf-8')
    info=_runtime_info(Path('target-python'), (3,50,4))
    with (patch.object(m,'resolve_uv',return_value=str(uv)),
          patch.object(m,'_uv_self_update_is_fresh',return_value=True),
          patch.object(m,'repair_vulnerable_runtime',return_value=m.RuntimeRepairResult('not-applicable')),
          patch.object(m,'probe_sqlite_runtime',return_value=info)):
        with pytest.raises(SystemExit) as error:
            m.update_managed_uv()
    assert error.value.code == 1
