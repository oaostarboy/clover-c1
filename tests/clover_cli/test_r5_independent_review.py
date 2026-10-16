"""Independent portable counterexamples; process witnesses are synthetic, not native certification."""
import copy
from types import SimpleNamespace
import json

import pytest
from tests.clover_cli.test_windows_update_receipt_handoff import handoff
from clover_cli import update_receipt as receipt
from clover_cli import windows_update_receipt_handoff as bridge


@pytest.mark.parametrize('verified', [False, None, True])
def test_unmapped_only_failed_real_verifier_must_not_publish_success(handoff, monkeypatch, verified):
    from clover_cli import gateway, update_cmd, update_restart_watcher as watcher
    handoff.child()
    monkeypatch.setattr(update_cmd, '_m', lambda: SimpleNamespace(
        _is_windows=lambda: True, _refresh_windows_gateway_launchers=lambda: None,
        _verify_windows_gateway_relaunch=lambda **kwargs: verified))
    monkeypatch.setattr(gateway, 'launch_detached_gateway_restart_by_cmdline', lambda *args: True)
    monkeypatch.setattr(update_cmd, '_with_current_gateway_interpreter', lambda argv: argv)
    monkeypatch.setattr(watcher, 'write_relaunch_marker', lambda *args: None)
    token = {'resume_needed': True, 'profiles': {}, 'unmapped': [
        {'pid': 303, 'argv': ['python.exe', '-m', 'clover_cli.main', 'gateway', 'run']}], 'services': []}
    update_cmd._resume_windows_gateways_after_update(token)
    assert token['unmapped_relaunched'] == 1
    assert token['relaunch_verification_result'] is verified
    receipt.record_windows_continuation_result(healthy=True, token=token)
    path = receipt.finalize_pending_update_receipt(0)
    actual = json.loads(path.read_text())
    assert actual['outcome'] == ('success' if verified is True else 'partial'), actual


def _resume(handoff):
    handoff.phase.update(pid=202, epoch=handoff.phase['epoch'] + 1)
    current = receipt.UpdateReceipt().data
    interpreter = {'prefix': handoff.fake_sys.prefix, 'executable': handoff.fake_sys.executable,
        'base_executable': handoff.fake_sys._base_executable, 'argv': ['child-module', *handoff.arguments]}
    path, status = bridge.resume(current, handoff.directory, handoff.repo,
        process_chain=lambda *args, **kwargs: copy.deepcopy(handoff.chain),
        interpreter=interpreter, now=lambda: handoff.phase['epoch'], wait_seconds=0)
    return current, path, status


@pytest.mark.parametrize('handoff', [True, False], indirect=True)
@pytest.mark.parametrize('boundary', ['canonical', 'latest'])
def test_adoption_recovers_after_canonical_saved_but_latest_write_fails(handoff, monkeypatch, boundary):
    if handoff.parent is None:
        monkeypatch.setattr(receipt, '_windows_handoff_child_pid', lambda: 202)
        assert receipt.finalize_pending_update_receipt(0) is None
        monkeypatch.setattr(receipt, '_windows_handoff_child_pid', lambda: 0)
    atomic = bridge._atomic
    def fail_latest(path, raw):
        if (path.name == 'latest.json') if boundary == 'latest' else path.name.startswith('update_'):
            raise OSError('synthetic interrupted latest publication')
        return atomic(path, raw)
    monkeypatch.setattr(bridge, '_atomic', fail_latest)
    current, path, status = _resume(handoff)
    assert status == 'unverified'
    monkeypatch.setattr(bridge, '_atomic', atomic)
    current, path, status = _resume(handoff)
    assert status == 'adopted', current.get(bridge.TAG)


def test_other_claimant_is_rejected_without_touching_parent(handoff):
    current, path, status = _resume(handoff)
    assert status == 'adopted'
    before = path.read_bytes()
    claim = next((handoff.directory / 'handoff_phases').glob('claim-*.json'))
    payload = json.loads(claim.read_text())
    payload['continuation_pid'] = 999
    claim.write_text(json.dumps(payload))
    current, path, status = _resume(handoff)
    assert status == 'unverified'
    assert handoff.parent.read_bytes() == before


@pytest.mark.parametrize('handoff', [True, False], indirect=True)
def test_owned_terminal_publication_can_finish_without_a_second_receipt(handoff, monkeypatch):
    from clover_cli import update_receipt as r
    handoff.child() if handoff.parent is not None else _begin_modern_child(handoff, monkeypatch)
    r.record_windows_continuation_result(healthy=True, token={
        'resume_needed': False, 'relaunched_profiles': ['default'],
        'relaunch_verification_result': True,
    })
    original = type(handoff.directory).write_text
    interrupted = []
    def interrupt_latest(path, *args, **kwargs):
        if path == handoff.directory / 'latest.json':
            interrupted.append(True)
            raise OSError('synthetic terminal latest publication interruption')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as context:
        context.setattr(type(handoff.directory), 'write_text', interrupt_latest)
        written = r.finalize_pending_update_receipt(0)
        assert written is not None
    assert interrupted == [True]
    assert json.loads(written.read_text())['outcome'] == 'success'
    before_recovery = r.read_latest_receipt()
    assert before_recovery is not None
    assert before_recovery['outcome'] == 'running'
    handoff.child()
    assert r._current is None
    canonical = list(handoff.directory.glob('update_*.json'))
    assert len(canonical) == 1
    assert canonical[0].read_bytes() == (handoff.directory / 'latest.json').read_bytes()
    latest = r.read_latest_receipt()
    assert latest is not None
    assert latest['outcome'] == 'success'


def _begin_modern_child(handoff, monkeypatch):
    from clover_cli import update_receipt as r
    monkeypatch.setattr(r, '_windows_handoff_child_pid', lambda: 202)
    assert r.finalize_pending_update_receipt(0) is None
    monkeypatch.setattr(r, '_windows_handoff_child_pid', lambda: 0)
    handoff.child()
