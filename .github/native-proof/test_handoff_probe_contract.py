"""Read-only allowlisted handoff probe contracts; synthetic ownership data."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).with_name('handoff_probe.py')


def probe_module():
    assert SCRIPT.is_file(), 'Windows handoff probe does not yet retain bridge ownership observations'
    spec = importlib.util.spec_from_file_location('handoff_probe_contract', SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_files_preserve_only_safe_handoff_and_receipt_facts(tmp_path):
    module = probe_module()
    home = tmp_path / 'home'
    repo = home / 'clover-c1'
    home.mkdir()
    receipt_dir = home / 'logs/update_receipts'
    receipt_dir.mkdir(parents=True)
    (home / '.clover-update-heartbeat.json').write_text(json.dumps({
        'updater_pid': 321, 'updater_create_time': 123.25, 'refreshed_at': 124.0,
        'pre_pull_sha': 'a' * 40, 'repo': str(repo), 'ready_for_probe': False,
        'pre_update_snapshot_id': 'snapshot-123', 'unrelated_secret': 'NEVER_PUBLISH_THIS'}), encoding='utf-8')
    (receipt_dir / 'latest.json').write_text(json.dumps({
        'pid': 456, 'started_at': '2026-10-15T22:00:00+00:00',
        'finished_at': None, 'outcome': 'running', 'pre_update': {'sha': 'a' * 40},
        'post_update': {}, 'argv': ['--api-key=NEVER_PUBLISH_THIS'],
        'plan': {'credentials': 'NEVER_PUBLISH_THIS'}}), encoding='utf-8')
    sample = module.sample(home, repo)
    assert sample['beacon']['updater_pid'] == 321
    assert sample['beacon']['updater_create_time'] == 123.25
    assert sample['beacon']['repo_matches_install'] is True
    assert sample['beacon']['snapshot'] == 'snapshot-123'
    assert sample['receipt']['pid'] == 456
    assert sample['receipt']['pre_sha'] == 'a' * 40
    assert 'NEVER_PUBLISH_THIS' not in json.dumps(sample)
    assert 'argv' not in sample['receipt'] and 'plan' not in sample['receipt']


def test_malformed_and_foreign_fields_do_not_echo_unsafe_payloads(tmp_path):
    module = probe_module()
    home = tmp_path / 'home'
    home.mkdir()
    path = home / '.clover-update-heartbeat.json'
    path.write_text(json.dumps({'updater_pid': True, 'updater_create_time': float('nan'),
                               'repo': 'https://user:DO_NOT_ECHO@example.invalid',
                               'pre_pull_sha': 'DO_NOT_ECHO', 'pre_update_snapshot_id': 'DO_NOT_ECHO/?token=x'}), encoding='utf-8')
    sample = module.sample(home, home / 'clover-c1')
    assert sample['beacon']['updater_pid'] is None
    assert sample['beacon']['updater_create_time'] is None
    assert sample['beacon']['repo_matches_install'] is False
    assert sample['beacon']['pre_pull_sha'] is None
    assert sample['beacon']['snapshot'] is None
    assert 'DO_NOT_ECHO' not in json.dumps(sample)
    path.write_text(json.dumps({'updater_pid': 10**100, 'updater_create_time': 10**1000}), encoding='utf-8')
    sample = module.sample(home, home / 'clover-c1')
    assert sample['beacon']['updater_pid'] is None
    assert sample['beacon']['updater_create_time'] is None
    for invalid_pid in (None, True, '123; DO_NOT_EXECUTE', -1, 10**100):
        assert module.processes(invalid_pid) == []
    path.write_text('{DO_NOT_ECHO', encoding='utf-8')
    sample = module.sample(home, home / 'clover-c1')
    assert sample['beacon']['read_error'] == 'JSONDecodeError'
    assert 'DO_NOT_ECHO' not in json.dumps(sample)


def test_actual_product_beacon_producer_preserves_snapshot_and_real_owner(tmp_path):
    from clover_cli.update_restart_watcher import write_beacon

    module = probe_module()
    home = tmp_path / 'home'
    repo = home / 'clover-c1'
    snapshot = 'pre-update-20261015T220900'
    path = write_beacon(['not-a-real-gateway', 'DO_NOT_ECHO'], clover_home=home,
                        pre_pull_sha='a' * 40, repo=str(repo), pre_update_snapshot_id=snapshot)
    raw = json.loads(path.read_text(encoding='utf-8'))
    observed = module.sample(home, repo)
    assert observed['beacon']['snapshot'] == raw['pre_update_snapshot_id'] == snapshot
    assert observed['beacon']['updater_pid'] == raw['updater_pid'] == os.getpid()
    assert observed['beacon']['updater_create_time'] == raw['updater_create_time']
    assert 'DO_NOT_ECHO' not in json.dumps(observed)


def test_probe_history_is_bounded_and_deduplicates_unchanged_observations():
    module = probe_module()
    history = []
    assert module.record(history, {'beacon': {'updater_pid': 1}}, limit=3)
    assert not module.record(history, {'beacon': {'updater_pid': 1}}, limit=3)
    for pid in range(2, 8):
        assert module.record(history, {'beacon': {'updater_pid': pid}}, limit=3)
    assert len(history) == 3
    assert history[0]['beacon']['updater_pid'] == 1
    assert history[-1]['beacon']['updater_pid'] == 7


def test_real_cli_refuses_non_hosted_scope_without_writing(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    output = tmp_path / 'observations.json'
    result = subprocess.run([sys.executable, '-I', str(SCRIPT), '--home', str(home),
                             '--repo', str(home / 'clover-c1'), '--output', str(output),
                             '--stop', str(tmp_path / 'stop')],
                            env={key: value for key, value in os.environ.items() if key not in ('GITHUB_ACTIONS', 'RUNNER_ENVIRONMENT')},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert 'Disposable hosted diagnostic scope required' in result.stderr
    assert not output.exists()
