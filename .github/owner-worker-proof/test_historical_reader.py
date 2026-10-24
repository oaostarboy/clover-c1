"""Historical reader source is control history, never the installed checkout."""
import ast
from pathlib import Path

HERE = Path(__file__).resolve().parent


def test_reader_archives_control_source_but_reads_installed_home_and_python():
    tree = ast.parse((HERE / 'windows_update.py').read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == 'readable_by_156']
    assert len(calls) == 1
    assert [ast.unparse(n) for n in calls[0].args] == [
        'a.source', 'home', 'python(live)', "'native-inflight'", 'scratch']


def test_driver_validates_reader_before_install_and_persists_archive_before_read():
    text = (HERE / 'windows_update.py').read_text()
    main = text[text.index('def main(a):'):]
    assert main.index('historical_reader_identity(a.source)') < main.index('bootstrap.resolve(a.uv)')
    assert main.index('historical_archive_identity(a.source, scratch)') < main.index('legacy.readable_by_156(')
    assert main.index("'historical-reader-archive.json'") < main.index('legacy.readable_by_156(')
    assert "digest(scratch / 'v156.tar') == reader_archive['archive_sha256']" in main


def test_real_frozen_reader_archives_control_bundle_and_reads_new_installed_db(tmp_path, monkeypatch):
    """Actual Git, bundled history, frozen helper, new schema, and old SQL reads.

    This is a Linux fixture check, not native update or disposal acceptance.
    """
    import hashlib
    import importlib.util
    import json
    import os
    import runpy
    import sqlite3
    import subprocess
    import sys
    import tarfile

    root = HERE.parents[1]
    def command(argv, **kwargs):
        return subprocess.run(list(map(str, argv)), check=True, capture_output=True, text=True, **kwargs)
    def git(repo, *args):
        return command(['git', '--no-replace-objects', '-C', repo, *args]).stdout.strip()
    spec = importlib.util.spec_from_file_location('reader_account', HERE / 'account_update.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pins = json.loads((HERE / 'pins.json').read_text())
    control = tmp_path / 'control'
    control.mkdir()
    git(control, 'init', '-q')
    git(control, 'fetch', '--no-tags', root, pins['target'], pins['old_callers']['1.5.6-suni'],
        module.REQUIRED_HISTORICAL_REF + ':' + module.REQUIRED_HISTORICAL_REF)
    git(control, 'checkout', '-qb', 'main', pins['target'])
    assert git(control, 'rev-parse', 'HEAD^{tree}') == pins['target_tree']
    # Exact immutable historical proof source, not a hand-rebuilt reader.
    archive = tmp_path / 'helpers.tar'
    git(root, 'archive', '--format=tar', '-o', archive, pins['historical_helpers'], '.github/native-proof')
    helpers_root = tmp_path / 'helpers'
    helpers_root.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(helpers_root, filter='data')
    helpers = helpers_root / '.github/native-proof'
    monkeypatch.syspath_prepend(str(helpers))
    legacy = runpy.run_path(str(helpers / 'legacy156_checks.py'))
    uv = tmp_path / 'uv.exe'
    uv.write_bytes(b'inert copied packaging bytes; never execute')
    request = dict(source=str(control), helpers=str(helpers), uv=str(uv), setup='zyra',
                   old=pins['old_callers']['1.5.6-suni'], candidate=pins['target'], target=pins['target'],
                   tree=pins['target_tree'], entry='terminal', turn_mode='inflight')
    request_file = tmp_path / 'request.json'
    request_file.write_text(json.dumps(request))
    profile = tmp_path / 'private-profile'
    profile.mkdir()
    config = module.prepare(request_file, profile, root)
    bundled, fixture = module.clone_bundles(config, profile, command)
    assert module.historical_reader_identity(bundled) == module.READER_PIN
    assert module.repo_identity(fixture, ['HEAD']) == config['packaging']['fixture_bundle']
    installed = tmp_path / 'installed'
    command(['git', 'clone', '--quiet', '--depth', '1', '--no-tags', '--branch', 'main', control.as_uri(), installed])
    assert git(installed, 'rev-parse', '--is-shallow-repository') == 'true'
    assert command(['git', '-C', installed, 'tag', '--list']).stdout == ''
    git(installed, 'config', 'remote.origin.fetch', '+refs/heads/main:refs/remotes/origin/main')
    # Use a genuine private Python under the installed path with read-only
    # access to already installed third-party packages. Never install into a
    # product/peer environment and never run an installer or updater here.
    live = installed / 'venv'
    command([sys.executable, '-m', 'venv', '--without-pip', live])
    live_python = live / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    sites = command([live_python, '-c', 'import site;print(site.getsitepackages()[0])']).stdout.strip()
    dependencies = [p for p in sys.path if 'site-packages' in p and Path(p).is_dir()]
    Path(sites, 'readonly-test-dependencies.pth').write_text('\n'.join(dependencies) + '\n')
    home = tmp_path / 'new-home'
    home.mkdir()
    seed = (
        "from clover_state import SessionDB;import clover_state,sys,json;"
        "assert clover_state.__file__.startswith(sys.argv[1]);"
        "db=SessionDB();db.create_session('native-inflight','cli');"
        "db.append_message('native-inflight','user','private local reader proof');"
        "db.append_message('native-inflight','assistant','private completed reply');"
        "db.close();print(json.dumps(dict(source=clover_state.__file__)))"
    )
    env = dict(os.environ, CLOVER_HOME=str(home), PYTHONPATH=str(installed), CLOVER_STATE_DB_GUARD_BYPASS='1')
    seeded = json.loads(command([live_python, '-c', seed, installed], cwd=tmp_path, env=env).stdout.strip().splitlines()[-1])
    assert git(installed, 'status', '--porcelain') == ''
    db_before = hashlib.sha256((home / 'state.db').read_bytes()).hexdigest()
    scratch = tmp_path / 'reader'
    scratch.mkdir()
    # The exact former call fails on the real released shallow/no-tag shape.
    import pytest
    with pytest.raises(subprocess.CalledProcessError) as missing:
        legacy['readable_by_156'](installed, home, live_python, 'native-inflight', scratch)
    assert missing.value.returncode == 128
    proof = module.historical_archive_identity(bundled, scratch)
    (tmp_path / 'historical-reader-before.json').write_text(json.dumps(proof, indent=2))
    result = legacy['readable_by_156'](bundled, home, live_python, 'native-inflight', scratch)
    assert result == dict(messages=2, roles=['user', 'assistant'])
    assert module.digest(scratch / 'v156.tar') == proof['archive_sha256']
    assert module.digest(scratch / 'v156-src/clover_state.py') == module.READER_PIN['reader_blobs']['clover_state.py']['sha256']
    # Independently inspect actual transitive import origins and every returned
    # column using the same old archive, interpreter, and installed database.
    inspect = (
        "import sys,json;from pathlib import Path;from clover_state import SessionDB;"
        "db=SessionDB(read_only=True);m=db.get_messages('native-inflight');"
        "mods={k:v.__file__ for k,v in sys.modules.items() if getattr(v,'__file__',None) and "
        "(k.startswith('clover_') or k=='agent' or k.startswith('agent.'))};"
        "assert all(Path(v).is_relative_to(sys.argv[1]) for v in mods.values()),mods;"
        "print(json.dumps(dict(modules=mods,messages=m)));db.close()"
    )
    audit_env = dict(env, PYTHONPATH=str(scratch / 'v156-src'))
    audit = json.loads(command([live_python, '-c', inspect, scratch / 'v156-src'], cwd=scratch, env=audit_env).stdout.strip().splitlines()[-1])
    assert len(audit['messages']) == 2
    assert audit['messages'][0]['content'] == 'private local reader proof'
    assert audit['messages'][1]['content'] == 'private completed reply'
    check = legacy['state_db_check'](home)
    assert check['quick_check'] == 'ok' and check['sessions'] == 1 and check['messages'] == 2
    with sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro', uri=True) as db:
        columns = {table: [r[1] for r in db.execute('PRAGMA table_info(' + table + ')')]
                   for table in ('sessions', 'messages')}
    assert {'session_id', 'role', 'content', 'tool_call_id'} <= set(columns['messages'])
    assert hashlib.sha256((home / 'state.db').read_bytes()).hexdigest() == db_before
    assert git(installed, 'status', '--porcelain') == ''
    assert git(installed, 'tag', '--list') == ''
    assert module.historical_reader_identity(bundled) == module.READER_PIN
    receipt = dict(scope='local-linux-fixture-only-not-native-acceptance', product=seeded,
                   installed=dict(sha=git(installed, 'rev-parse', 'HEAD'), tree=git(installed, 'rev-parse', 'HEAD^{tree}'), shallow=True),
                   archive=proof, legacy_helper_sha256=module.digest(helpers / 'legacy156_checks.py'),
                   helper_commit=pins['historical_helpers'], packaging=config['packaging'],
                   reader_result=result, import_audit=audit, columns=columns,
                   db_check=check, db_sha256_unchanged=db_before, installed_tag_count=0,
                   installed_tracked_clean=True)
    (tmp_path / 'historical-reader-result.json').write_text(json.dumps(receipt, indent=2))
