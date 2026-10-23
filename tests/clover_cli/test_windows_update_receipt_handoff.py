"""Windows receipt handoff contracts. Process/clock witnesses are synthetic.

Filesystem, receipt producer/finalizer, beacon producer and inventory consumer
are real. These tests do not claim native Windows process or gateway proof.
The legacy producer path intentionally omits the new transfer API, matching
v1.5.5's unchanged normal begin/save/finalize APIs and sys.exit(0) boundary.
"""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from clover_cli import build_info, config, update_inventory, update_lock, update_receipt as receipt
from clover_cli import update_restart_watcher as watcher


@pytest.fixture
def handoff(tmp_path, monkeypatch, request):
    mode = getattr(request, 'param', True)
    options = mode if isinstance(mode, dict) else {}
    home = tmp_path / 'home'
    repo = home / 'clover-c1'
    venv = repo / 'venv'
    python = str(venv / 'Scripts/python.exe')
    base_python = str(tmp_path / 'base/python.exe')
    now = datetime.now(timezone.utc).timestamp()
    phase = {'pid': 101, 'epoch': now, 'sha': 'a' * 40}
    arguments = ['update', '--yes']
    fake_sys = SimpleNamespace(platform='win32', argv=[str(venv / 'Scripts' / options.get('launcher', 'clover.exe')), *arguments],
                               executable=python, _base_executable=base_python,
                               prefix=str(venv), base_prefix=str(tmp_path / 'base'), stderr=sys.stderr)
    fake_os = SimpleNamespace(getpid=lambda: phase['pid'], environ={})
    monkeypatch.setattr(receipt, 'sys', fake_sys)
    monkeypatch.setattr(receipt, 'os', fake_os)
    monkeypatch.setattr(receipt, 'time', SimpleNamespace(time=lambda: phase['epoch'], strftime=time.strftime))
    monkeypatch.setattr(receipt, '_utc_now_iso', lambda: datetime.fromtimestamp(phase['epoch'], timezone.utc).isoformat())
    monkeypatch.setattr(receipt, '_current', None)
    monkeypatch.setattr(receipt, '_began_in_process', False)
    monkeypatch.setattr(receipt, '_written_in_process', False)
    monkeypatch.setattr(receipt, '_project_root', lambda: repo, raising=False)
    monkeypatch.setattr(config, 'get_clover_home', lambda: home)
    monkeypatch.setattr(build_info, 'get_code_identity', lambda **_kwargs: {'sha': phase['sha'], 'source': 'git'})
    monkeypatch.setattr(update_lock, 'record_update_outcome', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(watcher, '_process_create_time', lambda _pid: now + 1)
    monkeypatch.setattr(watcher, 'time', SimpleNamespace(time=lambda: phase['epoch']))
    chain = [{'pid': 202, 'parent_pid': 101, 'executable': python,
              'created_utc': datetime.fromtimestamp(now + 1, timezone.utc).isoformat()}]
    monkeypatch.setattr(receipt, '_windows_process_chain', lambda *_args, **_kwargs: copy.deepcopy(chain), raising=False)
    plan = {'profiles': ['default'], 'runtimes': [{'kind': 'gateway', 'profile': 'default', 'pid': 303,
                                                'supervisor': 'manual', 'restart_via': 'manual'}]}
    receipt.begin_update_receipt()
    parent_receipt = receipt._current
    assert parent_receipt is not None
    parent_receipt.data['plan'] = copy.deepcopy(plan)
    receipt.save_pending_receipt('a' * 40, 'pre-update-fixture')
    phase.update(epoch=now + 1, sha='b' * 40)
    watcher.write_beacon(['not-a-real-gateway'], clover_home=home, pre_pull_sha='a' * 40,
                         repo=str(repo), pre_update_snapshot_id='pre-update-fixture')
    watcher.hand_off_beacon(202, clover_home=home)
    phase['epoch'] = now + 2
    parent_path = None
    parent_bytes = None
    legacy = options.get('legacy', True) if options else mode
    if legacy:
        parent_path = receipt.finalize_pending_update_receipt(0, stop_reason='sys.exit(0)')
        assert parent_path is not None
        parent_bytes = parent_path.read_bytes()

    def child():
        phase.update(pid=202, epoch=now + 3)
        fake_os.environ['CLOVER_UPDATE_REEXEC'] = '1'
        fake_sys.argv = [str(repo / 'clover_cli/main.py'), *arguments]
        receipt.begin_update_receipt()

    return SimpleNamespace(home=home, repo=repo, directory=receipt._receipt_dir(), parent=parent_path,
                           parent_bytes=parent_bytes, phase=phase, chain=chain, child=child,
                           plan=plan, arguments=arguments, fake_os=fake_os, fake_sys=fake_sys)


def test_legacy_shim_continuation_keeps_original_run_and_finalizes_one_canonical_receipt(handoff):
    handoff.child()
    current = receipt._current
    assert current is not None
    assert current.data['pre_update']['sha'] == 'a' * 40, current.data.get('windows_dependency_handoff')
    original = json.loads(handoff.parent_bytes)
    assert current.data['pid'] == original['pid'] == 101
    assert current.data['started_at'] == original['started_at']
    assert current.data['argv'] == original['argv']
    assert current.data['finished_at'] is None and current.data['outcome'] == 'running'
    update_inventory.record_plan_in_receipt(SimpleNamespace(to_dict=lambda: {'profiles': [], 'runtimes': []}))
    assert current.data['plan'] == handoff.plan, 'continuation inventory replaced original runtime plan'
    receipt.record_windows_continuation_result(healthy=True, token={'resume_needed': False,
        'relaunched_profiles': ['default'], 'restarted_services': [], 'relaunch_verification_result': True})
    result = receipt.finalize_pending_update_receipt(0, stop_reason='completed at command boundary')
    assert result == handoff.parent
    assert list(handoff.directory.glob('update_*.json')) == [handoff.parent]
    canonical = json.loads(result.read_text(encoding='utf-8'))
    assert canonical == json.loads((handoff.directory / 'latest.json').read_text(encoding='utf-8'))
    assert canonical['pre_update']['sha'] == 'a' * 40 and canonical['post_update']['sha'] == 'b' * 40
    assert canonical['pid'] == 101 and canonical['outcome'] == 'success'
    assert canonical['gateway_restart']['relaunched_profiles'] == ['default']
    continuation = canonical['windows_dependency_handoff']
    assert continuation['continuation_pid'] == 202
    assert (handoff.directory / continuation['legacy_phase_file']).read_bytes() == handoff.parent_bytes
    assert receipt.finalize_pending_update_receipt(0) is None


@pytest.mark.parametrize('handoff', [True, False], indirect=True)
def test_handoff_clock_tick_before_measured_birth_preserves_owned_receipt(handoff, monkeypatch):
    """Replay R9's measured 468us inversion through the real beacon producer."""
    birth = handoff.phase['epoch'] - 1
    monkeypatch.setattr(watcher, '_process_create_time', lambda _pid: birth)
    handoff.phase['epoch'] = birth - 0.000468
    assert watcher.hand_off_beacon(202, clover_home=handoff.home)
    if handoff.parent is None:
        handoff.phase['epoch'] = birth + 1
        monkeypatch.setattr(receipt, '_windows_handoff_child_pid', lambda: 202)
        assert receipt.finalize_pending_update_receipt(0) is None
        monkeypatch.setattr(receipt, '_windows_handoff_child_pid', lambda: 0)
    handoff.child()
    current = receipt._current
    assert current is not None
    assert current.data['pre_update']['sha'] == 'a' * 40, current.data.get('windows_dependency_handoff')
    assert current.data['windows_dependency_handoff']['state'] == 'adopted'
    assert current.data['pid'] == 101
    receipt.record_windows_continuation_result(healthy=True, token={
        'resume_needed': False, 'relaunched_profiles': ['default'], 'relaunch_verification_result': True})
    final = receipt.finalize_pending_update_receipt(0)
    assert final is not None
    assert receipt.read_latest_receipt()['outcome'] == 'success'
    assert len(list(handoff.directory.glob('update_*.json'))) == 1


@pytest.mark.parametrize('fault', [None, 'before_parent', 'after_completion', 'missing_birth', 'reused_pid'])
def test_immutable_legacy_publisher_uses_proven_parent_phase_not_child_clock_tick(handoff, fault):
    """Replay R10's actual old-release publisher; do not run the corrected writer."""
    from clover_cli import windows_update_receipt_handoff as bridge
    path = handoff.home / '.clover-update-heartbeat.json'
    beacon = json.loads(path.read_text())
    parent = json.loads(handoff.parent_bytes)
    # Real native old2new inversion: 1792122513.0407076 < 1792122513.0415337.
    beacon['refreshed_at'] = beacon['updater_create_time'] - .0008261
    if fault == 'before_parent':
        beacon['refreshed_at'] = bridge._stamp(parent['started_at']) - .001
    elif fault == 'after_completion':
        beacon['refreshed_at'] = bridge._stamp(parent['finished_at']) + .001
    elif fault == 'missing_birth':
        beacon['updater_create_time'] = None
    elif fault == 'reused_pid':
        beacon['updater_create_time'] -= 10
    path.write_text(json.dumps(beacon))
    handoff.child()
    current = receipt._current
    assert current is not None
    if fault is None:
        assert current.data['windows_dependency_handoff']['state'] == 'adopted', current.data
        assert current.data['pre_update']['sha'] == 'a' * 40 and current.data['pid'] == 101
        receipt.record_windows_continuation_result(healthy=True, token={
            'resume_needed': False, 'relaunched_profiles': ['default'], 'relaunch_verification_result': True})
        final = receipt.finalize_pending_update_receipt(0)
        assert final is not None and len(list(handoff.directory.glob('update_*.json'))) == 1
    else:
        assert current.data['windows_dependency_handoff']['state'] == 'unverified'
        assert handoff.parent.read_bytes() == handoff.parent_bytes
        assert not (handoff.directory / 'handoff_phases').exists()


def test_cold_process_query_can_retry_without_losing_original_receipt(handoff, monkeypatch):
    """Native 39889573087 timed out its only CIM query after five seconds."""
    from clover_cli import windows_update_receipt_handoff as bridge
    import subprocess
    clock = [0.0]
    monkeypatch.setattr(bridge, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda delay: clock.__setitem__(0, clock[0] + delay)))
    calls = []

    def cold_query(pid, *, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            clock[0] += timeout
            raise subprocess.TimeoutExpired('private-process-command', timeout)
        clock[0] += .1
        return copy.deepcopy(handoff.chain)

    handoff.phase.update(pid=202, epoch=handoff.phase['epoch'] + 1)
    current = receipt.UpdateReceipt().data
    path, status = bridge.resume(current, handoff.directory, handoff.repo,
        process_chain=cold_query, interpreter={'prefix': handoff.fake_sys.prefix,
        'executable': handoff.fake_sys.executable, 'base_executable': handoff.fake_sys._base_executable,
        'argv': ['child-module', *handoff.arguments]}, now=lambda: handoff.phase['epoch'])
    assert status == 'adopted', current.get(bridge.TAG)
    assert path == handoff.parent
    assert current['pid'] == 101 and current['pre_update']['sha'] == 'a' * 40
    assert current['outcome'] == 'running'
    diagnostics = current[bridge.TAG + '_probe']
    assert diagnostics['attempt_count'] == 1
    assert diagnostics['attempts'][0]['reason'] == 'TimeoutExpired'
    assert 'private-process-command' not in json.dumps(diagnostics)
    assert len(calls) == 2


def test_refusal_retains_predicate_before_later_process_query_timeout(handoff, monkeypatch):
    from clover_cli import windows_update_receipt_handoff as bridge
    import subprocess
    clock = [0.0]
    monkeypatch.setattr(bridge, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda delay: clock.__setitem__(0, clock[0] + delay)))
    beacon_path = handoff.home / '.clover-update-heartbeat.json'
    beacon = json.loads(beacon_path.read_text())
    # Keep this a truly stale legacy phase, not a coarse child-clock tick.
    beacon['refreshed_at'] = bridge._stamp(json.loads(handoff.parent_bytes)['started_at']) - .001
    beacon_path.write_text(json.dumps(beacon))
    calls = []

    def query(pid, *, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            clock[0] += .1
            return copy.deepcopy(handoff.chain)
        clock[0] = 1.1
        raise subprocess.TimeoutExpired('not-persisted-command', timeout)

    handoff.phase.update(pid=202, epoch=handoff.phase['epoch'] + 1)
    current = receipt.UpdateReceipt().data
    path, status = bridge.resume(current, handoff.directory, handoff.repo,
        process_chain=query, interpreter={'prefix': handoff.fake_sys.prefix,
        'executable': handoff.fake_sys.executable, 'base_executable': handoff.fake_sys._base_executable,
        'argv': ['child-module', *handoff.arguments]}, now=lambda: handoff.phase['epoch'], wait_seconds=1)
    assert path is None and status == 'unverified'
    diagnostics = current['windows_dependency_handoff']['probe']
    assert [x['reason'] for x in diagnostics['attempts']] == ['stale beacon', 'TimeoutExpired']
    assert [x['phase'] for x in diagnostics['attempts']] == ['beacon', 'process_chain']
    assert [x['process_timeout_seconds'] for x in diagnostics['attempts']] == calls
    assert diagnostics['elapsed_seconds'] == 1.1
    assert 'not-persisted-command' not in json.dumps(diagnostics)
    assert handoff.parent.read_bytes() == handoff.parent_bytes


@pytest.mark.parametrize('parent_exits', [False, True])
def test_live_original_producer_is_not_adopted_until_its_measured_exit(handoff, monkeypatch, parent_exits):
    from clover_cli import windows_update_receipt_handoff as bridge
    clock = [0.0]
    monkeypatch.setattr(bridge, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda delay: clock.__setitem__(0, clock[0] + delay)))
    original = json.loads(handoff.parent_bytes)
    live_parent = {'pid': 101, 'parent_pid': 99, 'executable': handoff.fake_sys.executable,
                   'created_utc': original['started_at']}
    calls = []

    def query(pid, *, timeout):
        calls.append(timeout)
        clock[0] += .15
        return copy.deepcopy(handoff.chain + ([] if parent_exits and len(calls) > 1 else [live_parent]))

    handoff.phase.update(pid=202, epoch=handoff.phase['epoch'] + 1)
    current = receipt.UpdateReceipt().data
    path, status = bridge.resume(current, handoff.directory, handoff.repo, process_chain=query,
        interpreter={'prefix': handoff.fake_sys.prefix, 'executable': handoff.fake_sys.executable,
        'base_executable': handoff.fake_sys._base_executable, 'argv': ['child-module', *handoff.arguments]},
        now=lambda: handoff.phase['epoch'], wait_seconds=.5)
    if parent_exits:
        assert status == 'adopted' and path is not None
        assert current['pre_update']['sha'] == 'a' * 40
        diagnostics = current[bridge.TAG + '_probe']
    else:
        assert status == 'unverified' and path is None
        assert handoff.parent.read_bytes() == handoff.parent_bytes
        diagnostics = current[bridge.TAG]['probe']
    assert diagnostics['attempts'][0]['reason'] == 'original producer still alive'
    assert 0 < max(calls) <= .5
    assert clock[0] <= .65


