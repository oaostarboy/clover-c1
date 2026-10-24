"""Real offline Git transport regressions; no installer, account or executable uv."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

HERE = Path(__file__).resolve().parent


def load():
    spec = importlib.util.spec_from_file_location('account_update', HERE / 'account_update.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *map(str, args)], text=True).strip()


@pytest.fixture
def inputs(tmp_path):
    repo = tmp_path / 'origin'
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.email', 'offline@example.invalid')
    git(repo, 'config', 'user.name', 'Offline fixture')
    refs = []
    for n in range(3):
        (repo / 'input.py').write_text(f'# fixture {n}\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-qm', f'fixture {n}')
        refs.append(git(repo, 'rev-parse', 'HEAD'))
    helpers = tmp_path / 'historical'
    helpers.mkdir()
    (helpers / 'proof.py').write_text('# exact historical input\n')
    uv = tmp_path / 'uv.exe'
    uv.write_bytes(b'inert tool bytes; never execute')
    request = dict(source=str(repo), helpers=str(helpers), uv=str(uv), setup='zyra',
                   old=refs[0], candidate=refs[1], target=refs[2],
                   tree=git(repo, 'rev-parse', refs[2] + '^{tree}'),
                   entry='terminal', turn_mode='completed')
    path = tmp_path / 'request.json'
    path.write_text(json.dumps(request))
    return repo, path, request


def test_shallow_fixture_refused_before_private_staging(inputs, tmp_path):
    repo, path, request = inputs
    fixture = tmp_path / 'shallow'
    subprocess.run(['git', 'clone', '--quiet', '--depth', '1', repo.as_uri(), str(fixture)], check=True)
    assert git(fixture, 'rev-parse', '--is-shallow-repository') == 'true'
    profile = tmp_path / 'private-profile'
    profile.mkdir()
    with pytest.raises(ValueError, match='shallow'):
        load().prepare(path, profile, fixture)
    assert not (profile / 'update-inputs').exists()


@pytest.mark.parametrize('kind', ['source', 'missing-parent'])
def test_incomplete_source_or_fixture_fails_before_staging(inputs, tmp_path, kind):
    repo, path, request = inputs
    shallow = tmp_path / 'incomplete'
    subprocess.run(['git', 'clone', '--quiet', '--depth', '1', repo.as_uri(), str(shallow)], check=True)
    if kind == 'source':
        request['source'] = str(shallow)
        path.write_text(json.dumps(request))
        fixture = repo
        error = ValueError
    else:
        (shallow / '.git/shallow').unlink()
        fixture = shallow
        error = subprocess.CalledProcessError
    profile = tmp_path / 'private-profile'
    profile.mkdir()
    with pytest.raises(error):
        load().prepare(path, profile, fixture)
    assert not (profile / 'update-inputs').exists()


def test_full_staging_bundle_clone_and_pre_account_preflight(inputs, tmp_path):
    repo, path, request = inputs
    profile = tmp_path / 'private-profile'
    profile.mkdir()
    m = load()
    config = m.prepare(path, profile, repo)
    assert config['packaging']['fixture_bundle']['head'] == git(repo, 'rev-parse', 'HEAD')
    assert config['packaging']['source_bundle']['refs'][request['old']]['tree'] == git(repo, 'rev-parse', request['old'] + '^{tree}')
    m.clone_bundles(config, profile, lambda argv: subprocess.run(argv, check=True, capture_output=True))
    for name in ['source', 'fixture']:
        clone = profile / name
        assert git(clone, 'rev-parse', 'HEAD') == git(repo, 'rev-parse', 'HEAD')
        assert git(clone, 'rev-parse', 'HEAD^{tree}') == git(repo, 'rev-parse', 'HEAD^{tree}')
        assert (clone / 'input.py').read_bytes() == (repo / 'input.py').read_bytes()
        assert git(clone, 'rev-parse', '--is-shallow-repository') == 'false'
        git(clone, 'rev-list', '--objects', '--all', '--missing=error')
    controls = tmp_path / 'private-controls'
    controls.mkdir()
    result = m.preflight(path, repo, controls)
    assert result['source_bundle']['refs'][request['target']]['tree'] == request['tree']
    assert list(controls.iterdir()) == []
    assert Path(config['uv']).read_bytes() == b'inert tool bytes; never execute'
    assert config['helper_hashes'] == {'proof.py': m.digest(Path(request['helpers']) / 'proof.py')}


def test_rendered_workflow_checks_helpers_not_just_source_and_preflights_before_account():
    import yaml
    workflow = yaml.safe_load((HERE.parent / 'workflows/windows-owner-worker-proof.yml').read_text())
    steps = workflow['jobs']['updates']['steps']
    checkouts = {step['with']['path']: step for step in steps if step.get('uses', '').startswith('actions/checkout@')}
    assert checkouts['helpers']['with']['fetch-depth'] == 0
    assert checkouts['source']['with']['fetch-depth'] == 0
    # Historical checkout supplies file bytes only, not a repository bundle.
    assert 'fetch-depth' not in checkouts['historical']['with']
    runs = '\n'.join(step.get('run', '') for step in steps)
    assert runs.index("'preflight'") < runs.index('disposable_account.ps1')
    parent = (HERE / 'disposable_account.ps1').read_text()
    assert parent.index("'preflight'") < parent.index('New-LocalUser')
    assert parent.index("'preflight'") < parent.index('RandomNumberGenerator')
    assert workflow['permissions'] == {'contents': 'read'}



def test_unshallow_restores_actual_stage_bundle_clone(inputs, tmp_path):
    repo, path, request = inputs
    fixture = tmp_path / 'fixture'
    subprocess.run(['git', 'clone', '--quiet', '--depth', '1', repo.as_uri(), str(fixture)], check=True)
    subprocess.run(['git', '-C', str(fixture), 'fetch', '--unshallow'], check=True, capture_output=True)
    profile = tmp_path / 'private-profile'
    profile.mkdir()
    config = load().prepare(path, profile, fixture)
    assert config['packaging']['fixture_bundle']['head'] == git(repo, 'rev-parse', 'HEAD')
    assert git(fixture, 'rev-parse', '--is-shallow-repository') == 'false'


def test_historical_source_commit_missing_fails_before_staging(inputs, tmp_path):
    repo, path, request = inputs
    request['old'] = '0' * 40
    path.write_text(json.dumps(request))
    profile = tmp_path / 'private-profile'
    profile.mkdir()
    with pytest.raises(subprocess.CalledProcessError):
        load().prepare(path, profile, repo)
    assert not (profile / 'update-inputs').exists()


def test_preflight_cleans_private_control_on_bundle_clone_failure(inputs, tmp_path, monkeypatch):
    repo, path, request = inputs
    m = load()
    real_run = m.subprocess.run
    clones = []
    def command(argv, **kwargs):
        if argv[:3] == ['git', 'clone', '--no-hardlinks']:
            clones.append(argv)
            if 'fixture_bundle.bundle' in argv[3]:
                Path(argv[3]).write_bytes(b'corrupted bundle transport')
        return real_run(argv, **kwargs)
    monkeypatch.setattr(m.subprocess, 'run', command)
    controls = tmp_path / 'private-controls'
    controls.mkdir()
    with pytest.raises(subprocess.CalledProcessError):
        m.preflight(path, repo, controls)
    assert len(clones) == 2  # source succeeded; actual fixture clone rejected.
    assert list(controls.iterdir()) == []


def test_preflight_refuses_mismatched_target_tree(inputs, tmp_path):
    repo, path, request = inputs
    request['tree'] = '0' * 40
    path.write_text(json.dumps(request))
    controls = tmp_path / 'private-controls'
    controls.mkdir()
    with pytest.raises(ValueError, match='target tree'):
        load().preflight(path, repo, controls)
    assert list(controls.iterdir()) == []
