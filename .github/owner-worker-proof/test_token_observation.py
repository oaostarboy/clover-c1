"""Execute real portable PowerShell helper/launch blocks; NOT native logon proof."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
PWSH = os.environ.get('TOKEN_TEST_PWSH') or shutil.which('pwsh')


@unittest.skipUnless(PWSH, 'PowerShell required for portable helper execution')
class AccountPowerShellContracts(unittest.TestCase):
    def run_ps(self, body):
        with tempfile.TemporaryDirectory(dir=HERE) as tmp:
            script = Path(tmp) / 'test.ps1'
            script.write_text("$ErrorActionPreference='Stop'\n. '" +
                              str(HERE / 'account_common.ps1').replace("'", "''") + "'\n" + body,
                              encoding='utf-8')
            return subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-File', str(script)],
                                  capture_output=True, text=True, timeout=20, check=False)

    def test_environment_excludes_secrets_and_rebuilds_account_locations(self):
        result = self.run_ps("""
$locations = @{ SystemRoot='C:/Windows'; PATH='owned-tool-path'; USERPROFILE='runner-profile';
  HOME='runner-home'; APPDATA='runner-appdata'; PYTHONPATH='foreign'; PSModulePath='core-modules';
  GITHUB_TOKEN='sentinel-secret'; ACTIONS_RUNTIME_TOKEN='sentinel-secret'; OPENAI_API_KEY='sentinel-secret' }
$meta = @{ GITHUB_ACTIONS='true'; GITHUB_RUN_ID='123'; GITHUB_TOKEN='sentinel-secret' }
$e = New-AccountEnvironment -Locations $locations -Profile '/owned/account' -UserName 'clvtest' -Domain 'CI' -Evidence '/owned/evidence' -Metadata $meta
if ($e.HOME -ne '/owned/account' -or $e.USERPROFILE -ne '/owned/account' -or
    $e.CLOVER_HOME -ne '/owned/account/AppData/Local/clover' -or $e.APPDATA -ne '/owned/account/AppData/Roaming') { throw 'profile copied' }
