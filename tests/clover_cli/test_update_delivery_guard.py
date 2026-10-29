"""A legacy (no capability flag) gateway is updated, never refused; the receipt records it."""
import json
from types import SimpleNamespace

import pytest

from clover_cli import managed_uv


def test_post_pull_api_proceeds_for_legacy_gateway_and_records_receipt(monkeypatch, tmp_path, capsys):
    """The 1.5.6 updater's update_managed_uv() call must not exit 2 for its own gateway."""
    from clover_cli import gateway, update_cmd, update_receipt as receipt
    import gateway.status as status

    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    monkeypatch.setattr('sys.argv', ['clover', 'update', '--yes'])
    monkeypatch.setattr(receipt, '_current', None)
    monkeypatch.setattr(gateway, 'find_profile_gateway_processes', lambda **kw: [
        SimpleNamespace(profile='default', path=tmp_path, pid=123)])
    monkeypatch.setattr(gateway, 'find_gateway_pids', lambda **kw: [123])
    monkeypatch.setattr(update_cmd, '_gateway_pid_install_verdict', lambda pid: 'own')
    monkeypatch.setattr(status, 'runtime_status_pid_is_live', lambda row: True)
    monkeypatch.setattr(status, '_get_process_start_time', lambda pid: 42)
    (tmp_path/'gateway_state.json').write_text(json.dumps({
        'pid':123, 'start_time':42, 'kind':'clover-gateway', 'active_agents':0,
        'gateway_state':'running', 'clover_home':str(tmp_path)}))
    touched=[]
    monkeypatch.setattr(managed_uv, 'resolve_uv', lambda: touched.append('uv') or '/uv')
    monkeypatch.setattr(managed_uv, '_uv_self_update_is_fresh', lambda: True)
    monkeypatch.setattr(managed_uv, 'repair_vulnerable_runtime',
                        lambda uv: managed_uv.RuntimeRepairResult('not-needed'))
    monkeypatch.setattr(managed_uv, '_finish_update_runtime_attempt', lambda *a, **kw: None)
    assert managed_uv.update_managed_uv() == '/uv'          # no SystemExit, same return type
    assert touched == ['uv']                                 # uv work proceeded
    out = capsys.readouterr().out
    assert 'nothing was changed' not in out.lower()
    guard = receipt._current.data['delivery_restart_guard']
    assert guard['status'] == 'legacy-proceed'
    assert guard['reasons'] and 'maintenance_required' not in guard


def test_loaded_runner_advertises_ack_drain(monkeypatch):
    from gateway.run import GatewayRunner
    import gateway.status as status
    calls=[]
    monkeypatch.setattr(status, 'write_runtime_status', lambda **kw: calls.append(kw))
    runner=GatewayRunner.__new__(GatewayRunner)
    runner._restart_requested=False
    monkeypatch.setattr(runner, '_active_work_count', lambda: 0)
    runner._update_runtime_status('running')
    assert calls[0].get('update_restart_capability') == 'reply-carriers-through-ack-v1'


def test_status_does_not_inherit_predecessor_capability(monkeypatch, tmp_path):
    import gateway.status as status
    path=tmp_path/'gateway_state.json'
    path.write_text(json.dumps({'pid': 41, 'start_time': 1,
        'update_restart_capability':'reply-carriers-through-ack-v1'}))
    monkeypatch.setattr(status, '_get_runtime_status_path', lambda: path)
    monkeypatch.setattr(status, '_build_pid_record', lambda: {
        'pid':42, 'start_time':2, 'kind':'clover-gateway', 'argv':[]})
    status.write_runtime_status(gateway_state='starting')
    assert 'update_restart_capability' not in json.loads(path.read_text())


def test_modern_entry_classifies_before_update_work_and_continues(monkeypatch):
    from clover_cli import main as hm, update_cmd, update_delivery_guard
    calls=[]
    monkeypatch.setattr(update_delivery_guard, 'require_delivery_safe_update',
                        lambda: calls.append('guard') or {'status': 'legacy-proceed'})
    def mutation():
        calls.append('work')
        raise _Reached()
    monkeypatch.setattr(update_cmd, '_capture_active_lazy_features', mutation)
    monkeypatch.setattr(hm, '_capture_active_lazy_features', mutation, raising=False)
    try:
        with pytest.raises(_Reached):
            update_cmd._cmd_update_impl(SimpleNamespace(), False)
    finally:
        hm.__dict__.pop('_capture_active_lazy_features', None)
    assert calls == ['guard', 'work']


class _Reached(Exception):
    pass


@pytest.mark.parametrize('entry', ['_apply_pending_fleet_restart_catchup', '_run_pending_fleet_restart'])
def test_catchup_entries_classify_but_never_exit_for_legacy(monkeypatch, entry):
    from clover_cli import update_cmd, update_delivery_guard
    calls=[]
    monkeypatch.setattr(update_delivery_guard, 'require_delivery_safe_update',
                        lambda: calls.append('guard') or {'status': 'legacy-proceed'})
    monkeypatch.setattr(update_cmd, '_pending_fleet_restart_needed', lambda: False)
    monkeypatch.setattr(update_cmd, '_purge_stale_clover_modules', lambda: None)
    from clover_cli import gateway
    monkeypatch.setattr(gateway, 'find_gateway_pids', lambda **kw: [])
    getattr(update_cmd, entry)()
    assert calls == ['guard']


