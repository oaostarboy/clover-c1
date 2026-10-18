"""Distribution declarations and default-off contract, not a native build claim."""
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_neko_notices_declared_as_packaged_license_files():
    metadata = tomllib.loads((ROOT/'pyproject.toml').read_text())
    files = metadata['project']['license-files']
    for path in ('third_party/neko/LICENSE', 'third_party/neko/NOTICE', 'third_party/neko/README.md'):
        assert path in files, f'missing packaged notice: {path}'
        assert (ROOT/path).is_file()
    manifest = (ROOT/'third_party/neko/README.md').read_text()
    assert 'agent/presence_state.py' in manifest


def test_candidate_import_origins_are_worktree_pinned():
    import importlib
    modules = ['agent.presence_shadow', 'agent.presence_state', 'agent.presence_initiation',
               'agent.presence_runtime', 'agent.auxiliary_client', 'agent.turn_context',
               'cron.presence', 'gateway.presence_delivery', 'gateway.presence_controls_host',
               'gateway.run', 'plugins.platforms.telegram.adapter', 'clover_state']
    for name in modules:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        assert path.is_relative_to(ROOT), f'foreign import: {name}: {path}'
        print(f'IMPORT_ORIGIN {name} {path}')


def test_presence_defaults_off_and_separate_from_proactive():
    from clover_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG.get('presence', {}).get('enabled') is False, 'missing explicit default-off control'