def test_probe_failure_history_is_bounded_without_losing_first_predicate(handoff, monkeypatch):
    from clover_cli import windows_update_receipt_handoff as bridge
    clock = [0.0]
    monkeypatch.setattr(bridge, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda delay: clock.__setitem__(0, clock[0] + delay)))
    handoff.chain[0]['parent_pid'] = 999
    handoff.phase.update(pid=202, epoch=handoff.phase['epoch'] + 1)
    current = receipt.UpdateReceipt().data
    path, status = bridge.resume(current, handoff.directory, handoff.repo,
        process_chain=lambda *_args, **_kwargs: copy.deepcopy(handoff.chain),
        interpreter={'prefix': handoff.fake_sys.prefix, 'executable': handoff.fake_sys.executable,
        'base_executable': handoff.fake_sys._base_executable, 'argv': ['child-module', *handoff.arguments]},
        now=lambda: handoff.phase['epoch'], wait_seconds=1)
    assert path is None and status == 'unverified'
    probe = current[bridge.TAG]['probe']
    assert len(probe['attempts']) == 16 and probe['attempt_count'] > 16
    assert probe['attempts'][0]['elapsed_seconds'] == 0
    assert probe['attempts'][-1]['elapsed_seconds'] == 1
    assert all(x['reason'] == 'no original parent edge' for x in probe['attempts'])
    assert handoff.parent.read_bytes() == handoff.parent_bytes