@pytest.mark.parametrize('capability,live,expected', [
    ('reply-carriers-through-ack-v1', True, []),
    ('reply-carriers-through-ack-v1', False, ['unsafe']),
    (True, True, ['unsafe']),
    ('future-unknown', True, ['unsafe']),
    (None, True, ['unsafe']),
])
def test_capability_requires_live_exact_owner(monkeypatch, tmp_path, capability, live, expected):
    from clover_cli import gateway, update_cmd, update_delivery_guard as guard
    import gateway.status as status
    monkeypatch.setattr(gateway, 'find_profile_gateway_processes', lambda **kw: [
        SimpleNamespace(profile='default', path=tmp_path, pid=123)])
    monkeypatch.setattr(gateway, 'find_gateway_pids', lambda **kw: [123])
    monkeypatch.setattr(update_cmd, '_gateway_pid_install_verdict', lambda pid: 'own')
    monkeypatch.setattr(status, 'runtime_status_pid_is_live', lambda row: live)
    monkeypatch.setattr(status, '_get_process_start_time', lambda pid: 42)
    (tmp_path/'gateway_state.json').write_text(json.dumps({
        'pid':123,'start_time':42,'kind':'clover-gateway',
        'clover_home':str(tmp_path),'update_restart_capability':capability}))
    assert bool(guard._unsafe_gateways()) == bool(expected)


@pytest.mark.parametrize('live_start,unsafe', [(None, True), (41, True), (42, False)])
def test_guard_requires_rechecked_status_creation_time(monkeypatch, tmp_path, live_start, unsafe):
    """The diagnostic liveness helper permits unknown times; admission must not."""
    from clover_cli import gateway, update_cmd, update_delivery_guard as guard
    import gateway.status as status

    monkeypatch.setattr(gateway, 'find_profile_gateway_processes', lambda **kw: [
        SimpleNamespace(profile='default', path=tmp_path, pid=123, create_time=42)])
    monkeypatch.setattr(gateway, 'find_gateway_pids', lambda **kw: [123])
    monkeypatch.setattr(update_cmd, '_gateway_pid_install_verdict', lambda pid: 'own')
    monkeypatch.setattr(status, '_pid_exists', lambda pid: True)
    monkeypatch.setattr(status, '_get_process_start_time', lambda pid: live_start)
    (tmp_path/'gateway_state.json').write_text(json.dumps({
        'pid':123, 'start_time':42, 'kind':'clover-gateway',
        'clover_home':str(tmp_path), 'update_restart_capability':guard.CAPABILITY}),
        encoding='utf-8')

    assert bool(guard._unsafe_gateways()) is unsafe


def test_legacy_proceed_preserves_pending_state_and_snapshot(monkeypatch, tmp_path):
    from clover_cli import update_delivery_guard as guard, update_receipt as receipt
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    monkeypatch.setattr(receipt, '_current', None)
    pending=tmp_path/'pending'; pending.write_bytes(b'original restart intent')
    db=tmp_path/'state.db'; db.write_bytes(b'uncertain delivery evidence')
    snapshot=tmp_path/'snapshot'; snapshot.write_bytes(b'original rollback evidence')
    monkeypatch.setattr(guard, '_unsafe_gateways', lambda: ['legacy: default (PID 123): identified legacy runtime'])
    record = guard.require_delivery_safe_update()
    assert record['status'] == 'legacy-proceed'
    assert pending.read_bytes() == b'original restart intent'
    assert db.read_bytes() == b'uncertain delivery evidence'
    assert snapshot.read_bytes() == b'original rollback evidence'
    assert receipt._current.data['delivery_restart_guard']['status'] == 'legacy-proceed'


def test_legacy_proceed_leaves_the_restart_watcher_armed(monkeypatch, tmp_path):
    """The frozen watcher must keep guarding the real restart that now follows."""
    import os
    from clover_cli import update_delivery_guard as guard, update_receipt as receipt
    from clover_cli import update_restart_watcher as watcher
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    monkeypatch.setattr(receipt, '_current', None)
    monkeypatch.setattr(guard, '_unsafe_gateways', lambda: ['legacy: default (PID 123): identified legacy runtime'])
    beacon=watcher.beacon_path(tmp_path)
    beacon.write_text(json.dumps({'updater_pid':os.getpid(),
        'updater_create_time':watcher._process_create_time(os.getpid()),
        'pre_pull_sha':'old', 'repo':'staged-source'}))
    original=beacon.read_bytes()
    guard.require_delivery_safe_update()
    assert beacon.read_bytes() == original


def test_capable_gateway_is_recorded_as_capable_and_silent(monkeypatch, tmp_path, capsys):
    from clover_cli import update_delivery_guard as guard
    monkeypatch.setattr(guard, '_unsafe_gateways', lambda: [])
    assert guard.require_delivery_safe_update()['status'] == 'capable'
    assert capsys.readouterr().out == ''


def test_discovery_failure_defers_as_unverified(monkeypatch, tmp_path):
    from clover_cli import update_delivery_guard as guard, update_receipt as receipt
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    monkeypatch.setattr(receipt, '_current', None)
    monkeypatch.setattr(guard, '_unsafe_gateways', lambda: (_ for _ in ()).throw(RuntimeError('x')))
    with pytest.raises(SystemExit) as exc:
        guard.require_delivery_safe_update()
    assert exc.value.code == 1


def test_legacy_message_is_truthful_and_never_says_nothing_changed(monkeypatch, tmp_path, capsys):
    from clover_cli import update_delivery_guard as guard, update_receipt as receipt
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    monkeypatch.setattr(receipt, '_current', None)
    monkeypatch.setattr(guard, '_unsafe_gateways', lambda: ['legacy: default (PID 1): identified legacy runtime'])
    guard.require_delivery_safe_update()
    out = capsys.readouterr().out.lower()
    assert 'nothing was changed' not in out and 'deferred' not in out and 'maintenance' not in out
    assert 'continues' in out and 'restarted' in out
