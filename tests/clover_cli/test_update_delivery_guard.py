"""Compatibility refusal must precede any legacy restart or runtime mutation."""
import json
from types import SimpleNamespace

import pytest

from clover_cli import managed_uv


def test_post_pull_api_refuses_legacy_before_uv_mutation(monkeypatch, tmp_path):
    from clover_cli import gateway, update_cmd
    import gateway.status as status

    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    monkeypatch.setattr(gateway, 'find_profile_gateway_processes', lambda **kw: [
        SimpleNamespace(profile='default', path=tmp_path, pid=123)])
    monkeypatch.setattr(gateway, 'find_gateway_pids', lambda **kw: [123])
    monkeypatch.setattr(update_cmd, '_gateway_pid_install_verdict', lambda pid: 'own')
    monkeypatch.setattr(status, 'runtime_status_pid_is_live', lambda row: True)
    (tmp_path/'gateway_state.json').write_text(json.dumps({
        'pid':123, 'start_time':42, 'kind':'clover-gateway', 'active_agents':0,
        'gateway_state':'running', 'clover_home':str(tmp_path)}))
    touched=[]
    monkeypatch.setattr(managed_uv, 'resolve_uv', lambda: touched.append('uv'))
    monkeypatch.setattr(managed_uv, 'ensure_uv', lambda **kw: None)
    monkeypatch.setattr(managed_uv, '_finish_update_runtime_attempt', lambda *a, **kw: None)
    with pytest.raises(SystemExit) as exc:
        managed_uv.update_managed_uv()
    assert exc.value.code == 2
    assert touched == []


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


def test_modern_entry_guards_before_update_work(monkeypatch):
    from clover_cli import update_cmd, update_delivery_guard
    calls=[]
    def refuse():
        calls.append('guard')
        raise SystemExit(2)
    monkeypatch.setattr(update_delivery_guard, 'require_delivery_safe_update', refuse)
    def mutation():
        pytest.fail('update work ran before compatibility guard')
    monkeypatch.setattr(update_cmd, '_capture_active_lazy_features', mutation)
    with pytest.raises(SystemExit):
        update_cmd._cmd_update_impl(SimpleNamespace(), False)
    assert calls == ['guard']


@pytest.mark.parametrize('entry', ['_apply_pending_fleet_restart_catchup', '_run_pending_fleet_restart'])
def test_catchup_cannot_bypass_guard(monkeypatch, entry):
    from clover_cli import update_cmd, update_delivery_guard
    calls=[]
    def refuse():
        calls.append('guard')
        raise SystemExit(2)
    monkeypatch.setattr(update_delivery_guard, 'require_delivery_safe_update', refuse)
    monkeypatch.setattr(update_cmd, '_pending_fleet_restart_needed', lambda: False)
    monkeypatch.setattr(update_cmd, '_purge_stale_clover_modules', lambda: None)
    from clover_cli import gateway
    monkeypatch.setattr(gateway, 'find_gateway_pids', lambda **kw: [])
    with pytest.raises(SystemExit):
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
    (tmp_path/'gateway_state.json').write_text(json.dumps({
        'pid':123,'start_time':42,'kind':'clover-gateway',
        'clover_home':str(tmp_path),'update_restart_capability':capability}))
    assert bool(guard._unsafe_gateways()) == bool(expected)


def test_refusal_preserves_pending_state_and_snapshot(monkeypatch, tmp_path):
    from clover_cli import update_delivery_guard as guard, update_receipt as receipt
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    monkeypatch.setattr(receipt, '_current', None)
    pending=tmp_path/'pending'; pending.write_bytes(b'original restart intent')
    db=tmp_path/'state.db'; db.write_bytes(b'uncertain delivery evidence')
    snapshot=tmp_path/'snapshot'; snapshot.write_bytes(b'original rollback evidence')
    monkeypatch.setattr(guard, '_unsafe_gateways', lambda: ['legacy'])
    with pytest.raises(SystemExit) as exc: guard.require_delivery_safe_update()
    assert exc.value.code == 2
    assert pending.read_bytes() == b'original restart intent'
    assert db.read_bytes() == b'uncertain delivery evidence'
    assert snapshot.read_bytes() == b'original rollback evidence'
    latest=json.loads((tmp_path/'logs/update_receipts/latest.json').read_text())
    assert latest['outcome'] == 'refused'
    assert latest['delivery_restart_guard']['maintenance_required'] is True
