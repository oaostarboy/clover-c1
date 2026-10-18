"""Differential literal-settings parity through the genuine ordinary loader."""
from copy import deepcopy
import pytest
from tests.gateway.test_emotions_recheck_config_parity import test_status_route_agrees_with_effective_config as compare


CASES = []
for key in ('default', 'model', 'name'):
    for nested in ('model', 'default', 'name'):
        for provider in ('auto', 'openai', 'anthropic'):
            config = {'model': {key: {nested: 'synthetic-model', 'provider': 'openai'}, 'provider': provider}}
            CASES.extend([(config, {}), ({'model': {'provider': 'anthropic', 'default': 'user-model'}}, config)])
for root in ('base_url', 'api_base'):
    for inner in ('base_url', 'api_base'):
        for managed in (False, True):
            config = {root: 'https://root.invalid/v1', 'model': {'provider': 'custom', 'default': 'synthetic-model', inner: 'https://inner.invalid/v1'}}
            CASES.append(({}, config) if managed else (config, {}))
for alias in ('default', 'model', 'name'):
    CASES.extend([
        ({'model': {'provider': 'openai', alias: 'synthetic-model'}}, {}),
        ({'model': {'provider': 'openai', 'default': 'synthetic-model'}}, {'model': {alias: 'managed-model'}}),
        ({'model': {'default': '', 'provider': '', alias: 'synthetic-model'}, 'provider': 'openai'}, {}),
    ])
CASES.extend([
    ({'model': 'synthetic-model', 'provider': 'openai'}, {}),
    ({'model': {'default': 'synthetic-model'}, 'provider': 'openai'}, {}),
    ({'model': {'provider': 'openai', 'default': 'synthetic-model', 'base_url': 'https://main.invalid/v1'},
      'auxiliary': {'presence': {'provider': 'anthropic', 'model': 'task-model'}}}, {}),
    ({'model': {'provider': 'openai', 'default': 'synthetic-model'}, 'auxiliary': {'presence': {'provider': 'auto'}}},
     {'presence': {'enabled': False}}),
])


@pytest.mark.parametrize('user,managed', CASES)
def test_expanded_effective_config_parity(tmp_path, monkeypatch, user, managed):
    before = deepcopy((user, managed))
    compare(tmp_path, monkeypatch, user, managed)
    assert (user, managed) == before


def test_shared_normalizer_and_snapshot_import_origins_are_source_pinned():
    import importlib
    import json
    from pathlib import Path
    import sys
    from clover_cli.config import _normalize_root_model_keys
    from clover_cli.model_config import normalize_root_model_keys
    root = Path(__file__).resolve().parents[2]
    names = ('agent.emotions', 'agent.emotions_snapshot', 'agent.emotions_config',
             'agent.workstream_origin', 'clover_cli.config', 'clover_cli.model_config')
    origins = {n: str(Path(importlib.import_module(n).__file__).resolve()) for n in names}
    assert all(Path(p).is_relative_to(root) for p in origins.values())
    assert _normalize_root_model_keys is normalize_root_model_keys
    print('FINAL_FENCE_IMPORT_ORIGINS', json.dumps({'python': sys.executable, 'modules': origins}))