def test_real_legacy_beacon_shape_without_rollback_metadata_uses_owned_lineage(handoff):
    path = handoff.home / '.clover-update-heartbeat.json'
    beacon = json.loads(path.read_text(encoding='utf-8'))
    for key in ('repo', 'pre_pull_sha', 'pre_update_snapshot_id'):
        beacon[key] = None
    path.write_text(json.dumps(beacon), encoding='utf-8')
    handoff.child()
    current = receipt._current
    assert current is not None
    assert current.data['pre_update']['sha'] == 'a' * 40, current.data.get('windows_dependency_handoff')
    assert current.data['windows_dependency_handoff']['state'] == 'adopted'
    assert current.data['pre_update_snapshot_id'] == 'pre-update-fixture'


def test_venv_launcher_anchor_can_precede_parent_exit_while_base_child_follows_it(handoff):
    now = handoff.phase['epoch'] - 2
    handoff.chain[:] = [
        {'pid': 202, 'parent_pid': 201, 'executable': handoff.fake_sys._base_executable,
         'created_utc': datetime.fromtimestamp(now + 2.001, timezone.utc).isoformat()},
        {'pid': 201, 'parent_pid': 101, 'executable': handoff.fake_sys.executable,
         'created_utc': datetime.fromtimestamp(now + 1.99, timezone.utc).isoformat()},
    ]
    path = handoff.home / '.clover-update-heartbeat.json'
    beacon = json.loads(path.read_text(encoding='utf-8'))
    beacon.update(updater_pid=201, updater_create_time=now + 1.99, refreshed_at=now + 1.991)
    path.write_text(json.dumps(beacon), encoding='utf-8')
    handoff.child()
    current = receipt._current
    assert current is not None
    assert current.data['pid'] == 101
    assert current.data['windows_dependency_handoff']['anchor_pid'] == 201