foreach ($k in @('GITHUB_TOKEN','ACTIONS_RUNTIME_TOKEN','OPENAI_API_KEY','PYTHONPATH','PSModulePath')) {
  if ($e.ContainsKey($k)) { throw 'secret or foreign environment copied' }
}
if ($e.SystemRoot -ne 'C:/Windows' -or $e.GITHUB_RUN_ID -ne '123' -or $e.PATH -ne 'owned-tool-path') { throw 'required locations lost' }
'PASS'
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('sentinel-secret', result.stdout + result.stderr)

    def test_cleanup_runs_every_step_then_propagates_failure(self):
        result = self.run_ps("""
$events = [Collections.Generic.List[string]]::new()
try {
  Invoke-AccountCleanup -Steps @(
    { $events.Add('process'); throw 'sentinel-secret' },
    { $events.Add('task') }, { $events.Add('profile'); throw 'profile-error' },
    { $events.Add('account') })
  throw 'false pass'
} catch {
  if ($_.Exception.Message -eq 'false pass' -or $_.Exception.Message -like '*sentinel*') { throw }
  if (($events -join ',') -ne 'process,task,profile,account') { throw 'cleanup stopped early' }
}
'PASS'
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('sentinel-secret', result.stdout + result.stderr)

    @staticmethod
    def good():
        return dict(exit=0, sid='created-sid', admin=False, is_user_an_admin=False,
                    admin_only_read_denied=True, private_home_write=True, registration_directory_ready=True,
                    userprofile='/owned/account', home='/owned/account',
                    appdata='/owned/account/AppData/Roaming',
                    localappdata='/owned/account/AppData/Local',
                    clover_home='/owned/account/AppData/Local/clover')

    def test_receipt_requires_actual_new_identity_access_profile_and_zero_exit(self):
        good = self.good()
        values = [(good, 0, False)]
        for key, wrong in [('exit', 1), ('sid', 'runner-sid'), ('admin', True),
                           ('is_user_an_admin', True), ('admin_only_read_denied', False),
                           ('private_home_write', False), ('userprofile', 'runner-profile'),
                           ('home', 'runner-home'), ('appdata', 'runner-appdata'),
                           ('localappdata', 'runner-appdata'), ('clover_home', 'foreign')]:
            values.append((dict(good, **{key: wrong}), 0, True))
        for key in good:
            missing = good.copy()
            del missing[key]
            values.append((missing, 0, True))
        values.extend([(good, 1, True), (None, 0, True)])
        fixtures = json.dumps(values).replace("'", "''")
        result = self.run_ps("""
$fixtures = 'FIXTURES' | ConvertFrom-Json
foreach ($case in $fixtures) {
  $failed = $false
  try { Assert-AccountResult -Result $case[0] -ExitCode $case[1] -ExpectedSid 'created-sid' -RunnerSid 'runner-sid' -Profile '/owned/account' -CloverHome '/owned/account/AppData/Local/clover' }
  catch { $failed = $true }
  if ($failed -ne $case[2]) { throw 'receipt false pass or false refusal' }
}
'PASS'
""".replace('FIXTURES', fixtures))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_launch_failure_timeout_missing_receipt_and_nonzero_exit_cannot_pass(self):
        text = (HERE / 'disposable_account.ps1').read_text(encoding='utf-8')
        block = text[text.index('    try { if (-not $process.Start())'):text.index('    $passed = $true')]
        with tempfile.TemporaryDirectory(dir=HERE) as tmp:
            evidence = Path(tmp)
            (evidence / 'access.json').write_text(json.dumps(self.good()), encoding='utf-8')
            for mode in ('false', 'throw', 'timeout', 'exit1', 'missing', 'ok'):
                body = """
$sid='created-sid'; $runner=[pscustomobject]@{ User=[pscustomobject]@{ Value='runner-sid' } }
$profile='/owned/account'; $childEnv=@{ CLOVER_HOME='/owned/account/AppData/Local/clover' }
$childEvidence='EVIDENCE'; $RequestFile=$null
$process = [pscustomobject]@{ ExitCode=EXIT }
$process | Add-Member -MemberType ScriptMethod -Name Start -Value { START }
$process | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($limit); WAIT }
try {
BLOCK
  'PASS'
} catch {
  Write-Output $_.Exception.Message
  exit 1
}
"""
                body = body.replace('EVIDENCE', str(evidence).replace("'", "''"))
                body = body.replace('EXIT', '1' if mode == 'exit1' else '0')
                body = body.replace('START', "throw 'sentinel-secret'" if mode == 'throw' else ('$false' if mode == 'false' else '$true'))
                body = body.replace('WAIT', '$false' if mode == 'timeout' else '$true').replace('BLOCK', block)
                if mode == 'missing':
                    (evidence / 'access.json').unlink()
                elif not (evidence / 'access.json').exists():
                    (evidence / 'access.json').write_text(json.dumps(self.good()), encoding='utf-8')
                result = self.run_ps(body)
                with self.subTest(mode=mode):
                    self.assertEqual(result.returncode, 0 if mode == 'ok' else 1, result.stderr)
                    self.assertNotIn('sentinel-secret', result.stdout + result.stderr)
                    if mode != 'ok':
                        self.assertNotIn('PASS', result.stdout)


    def test_actual_finally_cleans_only_created_sid_and_still_removes_user_after_profile_failure(self):
        text = (HERE / 'disposable_account.ps1').read_text(encoding='utf-8')
        block = text[text.index('    # Continue all cleanup steps'):text.index('\nif (-not $passed -or')]
        # Strip the final brace of the outer finally; run its exact body with OS
        # cmdlet stubs. No native account/process API is called on this host.
        block = block.rsplit('}', 1)[0]
        for fail in (False, True):
            with tempfile.TemporaryDirectory(dir=HERE) as tmp:
                body = r"""
