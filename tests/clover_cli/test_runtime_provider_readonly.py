"""Real normal resolver + real pool selection with synthetic persisted credentials."""
import json
import os

import pytest
import yaml

from clover_cli.config import DEFAULT_CONFIG
from clover_cli.runtime_provider import resolve_runtime_provider
from tests.agent.test_provider_security_corrected import entry

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Secure POSIX existing-state reader')


def profile(monkeypatch, tmp_path):
    home = tmp_path / 'profile'
    home.mkdir(mode=0o700)
    managed = tmp_path / 'managed'
    managed.mkdir(mode=0o700)
    monkeypatch.setenv('CLOVER_HOME', str(home))
    monkeypatch.setenv('CLOVER_MANAGED_DIR', str(managed))
    config = {'_config_version': DEFAULT_CONFIG['_config_version'],
              'model': {'provider': 'openai-codex', 'default': 'gpt-6-astra-900k'}}
    (home / 'config.yaml').write_text(yaml.safe_dump(config))
    (home / 'config.yaml').chmod(0o600)
    credential = entry('SCRIPTED-existing-owner')
    credential.id = 'SCRIPTED-persisted-row-not-account-claim'
    (home / 'auth.json').write_text(json.dumps({'version': 1, 'credential_pool': {'openai-codex': [credential.to_dict()]}}))
    (home / 'auth.json').chmod(0o600)
    return home, config, credential


def inventory(home):
    return {p.name: (p.read_bytes(), p.stat().st_mode) for p in home.iterdir()}


def test_normal_resolver_selects_exact_existing_owner_without_writes(monkeypatch, tmp_path):
    home, config, credential = profile(monkeypatch, tmp_path)
    before = inventory(home), dict(os.environ)
    from clover_cli import config as config_module
    import agent.credential_pool as pools
    def forbidden(*args, **kwargs):
        pytest.fail('read-only selection attempted ordinary initialization/auth write/refresh')
    monkeypatch.setattr(config_module, 'ensure_clover_home', forbidden)
    monkeypatch.setattr(pools, 'write_credential_pool', forbidden)
    monkeypatch.setattr(pools.CredentialPool, '_refresh_pending_entries', forbidden)
    runtime = resolve_runtime_provider(read_only=True)
    assert runtime['model'] == config['model']['default']
    assert runtime['provider'] == 'openai-codex'
    assert runtime['api_mode'] == 'codex_responses'
    assert runtime['api_key'] == credential.access_token
    assert runtime['credential_pool'].current().id == credential.id
    assert runtime['credential_pool'].select().id == credential.id
    assert (inventory(home), dict(os.environ)) == before


def test_readonly_selection_rejects_token_with_unverifiable_expiry(monkeypatch, tmp_path):
    home, _, _ = profile(monkeypatch, tmp_path)
    path = home / 'auth.json'
    store = json.loads(path.read_text())
    store['credential_pool']['openai-codex'][0]['access_token'] = 'SCRIPTED-not-a-jwt'
    path.write_text(json.dumps(store))
    from clover_cli.readonly_files import ReadOnlyStateError
    with pytest.raises(ReadOnlyStateError):
        resolve_runtime_provider(read_only=True)


@pytest.mark.parametrize('kwargs', [
    {'requested': 'openrouter'}, {'target_model': 'different-model'},
    {'explicit_api_key': 'SCRIPTED-no-override'}, {'explicit_base_url': 'https://example.invalid'},
])
def test_readonly_resolver_refuses_route_overrides(monkeypatch, tmp_path, kwargs):
    home, _, _ = profile(monkeypatch, tmp_path)
    before = inventory(home)
    from clover_cli.readonly_files import ReadOnlyStateError
    with pytest.raises(ReadOnlyStateError):
        resolve_runtime_provider(read_only=True, **kwargs)
    assert inventory(home) == before


@pytest.mark.parametrize('mutation', ['dead', 'exhausted', 'insecure_auth', 'invalid_json', 'duplicate_json', 'boolean_version', 'no_id', 'wrong_auth_type', 'no_pool'])
def test_readonly_resolver_does_not_repair_unusable_auth(monkeypatch, tmp_path, mutation):
    home, _, _ = profile(monkeypatch, tmp_path)
    auth = home / 'auth.json'
    store = json.loads(auth.read_text())
    row = store['credential_pool']['openai-codex'][0]
    if mutation in {'dead', 'exhausted'}: row['last_status'] = mutation
    elif mutation == 'no_id': row.pop('id')
    elif mutation == 'wrong_auth_type': row['auth_type'] = 'api_key'
    elif mutation == 'no_pool': store['credential_pool'] = {}
    elif mutation == 'boolean_version': store['version'] = True
    text = json.dumps(store) if mutation != 'invalid_json' else '{invalid'
    if mutation == 'duplicate_json': text = text.replace('"version": 1', '"version": 0, "version": 1')
    auth.write_text(text)
    if mutation == 'insecure_auth': auth.chmod(0o644)
    before = inventory(home)
    from clover_cli.readonly_files import ReadOnlyStateError
    with pytest.raises(ReadOnlyStateError):
        resolve_runtime_provider(read_only=True)
    assert inventory(home) == before


def test_readonly_selected_owner_rechecks_permissions_on_readback(monkeypatch, tmp_path):
    home, _, _ = profile(monkeypatch, tmp_path)
    runtime = resolve_runtime_provider(read_only=True)
    (home/'auth.json').chmod(0o644)
    from clover_cli.readonly_files import ReadOnlyStateError
    with pytest.raises(ReadOnlyStateError):
        runtime['credential_pool'].current()