@pytest.mark.parametrize('handoff', [False], indirect=True)
def test_new_parent_transfers_pending_receipt_without_publishing_early_success(handoff, monkeypatch):
    monkeypatch.setattr(receipt, '_windows_handoff_child_pid', lambda: 202, raising=False)
    assert receipt.finalize_pending_update_receipt(0, stop_reason='sys.exit(0)') is None
    assert list(handoff.directory.glob('update_*.json')) == []
    pending = json.loads((handoff.directory / 'latest.json').read_text(encoding='utf-8'))
    assert pending['outcome'] == 'running'
    assert pending['finished_at'] is None
    assert pending['windows_dependency_handoff']['state'] == 'transferred'
    monkeypatch.setattr(receipt, '_windows_handoff_child_pid', lambda: 0)
    handoff.child()
    current = receipt._current
    assert current is not None
    assert current.data['pre_update']['sha'] == 'a' * 40, current.data.get('windows_dependency_handoff')
    assert current.data['pid'] == 101
    receipt.record_windows_continuation_result(healthy=True, token={
        'resume_needed': False, 'relaunched_profiles': ['default'],
        'restarted_services': [], 'relaunch_verification_result': True})
    final = receipt.finalize_pending_update_receipt(0)
    assert final is not None and final.name.endswith('_101.json')
    assert len(list(handoff.directory.glob('update_*.json'))) == 1


