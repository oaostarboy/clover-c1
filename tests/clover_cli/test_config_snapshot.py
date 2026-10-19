"""Filesystem-read-only configuration, distinct from the shared-cache fast path."""
import os
from pathlib import Path

import pytest

from clover_cli import config


@pytest.mark.skipif(os.name != 'posix', reason='Secure descriptor-relative POSIX reads')
def test_snapshot_reads_existing_without_initializing(monkeypatch, tmp_path):
    home = tmp_path / 'profile'
    home.mkdir(mode=0o700)
    path = home / 'config.yaml'
    path.write_text('model:\n  provider: openai-codex\n  default: exact-model\nsecurity:\n  redact_secrets: true\n')
    path.chmod(0o600)
    monkeypatch.setenv('CLOVER_HOME', str(home))
    managed = tmp_path / 'managed'
    managed.mkdir(mode=0o700)
    monkeypatch.setenv('CLOVER_MANAGED_DIR', str(managed))
    def forbidden(*args, **kwargs):
        pytest.fail('read-only configuration attempted initialization/migration')
    monkeypatch.setattr(config, 'ensure_clover_home', forbidden)
    monkeypatch.setattr(config, 'migrate_config', forbidden)
    before = (path.read_bytes(), path.stat().st_mode, tuple(home.iterdir()), dict(os.environ))
    loader = getattr(config, 'load_config_snapshot', None)
    assert callable(loader), 'supported non-mutating snapshot loader is missing'
    result = loader()
    assert result['model']['default'] == 'exact-model'
    assert result['security']['redact_secrets'] is True
    result['model']['default'] = 'caller edit'
    assert loader()['model']['default'] == 'exact-model'
    assert (path.read_bytes(), path.stat().st_mode, tuple(home.iterdir()), dict(os.environ)) == before


@pytest.mark.parametrize('section', ['security', 'approvals', 'proactive', 'presence', 'auxiliary', 'providers'])
def test_snapshot_rejects_malformed_policy_sections(monkeypatch, tmp_path, section):
    home = tmp_path / 'profile'
    home.mkdir(mode=0o700)
    path = home / 'config.yaml'
    path.write_text(section + ': []\n')
    path.chmod(0o600)
    monkeypatch.setenv('CLOVER_HOME', str(home))
    from clover_cli.readonly_files import ReadOnlyStateError
    with pytest.raises(ReadOnlyStateError):
        config.load_config_snapshot()


def test_snapshot_rejects_duplicate_policy_keys(monkeypatch, tmp_path):
    home = tmp_path / 'profile'
    home.mkdir(mode=0o700)
    path = home / 'config.yaml'
    path.write_text('security:\n  redact_secrets: false\nsecurity:\n  redact_secrets: true\n')
    path.chmod(0o600)
    monkeypatch.setenv('CLOVER_HOME', str(home))
    from clover_cli.readonly_files import ReadOnlyStateError
    with pytest.raises(ReadOnlyStateError):
        config.load_config_snapshot()
