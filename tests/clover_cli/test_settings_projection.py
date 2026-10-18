"""Cold settings ownership contracts; every configuration is synthetic."""
import pytest


def test_emotions_delegates_to_settings_owner(monkeypatch):
    from clover_cli import settings_projection
    from agent.emotions_config import snapshot

    projected = {'presence': {'enabled': False}}
    monkeypatch.setattr(settings_projection, 'read_presence_status_settings', lambda: projected)
    assert snapshot() is projected


def test_unregistered_raw_reader_is_rejected(monkeypatch, tmp_path):
    from tests.clover_cli import test_config_read_guard as guard

    path = tmp_path / 'unauthorized_reader.py'
    path.write_text("path = home / 'config.yaml'\nraw = yaml.safe_load(path.read_text())\n")
    monkeypatch.setattr(guard, 'REPO_ROOT', tmp_path)
    with pytest.raises(AssertionError, match='unauthorized_reader.py:2'):
        guard.test_no_raw_config_yaml_reads_outside_owner_modules()


def test_managed_policy_overrides_only_projected_leaves(monkeypatch, tmp_path):
    from clover_cli.settings_projection import read_presence_status_settings

    user = tmp_path / 'user'
    managed = tmp_path / 'managed'
    user.mkdir()
    managed.mkdir()
    monkeypatch.setenv('CLOVER_HOME', str(user))
    monkeypatch.setenv('CLOVER_MANAGED_DIR', str(managed))
    (user / 'config.yaml').write_text('presence:\n  enabled: true\n  budget_mode: request\nmodel: synthetic-model\nprovider: openai\nsecret: ignored\n')
    (managed / 'config.yaml').write_text('presence:\n  enabled: false\n')
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = read_presence_status_settings()
    assert result['presence'] == {'enabled': False, 'budget_mode': 'request'}
    assert result['model'] == {'default': 'synthetic-model', 'provider': 'openai'}
    assert 'secret' not in result
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
