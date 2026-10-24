"""The real released-installer wrapper must not leak its temporary uv policy.

Only native subprocess/provenance boundaries are mocked. install() and its
actual restoration finally execute for success and both installer failures.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent


@pytest.mark.parametrize('preference', [None, 'only-system', 'only-managed', 'system', 'managed'])
@pytest.mark.parametrize('outcome', ['success', 'manifest-run', 'full-run', 'manifest-invalid', 'completion-invalid'])
def test_installer_temporary_python_policy_restores_at_actual_lifetime_boundary(
    tmp_path, monkeypatch, preference, outcome,
):
    spec = importlib.util.spec_from_file_location('installer_policy_probe', HERE / 'released_installer.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('RUNNER_ENVIRONMENT', 'github-hosted')
    caller = '3b54f413376eb99f8b83bfd4a0a340d01825014d'
    evidence, scratch, home = [tmp_path / name for name in ('evidence', 'scratch', 'home')]
    for path in (evidence, scratch, home):
        path.mkdir()
    source = tmp_path / 'source'
    uv = tmp_path / 'uv.exe'
    uv.write_bytes(b'inert mocked bootstrap tool')
    env = {'KEEP': 'unchanged', 'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'original-key',
           'GIT_CONFIG_VALUE_0': 'original-value'}
    if preference is not None:
        env['UV_PYTHON_PREFERENCE'] = preference
    incoming = dict(env)
    provenance = dict(caller_sha=caller, caller_tree='f' * 40)
    monkeypatch.setattr(module, 'materialize', lambda *args: provenance)
    monkeypatch.setattr(module, 'command', lambda *args, **kwargs: ['inert-native-command'])

    def git(repo, *args):
        if args == ('diff', '--name-only', 'HEAD'):
            return ''
        if any('tree' in str(arg) for arg in args):
            return provenance['caller_tree']
        return caller

    monkeypatch.setattr(module, 'git', git)
    stages = [dict(stage=stage, ok=True, skipped=False) for stage in module.STAGES]
    completion = stages + [dict(ok=True, protocol_version=1)]
    manifest = [dict(protocol_version=1, stages=[dict(name=s) for s in module.STAGES])]
    invoked = []

    def run(argv, name, **kwargs):
        invoked.append(name)
        if name in ('released-installer-manifest', 'released-installer'):
            assert env['UV_PYTHON_PREFERENCE'] == 'only-managed'
            assert all(env[key] == value for key, value in module.origin_rewrites(scratch / 'installer-origin.git').items())
            if (outcome, name) in [('manifest-run', 'released-installer-manifest'),
                                    ('full-run', 'released-installer')]:
                raise subprocess.CalledProcessError(17, argv)
            values = manifest if name.endswith('manifest') else completion
            if (outcome, name) in [('manifest-invalid', 'released-installer-manifest'),
                                    ('completion-invalid', 'released-installer')]:
                values = []
            (evidence / (name + '.log')).write_text('\n'.join(json.dumps(v) for v in values), encoding='utf-8')
            if name == 'released-installer':
                for relative in ('venv/Scripts/python.exe', 'venv/Scripts/clover.exe'):
                    path = source / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
                (source / '.clover-bootstrap-complete').write_text(json.dumps(dict(schemaVersion=1, pinnedCommit=caller)))
                for relative in ('config.yaml', '.env', 'bin/clover.cmd'):
                    path = home / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
        elif name == 'released-installer-registration':
            # This post-installer boundary precedes any gateway/updater launch.
            assert env == incoming
            (evidence / (name + '.log')).write_text(json.dumps(dict(home=str(home), path=str(home / 'bin'))))

    if outcome == 'success':
        assert module.install(tmp_path, caller, caller, evidence, scratch, source, home, str(uv), env, run) == provenance
        assert 'released-installer-registration' in invoked
    else:
        error = subprocess.CalledProcessError if outcome.endswith('-run') else ValueError
        with pytest.raises(error):
            module.install(tmp_path, caller, caller, evidence, scratch, source, home, str(uv), env, run)
    assert env == incoming
    assert 'released-installer-manifest' in invoked
    assert ('released-installer' in invoked) == (outcome not in ('manifest-run', 'manifest-invalid'))
