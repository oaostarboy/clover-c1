"""Legacy capability absence is not permission to skip identity fences."""
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def owned_gateway(monkeypatch, tmp_path):
    from clover_cli import gateway, update_cmd, update_receipt
    import gateway.status as status
    monkeypatch.setenv('CLOVER_HOME', str(tmp_path))
    monkeypatch.setattr(update_receipt, '_current', None)
    monkeypatch.setattr(gateway, 'find_profile_gateway_processes', lambda **kw: [
        SimpleNamespace(profile='default', path=tmp_path, pid=123, create_time=42)])
    monkeypatch.setattr(gateway, 'find_gateway_pids', lambda **kw: [123])
    monkeypatch.setattr(update_cmd, '_gateway_pid_install_verdict', lambda pid: 'own')
    monkeypatch.setattr(status, '_get_process_start_time', lambda pid: 42)
    monkeypatch.setattr(status, 'runtime_status_pid_is_live', lambda row: True)
    row = {'pid':123, 'start_time':42, 'kind':'clover-gateway',
           'clover_home':str(tmp_path), 'active_agents':0, 'gateway_state':'running'}
    (tmp_path/'gateway_state.json').write_text(json.dumps(row))
    return tmp_path, row


def test_identified_legacy_runner_can_proceed(owned_gateway):
    from clover_cli.update_delivery_guard import require_delivery_safe_update
    assert require_delivery_safe_update()['status'] == 'legacy-proceed'


@pytest.mark.parametrize('mutation', ['missing', 'birth', 'home', 'kind', 'unknown-capability'])
def test_unverified_runtime_cannot_be_relabelled_as_legacy(owned_gateway, mutation):
    from clover_cli.update_delivery_guard import require_delivery_safe_update
    home, row = owned_gateway
    if mutation == 'missing':
        (home/'gateway_state.json').unlink()
    else:
        key, value = {'birth':('start_time', 41), 'home':('clover_home', str(home/'foreign')),
                      'kind':('kind', 'other'),
                      'unknown-capability':('update_restart_capability','unknown-contract')}[mutation]
        row[key] = value
        (home/'gateway_state.json').write_text(json.dumps(row))
    with pytest.raises(SystemExit) as exc:
        require_delivery_safe_update()
    assert exc.value.code == 1  # not old exit=2's false "nothing changed" verdict


def test_discovery_exception_fails_closed_without_claiming_legacy(owned_gateway, monkeypatch, capsys):
    from clover_cli import gateway
    from clover_cli.update_delivery_guard import require_delivery_safe_update
    def fail(**kw):
        raise RuntimeError('identity unavailable')
    monkeypatch.setattr(gateway, 'find_profile_gateway_processes', fail)
    with pytest.raises(SystemExit) as exc:
        require_delivery_safe_update()
    assert exc.value.code == 1
    assert 'nothing was changed' not in capsys.readouterr().out.lower()


def test_unmapped_unknown_install_fails_closed(owned_gateway, monkeypatch):
    from clover_cli import gateway, update_cmd
    from clover_cli.update_delivery_guard import require_delivery_safe_update
    monkeypatch.setattr(gateway, 'find_profile_gateway_processes', lambda **kw: [])
    monkeypatch.setattr(update_cmd, '_gateway_pid_install_verdict', lambda pid: 'unknown')
    with pytest.raises(SystemExit):
        require_delivery_safe_update()

@pytest.mark.parametrize('probe', ['timeout', 'unverified', 'exception'])
def test_unsafe_quiet_wait_never_signals_or_returns_to_forcekill_fallback(owned_gateway, monkeypatch, probe):
    from clover_cli import gateway
    calls = []
    def wait(pid):
        if probe == 'exception':
            raise RuntimeError('unreadable ledger')
        return probe
    monkeypatch.setattr(gateway, 'wait_for_legacy_gateway_idle', wait)
    monkeypatch.setattr(gateway.os, 'kill', lambda *args: calls.append(args))
    with pytest.raises(SystemExit) as exc:
        gateway._graceful_restart_via_sigusr1(123, 10)
    assert exc.value.code == 1
    assert calls == []


def test_defer_receipt_failure_cannot_escape_into_old_force_restart_handlers(owned_gateway, monkeypatch):
    from clover_cli import update_delivery_guard as guard, update_receipt
    def fail(*args, **kwargs):
        raise OSError('receipt unavailable')
    monkeypatch.setattr(update_receipt, 'finalize_update_receipt', fail)
    with pytest.raises(SystemExit) as exc:
        guard.defer_unsafe_restart('still busy')
    assert exc.value.code == 1


@pytest.mark.parametrize('problem', ['bad-db', 'missing-status', 'identity-changed'])
def test_quiet_wait_cannot_turn_missing_evidence_into_idle(owned_gateway, monkeypatch, problem):
    from clover_cli import gateway
    home, row = owned_gateway
    if problem == 'bad-db':
        (home/'state.db').write_bytes(b'not a database')
    elif problem == 'missing-status':
        (home/'gateway_state.json').unlink()
    else:
        row['start_time'] = 41
        (home/'gateway_state.json').write_text(json.dumps(row))
    assert gateway.wait_for_legacy_gateway_idle(123, max_wait=0, stable_seconds=0) == 'unverified'


@pytest.mark.parametrize('failure', ['drain-timeout', 'signal-error'])
def test_legacy_drain_timeout_cannot_fall_back_to_force_stop(owned_gateway, monkeypatch, failure):
    from clover_cli import gateway
    monkeypatch.setattr(gateway, 'wait_for_legacy_gateway_idle', lambda pid: 'idle')
    def signal(*args):
        if failure == 'signal-error':
            raise PermissionError('signal denied')
    monkeypatch.setattr(gateway.os, 'kill', signal)
    monkeypatch.setattr(gateway, '_wait_for_pid_exit', lambda *args: False)
    with pytest.raises(SystemExit) as exc:
        gateway._graceful_restart_via_sigusr1(123, 1)
    assert exc.value.code == 1


def test_ancestor_shortcut_cannot_skip_the_legacy_effect_fence(owned_gateway, monkeypatch):
    from clover_cli import gateway
    calls = []
    monkeypatch.setattr(gateway, '_is_pid_ancestor_of_current_process', lambda pid: True)
    monkeypatch.setattr(gateway, 'wait_for_legacy_gateway_idle', lambda pid: 'timeout')
    monkeypatch.setattr(gateway.os, 'kill', lambda *args: calls.append(args))
    with pytest.raises(SystemExit):
        gateway._request_gateway_self_restart(123)
    assert calls == []