$sid='created-sid'; $name='clv-created'; $description='exact-description'; $profile='/never-created-profile'
$account=[pscustomobject]@{ SID=$sid }; $process=$null; $secret=$null
$Evidence='EVIDENCE'; $passed=$true; $cleanupPassed=$false; $childExit=0; $RequestFile=$null
$script:ownedTask=$true; $script:ownedProfile=$true; $script:ownedUser=$true
$script:events=[Collections.Generic.List[string]]::new()
$script:failProfile=FAILPROFILE
function Get-CimInstance {
  param($ClassName,$Filter)
  if ($ClassName -eq 'Win32_Process') { return }
  if ($ClassName -ne 'Win32_UserProfile' -or $Filter -ne "SID='created-sid'") { throw 'foreign CIM target' }
  if ($script:ownedProfile) { [pscustomobject]@{ SID='created-sid'; LocalPath='/never-created-profile'; Special=$false; Loaded=$false } }
}
function Remove-CimInstance {
  param([Parameter(ValueFromPipeline=$true)]$InputObject)
  process {
    if ($InputObject.SID -ne 'created-sid') { throw 'foreign profile delete' }
    $script:events.Add('profile')
    if ($script:failProfile) { throw 'profile blocked' }
    $script:ownedProfile=$false
  }
}
function Get-ScheduledTask {
  param($TaskName,$TaskPath,$ErrorAction)
  if ($TaskName) { if ($script:ownedTask -and $TaskName -eq 'owned') { throw 'owned task remains' }; return }
  [pscustomobject]@{ TaskName='foreign'; TaskPath='/'; Principal=[pscustomobject]@{ UserId='foreign-sid' } }
  if ($script:ownedTask) { [pscustomobject]@{ TaskName='owned'; TaskPath='/'; Principal=[pscustomobject]@{ UserId='created-sid' } } }
}
function Stop-ScheduledTask { param($InputObject,$ErrorAction); if ($InputObject.TaskName -ne 'owned') { throw 'foreign stop' }; $script:events.Add('stop-owned-task') }
function Unregister-ScheduledTask { param($InputObject,[switch]$Confirm); if ($InputObject.TaskName -ne 'owned') { throw 'foreign task delete' }; $script:ownedTask=$false; $script:events.Add('delete-owned-task') }
function Get-LocalUser { param($SID,$ErrorAction); if ($SID -ne 'created-sid') { throw 'foreign user query' }; if ($script:ownedUser) { [pscustomobject]@{ SID=$SID; Name='clv-created'; Description='exact-description' } } }
function Remove-LocalUser { param([Parameter(ValueFromPipeline=$true)]$InputObject); process { if ($InputObject.SID -ne 'created-sid') { throw 'foreign user delete' }; $script:ownedUser=$false; $script:events.Add('user') } }
$failed=$false
try {
BLOCK
} catch { $failed=$true }
if ($failed -ne $script:failProfile) { throw 'cleanup result false pass' }
if (($script:events -join ',') -ne 'stop-owned-task,delete-owned-task,profile,user') { throw 'cleanup missing or wrong target' }
$receipt=Get-Content (Join-Path $Evidence 'RESULT.json') -Raw | ConvertFrom-Json
if ($receipt.cleanup_passed -eq $script:failProfile -or $receipt.passed -eq $script:failProfile) { throw 'cleanup receipt false pass' }
'PASS'
"""
                body = body.replace('EVIDENCE', str(tmp).replace("'", "''")).replace('FAILPROFILE', '$true' if fail else '$false').replace('BLOCK', block)
                result = self.run_ps(body)
                with self.subTest(profile_failure=fail):
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipIf(os.name == 'nt', 'portable non-Windows refusal control')
    def test_actual_entry_refuses_before_any_native_side_effect(self):
        import sys
        with tempfile.TemporaryDirectory(dir=HERE) as tmp:
            target = Path(tmp) / 'must-not-exist'
            result = subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-File',
                str(HERE / 'disposable_account.ps1'), '-Python', sys.executable,
                '-Evidence', str(target)], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Disposable hosted Windows runner required', result.stderr)
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