@pytest.mark.parametrize('verified', [True, False])
def test_real_resume_verdict_reaches_canonical_receipt(handoff, monkeypatch, verified):
    from clover_cli import gateway, update_cmd
    handoff.child()
    monkeypatch.setattr(update_cmd, '_m', lambda: SimpleNamespace(
        _is_windows=lambda: True, _refresh_windows_gateway_launchers=lambda: None,
        _verify_windows_gateway_relaunch=lambda **_kwargs: verified))
    monkeypatch.setattr(gateway, 'launch_detached_profile_gateway_restart', lambda *_args: True)
    monkeypatch.setattr(watcher, 'write_relaunch_marker', lambda *_args: None)
    token = {'resume_needed': True, 'profiles': {'default': 303}, 'unmapped': [], 'services': []}
    update_cmd._resume_windows_gateways_after_update(token)
    assert token['relaunch_verification_result'] is verified
    receipt.record_windows_continuation_result(healthy=True, token=token)
    path = receipt.finalize_pending_update_receipt(0)
    assert path is not None
    assert json.loads(path.read_text())['outcome'] == ('success' if verified else 'partial')


def test_duplicate_begin_after_completed_owned_continuation_cannot_create_second_receipt(handoff, monkeypatch):
    handoff.child()
    receipt.record_windows_continuation_result(healthy=True, token={
        'resume_needed': False, 'relaunched_profiles': ['default'], 'relaunch_verification_result': True})
    final = receipt.finalize_pending_update_receipt(0)
    before = final.read_bytes()
    monkeypatch.setattr(receipt, '_began_in_process', False)
    monkeypatch.setattr(receipt, '_written_in_process', False)
    handoff.child()
    assert receipt.finalize_pending_update_receipt(0) is None
    assert final.read_bytes() == before
    assert len(list(handoff.directory.glob('update_*.json'))) == 1
    assert receipt.warn_if_receipt_lost() is False


