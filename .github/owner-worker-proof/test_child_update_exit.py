"""Execute the unchanged child catch with an inert synchronous update seam.

Portable only: reuse established native API doubles; never install or update.
"""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

HERE = Path(__file__).resolve().parent


@pytest.mark.parametrize('driver_exit,access_failure,diagnostic_failure', [
    (0, False, False), (1, False, False), (1, False, True), (1, True, False),
])
def test_single_exit_preserves_access_and_update_failure(
        monkeypatch, driver_exit, access_failure, diagnostic_failure):
    spec = importlib.util.spec_from_file_location(
        'portable_child_update_boundary', HERE / 'test_child_stage_diagnostics.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.PWSH, 'Portable PowerShell required; no skipped proof'
    source = (HERE / 'account_child.ps1').read_text(encoding='utf-8')
    assert source.count('exit $exitCode') == 1
    real_run = subprocess.run

    def run_with_inert_update(argv, **kwargs):
        script = Path(argv[-1])
        assert script.name == 'body.ps1'
        body = script.read_text(encoding='utf-8')
        marker = '$exitCode = 1'
        assert body.count(marker) == 2
        setup = r'''
$c | Add-Member -NotePropertyName update -NotePropertyValue @{}
$Config='PORTABLE_NOT_EXECUTED'
function Observe-PortableToken {
    if ($args -contains 'run') {
        $access=Get-Content (Join-Path $c.evidence 'access.json') -Raw | ConvertFrom-Json
        $stage=Get-Content (Join-Path $c.evidence 'child-stage.json') -Raw | ConvertFrom-Json
        if ($access.exit -ne 0 -or $stage.phase -ne 'access_receipt' -or $stage.state -ne 'passed') {
            throw 'Synchronous driver entered before persisted access success'
        }
        [IO.File]::WriteAllText((Join-Path $c.evidence 'portable-update-entered.txt'), 'PORTABLE_NOT_EXECUTED')
        $global:LASTEXITCODE=DRIVER_EXIT
        return
    }
    @{is_user_an_admin=$false} | ConvertTo-Json | Set-Content (Join-Path $c.evidence 'token.json')
    $global:LASTEXITCODE=0
}
'''.replace('DRIVER_EXIT', str(driver_exit))
        if diagnostic_failure:
            setup += "\n$null=New-Item -ItemType Directory -Path (Join-Path $c.evidence 'update-failure.txt')\n"
        script.write_text(body.replace(marker, setup + '\n' + marker, 1), encoding='utf-8')
        return real_run(argv, **kwargs)

    monkeypatch.setattr(module.subprocess, 'run', run_with_inert_update)
    result, receipts = module.ChildStageDiagnostics().run_child_body(
        phase='home_write' if access_failure else None, startup_case='missing')
    assert result.returncode == (1 if driver_exit or access_failure else 0), result.stdout + result.stderr
    stage = json.loads(receipts['child-stage.json'])
    if access_failure:
        assert (stage['phase'], stage['state']) == ('home_write', 'failed')
        assert 'access.json' not in receipts
        assert 'child-failure.txt' in receipts
        assert 'portable-update-entered.txt' not in receipts
        assert 'update-failure.txt' not in receipts
    else:
        assert (stage['phase'], stage['state']) == ('access_receipt', 'passed')
        assert json.loads(receipts['access.json'])['exit'] == 0
        assert receipts['portable-update-entered.txt'] == 'PORTABLE_NOT_EXECUTED'
        assert 'child-failure.txt' not in receipts
        assert ('update-failure.txt' in receipts) == bool(driver_exit and not diagnostic_failure)
