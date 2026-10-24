"""Portable lifecycle contracts; never create an account or run an installer."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile

import pytest

HERE = Path(__file__).resolve().parent


def load():
    spec = importlib.util.spec_from_file_location('account_update', HERE / 'account_update.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def request():
    return dict(source='/source', helpers='/helpers', uv='/uv', setup='zyra',
                old='a' * 40, candidate='b' * 40, target='c' * 40, tree='d' * 40,
                entry='terminal', turn_mode='inflight')


def test_request_is_exact_startup_contract_not_secret_channel():
    m = load()
    assert m.validate_request(request()) == request()
    for field, value in [('password', 'sentinel-secret'), ('setup', 'troy'),
                         ('old', 'A' * 40), ('entry', 'shell'), ('turn_mode', 'idle')]:
        bad = dict(request(), **{field: value})
        with pytest.raises(ValueError):
            m.validate_request(bad)
    for field in request():
        bad = request()
        del bad[field]
        with pytest.raises(ValueError):
            m.validate_request(bad)


def test_account_parent_and_child_enclose_full_driver_not_registration_only():
    parent = (HERE / 'disposable_account.ps1').read_text()
    child = (HERE / 'account_child.ps1').read_text()
    assert '[string]$RequestFile' in parent
    assert "'prepare'" in parent and "'update-result.json'" in parent
    assert parent.index('Assert-AccountUpdateResult') < parent.index('$passed = $true')
    access = child.index("'access.json'")
    access_passed = child.index("Set-AccountChildStage -Phase 'access_receipt' -State 'passed'")
    run = child.index("& $c.python -I (Join-Path $PSScriptRoot 'account_update.py') 'run' --config $Config")
    # Earlier helper-local catches are not the enclosing account/update catch.
    outer_catch = child.index('\n} catch {\n    $originalFailure = $_.Exception')
    assert access < access_passed < run < child.index('$exitCode = 0', run) < outer_catch
    assert child.count("'account_update.py') 'run'") == 1
    assert child.index('exit $exitCode') > outer_catch
    assert 'CLOVER_FIXTURE_ACCOUNT_SID' in (HERE / 'windows_update.py').read_text()


def test_full_driver_command_preserves_all_state_and_identity_inputs():
    m = load()
    r = request()
    argv = m.driver_command(r, Path('/private/fixture'), Path('/private/source'),
                            Path('/private/evidence'), Path('/private/uv'), Path('/private/python'))
    assert argv[:3] == ['/private/python', '-I', '/private/fixture/.github/owner-worker-proof/windows_update.py']
    for key in ('old', 'candidate', 'target', 'tree', 'entry', 'turn_mode', 'setup'):
        assert argv[argv.index('--' + key.replace('_', '-')) + 1] == r[key]
    assert argv[argv.index('--source') + 1] == '/private/source'
    assert argv[argv.index('--evidence') + 1] == '/private/evidence'
    assert 'sentinel-secret' not in ' '.join(argv)


def test_update_receipt_requires_full_acceptance_and_created_account():
    m = load()
    r = request()
    good = dict(status='PASS', target=r['target'], old=r['old'], entry=r['entry'],
                turn_mode=r['turn_mode'], actual_windows_startup=True, actual_scheduled_task=False,
                driver_restart=False, updater_owned_successor=True, stability_seconds=90,
                real_terminal_tool=True, inflight_witness=True, latest_write_state=True)
    m.check_result(good, r)
    for field in good:
        bad = dict(good)
        del bad[field]
        with pytest.raises(ValueError):
            m.check_result(bad, r)
    for field, value in [('status', 'REFUSED'), ('target', 'e' * 40), ('driver_restart', True),
                         ('updater_owned_successor', False), ('stability_seconds', 89)]:
        with pytest.raises(ValueError):
            m.check_result(dict(good, **{field: value}), r)


@pytest.mark.parametrize('extensionless_uv', [False, True])
def test_staging_is_private_exact_git_bundles_and_tool_bytes(tmp_path, monkeypatch, extensionless_uv):
    m = load()
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'clone', '--quiet', '--no-hardlinks', str(HERE.parents[1]), str(repo)], check=True)
    helpers = tmp_path / 'helpers'
    helpers.mkdir()
    (helpers / 'proof.py').write_text('# portable input\n')
    uv = tmp_path / 'uv.exe'
    uv.write_bytes(b'portable inert bytes; never executed')
    profile = tmp_path / 'profile'
    profile.mkdir()
    head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    if extensionless_uv:
        monkeypatch.setattr(m.sys, 'platform', 'win32')
    uv_input = uv.with_suffix('') if extensionless_uv else uv
    r = dict(request(), source=str(repo), helpers=str(helpers), uv=str(uv_input), old=head, candidate=head, target=head,
             tree=subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD^{tree}'], text=True).strip())
    path = tmp_path / 'request.json'
    path.write_text(json.dumps(r))
    config = m.prepare(path, profile, HERE.parents[1])
    stage = profile / 'update-inputs'
    assert Path(config['uv']).read_bytes() == uv.read_bytes()
    assert (stage / 'historical' / 'proof.py').read_bytes() == (helpers / 'proof.py').read_bytes()
    for key in ('source_bundle', 'fixture_bundle'):
        assert Path(config[key]).is_relative_to(stage)
        subprocess.run(['git', 'bundle', 'verify', config[key]], cwd=repo, check=True, capture_output=True)
    assert not (stage / '.env').exists()
    with pytest.raises(FileExistsError):
        m.prepare(path, profile, HERE.parents[1])


def good_result(r):
    return dict(status='PASS', target=r['target'], old=r['old'], entry=r['entry'],
                turn_mode=r['turn_mode'], actual_windows_startup=True, actual_scheduled_task=False,
                driver_restart=False, updater_owned_successor=True, stability_seconds=90,
                real_terminal_tool=True, inflight_witness=True, latest_write_state=True)


@pytest.mark.parametrize('driver_exit', [0, 1])
def test_account_remains_through_driver_and_failure_never_writes_acceptance(monkeypatch, tmp_path, driver_exit):
    m = load()
    profile = tmp_path / 'profile'
    home = profile / 'AppData/Local/clover'
    home.mkdir(parents=True)
    (home / 'owned-write.txt').write_text('Private non-admin home write control')
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    inputs = dict(request=request(), helper_hashes={}, helpers=str(tmp_path))
    for key in ('source_bundle', 'fixture_bundle', 'uv'):
        path = tmp_path / key
        path.write_bytes(b'Inert portable test input, never executable')
        inputs[key] = str(path)
        inputs[key + '_sha256'] = m.digest(path)
    config = dict(update=inputs, expected_sid='created-sid', profile=str(profile),
                  evidence=str(evidence), python='/machine/python')
    monkeypatch.setattr(m.sys, 'platform', 'win32')
    monkeypatch.setattr(m.os, 'environ', dict(GITHUB_ACTIONS='true', RUNNER_ENVIRONMENT='github-hosted', CLOVER_HOME=str(home)))
    calls = []
    def command(argv, **kw):
        calls.append(argv)
        assert profile.exists(), 'profile removed during the driver lifetime'
        assert kw['env']['CLOVER_FIXTURE_ACCOUNT_SID'] == 'created-sid'
        assert 'GITHUB_TOKEN' not in kw['env']
        if len(argv) > 2 and 'windows_update.py' in argv[2]:
            assert not home.exists(), 'prerequisite marker blocks actual installer'
            (evidence / 'update/result.json').write_text(json.dumps(good_result(request())))
            return subprocess.CompletedProcess(argv, driver_exit)
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(m.subprocess, 'run', command)
    if driver_exit:
        with pytest.raises(ValueError, match='driver failed'):
            m.run(config)
        assert not (evidence / 'update-result.json').exists()
    else:
        m.run(config)
        result = json.loads((evidence / 'update-result.json').read_text())
        assert result['sid'] == 'created-sid'
        assert result['driver_result']['stability_seconds'] == 90
    assert len(calls) == 5
    assert profile.exists(), 'only outer SID-bound cleanup may remove the profile'


def test_parent_receipt_handles_real_json_types_and_rejects_wrong_identity(tmp_path):
    import shutil
    pwsh = shutil.which('pwsh')
    assert pwsh, 'Portable PowerShell execution required; no skipped proof'
    r = request()
    receipt = dict(passed=True, sid='created-sid', profile='/owned/profile', request=r, driver_result=good_result(r))
    fixtures = [(receipt, False)]
    for key, value in [('sid', 'runner-sid'), ('profile', '/runner/profile'), ('passed', False), ('passed', 'true')]:
        fixtures.append((dict(receipt, **{key: value}), True))
    for key, value in [('stability_seconds', 89), ('driver_restart', True), ('status', 'REFUSED')]:
        fixtures.append((dict(receipt, driver_result=dict(receipt['driver_result'], **{key: value})), True))
    script = tmp_path / 'receipt.ps1'
    script.write_text("$ErrorActionPreference='Stop'\n. '" + str(HERE / 'account_update_contract.ps1') + "'\n" +
        "$request='" + json.dumps(r) + "' | ConvertFrom-Json\n" +
        "$fixtures='" + json.dumps(fixtures) + "' | ConvertFrom-Json\n" + """
foreach ($case in $fixtures) {
    $failed=$false
    try { Assert-AccountUpdateResult -Result $case[0] -ExpectedSid 'created-sid' -Profile '/owned/profile' -Request $request }
    catch { $failed=$true }
    if ($failed -ne $case[1]) { throw 'receipt admission mismatch' }
}
""")
    result = subprocess.run([pwsh, '-NoLogo', '-NoProfile', '-File', str(script)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