@pytest.mark.parametrize('fault', [
    'missing_create_time', 'foreign_owner', 'reused_owner', 'foreign_repo', 'foreign_snapshot',
    'foreign_source', 'wrong_launcher', 'wrong_argv', 'foreign_prefix', 'missing_parent_edge',
    'foreign_executable', 'stale_parent', 'ambiguous_archive', 'malformed_schema',
])
def test_unproved_handoff_never_modifies_original_receipt(handoff, fault):
    from clover_cli import windows_update_receipt_handoff as bridge
    parent = json.loads(handoff.parent_bytes)
    beacon_path = handoff.home / '.clover-update-heartbeat.json'
    beacon = json.loads(beacon_path.read_text())
    interpreter = {'prefix': handoff.fake_sys.prefix, 'executable': handoff.fake_sys.executable,
                   'base_executable': handoff.fake_sys._base_executable,
                   'argv': ['child-module', *handoff.arguments]}
    if fault == 'missing_create_time': beacon['updater_create_time'] = None
    elif fault == 'foreign_owner': beacon['updater_pid'] = 999
    elif fault == 'reused_owner': beacon['updater_create_time'] -= 10
    elif fault == 'foreign_repo': beacon['repo'] = str(handoff.repo.parent / 'foreign')
    elif fault == 'foreign_snapshot': beacon['pre_update_snapshot_id'] = 'foreign-snapshot'
    elif fault == 'foreign_source': beacon['pre_pull_sha'] = 'c' * 40
    elif fault == 'wrong_launcher': parent['argv'][0] = str(handoff.repo.parent / 'foreign/clover.exe')
    elif fault == 'wrong_argv': interpreter['argv'].append('--gateway')
    elif fault == 'foreign_prefix': interpreter['prefix'] = str(handoff.repo.parent / 'foreign')
    elif fault == 'missing_parent_edge': handoff.chain[0]['parent_pid'] = 999
    elif fault == 'foreign_executable': handoff.chain[0]['executable'] = str(handoff.repo.parent / 'foreign/python.exe')
    elif fault == 'stale_parent': parent['finished_at'] = parent['started_at']
    elif fault == 'malformed_schema': parent['schema'] = True
    elif fault == 'ambiguous_archive':
        (handoff.directory / 'update_20000101_000000_101.json').write_bytes(handoff.parent_bytes)
    payload = json.dumps(parent).encode()
    handoff.parent.write_bytes(payload)
    (handoff.directory / 'latest.json').write_bytes(payload)
    beacon_path.write_text(json.dumps(beacon))
    handoff.phase.update(pid=202, epoch=handoff.phase['epoch'] + 1)
    handoff.fake_sys.argv = interpreter['argv']
    current = receipt.UpdateReceipt().data
    path, status = bridge.resume(current, handoff.directory, handoff.repo,
        process_chain=lambda *_args, **_kwargs: copy.deepcopy(handoff.chain),
        interpreter=interpreter, now=lambda: handoff.phase['epoch'], wait_seconds=0)
    assert status == 'unverified' and path is None
    assert handoff.parent.read_bytes() == payload
    assert (handoff.directory / 'latest.json').read_bytes() == payload
    assert current['pre_update']['sha'] == 'b' * 40
    assert not (handoff.directory / 'handoff_phases').exists()


@pytest.mark.parametrize('handoff', [
    {'launcher': 'clover', 'legacy': True},
    {'launcher': 'clover', 'legacy': False},
], indirect=True)
def test_distlib_extensionless_owned_launcher_preserves_original_receipt(handoff, monkeypatch):
    """Hosted v1.5.5 console stub strips .exe from sys.argv[0]."""
    if handoff.parent is None:
        monkeypatch.setattr(receipt, '_windows_handoff_child_pid', lambda: 202)
        assert receipt.finalize_pending_update_receipt(0) is None
        monkeypatch.setattr(receipt, '_windows_handoff_child_pid', lambda: 0)
    handoff.child()
    current = receipt._current
    assert current is not None
    assert current.data['pre_update']['sha'] == 'a' * 40, current.data.get('windows_dependency_handoff')
    assert Path(current.data['argv'][0]).name == 'clover'
    receipt.record_windows_continuation_result(healthy=True, token={
        'resume_needed': False, 'relaunched_profiles': ['default'],
        'restarted_services': [], 'relaunch_verification_result': True})
    assert receipt.finalize_pending_update_receipt(0) is not None
    latest = receipt.read_latest_receipt()
    assert latest is not None and latest['outcome'] == 'success'
    assert latest['pre_update']['sha'] == 'a' * 40 and latest['post_update']['sha'] == 'b' * 40
    assert Path(latest['argv'][0]).name == 'clover'
    assert len(list(handoff.directory.glob('update_*.json'))) == 1


@pytest.mark.parametrize('handoff', [
    {'launcher': 'clover.exe.bak'}, {'launcher': 'clover.py'},
    {'launcher': 'other/clover'}, {'launcher': '../other/clover'},
], indirect=True)
def test_similar_or_other_directory_launcher_is_not_promoted(handoff):
    from clover_cli import windows_update_receipt_handoff as bridge
    handoff.phase.update(pid=202, epoch=handoff.phase['epoch'] + 1)
    current = receipt.UpdateReceipt().data
    interpreter = {'prefix': handoff.fake_sys.prefix, 'executable': handoff.fake_sys.executable,
                   'base_executable': handoff.fake_sys._base_executable,
                   'argv': list(handoff.fake_sys.argv)}
    path, status = bridge.resume(current, handoff.directory, handoff.repo,
        process_chain=lambda *_args, **_kwargs: copy.deepcopy(handoff.chain),
        interpreter=interpreter, now=lambda: handoff.phase['epoch'], wait_seconds=0)
    assert path is None and status == 'unverified'
    assert current['windows_dependency_handoff']['reason'] == 'foreign original launcher'
    assert handoff.parent.read_bytes() == handoff.parent_bytes
    assert not (handoff.directory / 'handoff_phases').exists()
