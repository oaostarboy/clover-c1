"""Execute actual disposal finally with finite OS doubles, never native cleanup."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
PWSH = os.environ.get('TOKEN_TEST_PWSH') or shutil.which('pwsh')


@unittest.skipUnless(PWSH, 'PowerShell required for portable fixture execution')
class CleanupObservation(unittest.TestCase):
    def test_actual_finally_fences_targets_and_keeps_every_unresolved_resource_unknown(self):
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            script = Path(directory) / 'cleanup.ps1'
            script.write_text(r"""
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
. 'COMMON'
function Assert($value,$message) { if (-not $value) { throw $message } }
$source=[IO.File]::ReadAllText('SOURCE')
$init=[scriptblock]::Create($source.Substring($source.IndexOf('$cleanupInventory = '),$source.IndexOf('$childEvidence = ')-$source.IndexOf('$cleanupInventory = ')))
$begin=$source.IndexOf('    # Continue all cleanup steps')
$end=$source.IndexOf([Environment]::NewLine+'if (-not $passed -or')
# Source line endings can differ from the host's native newline.
if ($end -lt 0) { $end=$source.IndexOf("`nif (-not $passed -or") }
$finally=$source.Substring($begin,$end-$begin)
# Test-only process acquisition seam: no actual process handles or kills.
$finally=$finally.Substring(0,$finally.LastIndexOf('}')).Replace('[Diagnostics.Process]::GetProcessById($candidate.ProcessId)', '(Get-PortableProcess $candidate.ProcessId)')
$finally=[scriptblock]::Create($finally)
function Get-PortableProcess {
    param($Id)
    Assert ($Id -eq 17) 'foreign process acquisition'
    $p=[pscustomobject]@{SafeHandle=0;HasExited=$false}
    $p | Add-Member ScriptMethod Kill { $script:processKills++; if ($script:mode -ne 'process-remains') { $script:processPresent=$false } }
    $p | Add-Member ScriptMethod WaitForExit { param($limit); Assert ($limit -eq 10000) 'deadline changed'; return $script:mode -ne 'process-wait-failure' }
    $p | Add-Member ScriptMethod Dispose { }
    return $p
}
$realOwner=${function:Get-AccountProcessOwnerObservation}
$realPrincipal=${function:Get-AccountTaskPrincipalObservation}
function Get-CimInstance {
    [CmdletBinding()] param($ClassName,$Filter)
    if ($ClassName -eq 'Win32_Process') {
        if ($script:processPresent) {
            $birth=[datetime]'2026-01-01Z'
            if ($Filter -and $script:mode -eq 'process-birth-changed') { $birth=$birth.AddSeconds(1) }
            return [pscustomobject]@{ProcessId=17;CreationDate=$birth;Recheck=[bool]$Filter}
        }
        if ($script:mode -eq 'process-query-error') { throw [IO.IOException]::new('sentinel-secret') }
        if ($script:mode -in @('owner-null','owner-2','owner-3','owner-8','owner-9','owner-21','owner-other','owner-query-error','foreign-process')) {
            [pscustomobject]@{ProcessId=17;CreationDate=[datetime]'2026-01-01Z'}
        }
        return
    }
    Assert ($ClassName -eq 'Win32_UserProfile' -and $Filter -eq "SID='$sid'") 'foreign profile query'
    if ($script:mode -eq 'profile-query-error') { throw [IO.IOException]::new('sentinel-secret') }
    if ($script:profilePresent) {
        [pscustomobject]@{SID=$sid;LocalPath=$(if($script:mode -eq 'foreign-profile'){'foreign'}else{$profile});Special=$false;Loaded=$false}
    }
}
function Invoke-CimMethod {
    [CmdletBinding()] param($InputObject,$MethodName)
    Assert ($MethodName -eq 'GetOwnerSid') 'unexpected method'
    if ($script:mode -in @('owned-process','process-birth-changed','process-owner-changed','process-wait-failure','process-remains')) {
        return [pscustomobject]@{ReturnValue=0;Sid=$(if($InputObject.Recheck -and $script:mode -eq 'process-owner-changed'){'foreign-sid'}else{$sid})}
    }
    if ($script:mode -eq 'owner-query-error') { throw [IO.IOException]::new('sentinel-secret') }
    if ($script:mode -eq 'owner-null') { return }
    if ($script:mode -eq 'foreign-process') { return [pscustomobject]@{ReturnValue=0;Sid='foreign-sid'} }
    $code = switch ($script:mode) {'owner-2'{2};'owner-3'{3};'owner-8'{8};'owner-9'{9};'owner-21'{21};default{999}}
    [pscustomobject]@{ReturnValue=$code;Sid=$null}
}
function Remove-CimInstance {
    param([Parameter(ValueFromPipeline=$true)]$InputObject)
    process { Assert ($InputObject.SID -eq $sid -and $InputObject.LocalPath -eq $profile) 'foreign profile delete';$script:profilePresent=$false }
}
function Get-ScheduledTask {
    [CmdletBinding()] param()
    if ($script:mode -eq 'task-query-error') { throw [IO.IOException]::new('sentinel-secret') }
    if ($script:taskPresent) { [pscustomobject]@{TaskName='owned';TaskPath='/owned/';Principal=[pscustomobject]@{UserId=$sid;GroupId=''}} }
    if ($script:mode -eq 'principal-unresolved') { [pscustomobject]@{TaskName='foreign-untouched';TaskPath='/';Principal=[pscustomobject]@{UserId='unmapped';GroupId=''}} }
    if ($script:mode -eq 'foreign-task') { [pscustomobject]@{TaskName='foreign-untouched';TaskPath='/';Principal=[pscustomobject]@{UserId='foreign';GroupId=''}} }
}
function Get-AccountTaskPrincipalObservation {
    param($Principal,$OwnedSid)
    if ($Principal.UserId -eq 'foreign') { return @{state='resolved';sid='foreign-sid';kind='user';category='resolved_account';hresult_signed=$null;native_code=$null} }
    & $realPrincipal -Principal $Principal -OwnedSid $OwnedSid
}
function Stop-ScheduledTask { param($InputObject,$ErrorAction); Assert ($InputObject.TaskName -eq 'owned') 'foreign task stop' }
function Unregister-ScheduledTask {
    param($InputObject,[switch]$Confirm)
    Assert ($InputObject.TaskName -eq 'owned') 'foreign task delete'
    if ($script:mode -eq 'task-removal-error') { throw [IO.IOException]::new('sentinel-secret') }
    if ($script:mode -ne 'task-remains') { $script:taskPresent=$false }
}
function Get-LocalUser {
    [CmdletBinding()] param()
    if ($script:mode -eq 'account-query-error' -or ($script:mode -eq 'account-readback-error' -and -not $script:userPresent)) { throw [IO.IOException]::new('sentinel-secret') }
    # Full enumeration returns foreign resources too; no missing-SID lookup error.
    [pscustomobject]@{SID=[pscustomobject]@{Value='foreign-sid'};Name='foreign';Description='foreign'}
    if ($script:userPresent) { [pscustomobject]@{SID=[pscustomobject]@{Value=$sid};Name=$name;Description=$(if($script:mode -eq 'foreign-account'){'foreign'}else{$description})} }
}
function Remove-LocalUser {
    param([Parameter(ValueFromPipeline=$true)]$InputObject)
    process {
        Assert ($InputObject.SID.Value -eq $sid -and $InputObject.Name -eq $name -and $InputObject.Description -eq $description) 'foreign user delete'
        if ($script:mode -eq 'account-removal-error') { throw [IO.IOException]::new('sentinel-secret') }
        if ($script:mode -ne 'account-remains') { $script:userPresent=$false }
    }
}
function Get-AccountRegistryAbsence {
    param($OwnedSid)
    Assert ($OwnedSid -eq $sid) 'foreign registry target'
    @{query_status=$(if($script:mode -eq 'registry-query-error'){'failed'}else{'success'});
      hive_sid_absent=($script:mode -ne 'hive-remains');hive_classes_absent=$true;
      profile_list_absent=($script:mode -ne 'profile-list-remains');exception_category='none';hresult_signed=$null;native_code=$null}
}
$unknown=@('owner-null','owner-2','owner-3','owner-8','owner-9','owner-21','owner-other','owner-query-error','principal-unresolved','registry-query-error')
$failed=@('process-query-error','task-query-error','account-query-error','account-readback-error','profile-query-error','task-removal-error','task-remains','account-removal-error','account-remains','foreign-account','foreign-profile','hive-remains','profile-list-remains','process-birth-changed','process-owner-changed','process-wait-failure','process-remains')
$clean=@('all-clean','already-not-found','foreign-process','foreign-task','owned-task','owned-profile','owned-process')
foreach ($mode in @($unknown)+@($failed)+@($clean)) {
    $script:mode=$mode; $sid='created-sid';$name='owned';$description='Clover CI exact-tag'
    $profile='ROOT/absent-profile';$process=$null;$secret=$null;$childExit=0;$passed=$true;$cleanupPassed=$false
    $account=[pscustomobject]@{SID=[pscustomobject]@{Value=$sid}}
    $script:userPresent=$mode -ne 'already-not-found';$script:taskPresent=$mode -in @('owned-task','task-removal-error','task-remains')
    $script:processPresent=$mode -in @('owned-process','process-birth-changed','process-owner-changed','process-wait-failure','process-remains');$script:processKills=0
    $script:profilePresent=$mode -in @('owned-profile','foreign-profile')
    $Evidence=Join-Path 'ROOT' $mode; $null=New-Item -ItemType Directory -Path $Evidence
    . $init
    $caught=$false
    try { . $finally } catch { $caught=$true }
    $d=Get-Content (Join-Path $Evidence 'owned-cleanup.json') -Raw | ConvertFrom-Json
    $r=Get-Content (Join-Path $Evidence 'RESULT.json') -Raw | ConvertFrom-Json
    Assert ($d.owned_sid -eq $sid) 'owned resource identity missing'
    Assert ($r.cleanup_passed -eq ($mode -in $clean) -and $r.passed -eq ($mode -in $clean)) ('false disposal admission: '+$mode)
    Assert (($d.state -eq 'verified_absent') -eq ($mode -in $clean)) ('unknown upgraded: '+$mode)
    if ($mode -in $unknown) { Assert (-not $caught) 'observation unknown became removal error' }
    if ($mode -in $failed) { Assert $caught ('removal/query error swallowed: '+$mode) }
    if ($mode -like 'owner-*') {
        Assert ($d.processes.unresolved_owners -eq 1 -and $null -eq $d.processes.verified_absent) 'failed ownership query excluded live process'
        Assert ($d.processes.observations.Count -eq 1 -and $d.processes.observations[0].process_id -eq 17) 'lost unresolved process identity'
    }
    if ($mode -in @('process-birth-changed','process-owner-changed','foreign-process')) { Assert ($script:processKills -eq 0) 'identity guard allowed foreign or reused process kill' }
    if ($mode -eq 'owned-process') { Assert ($script:processKills -eq 1 -and $d.processes.owned_processes[0].handle_retained -and $d.processes.owned_processes[0].principal_sid -eq $sid) 'owned process lost captured identity' }
    if ($mode -eq 'already-not-found') { Assert (-not $d.account.found -and $d.account.verified_absent -and $d.account.query_status -eq 'success') 'expected not-found not separated from query failure' }
    if ($mode -eq 'account-readback-error') { Assert ($null -eq $d.account.verified_absent -and $d.account.query_status -eq 'failed') 'failed account readback passed' }
    if ($mode -eq 'principal-unresolved') { Assert ($d.tasks.unresolved_principals -eq 1 -and $null -eq $d.tasks.verified_absent) 'unmapped principal excluded' }
    if ($mode -eq 'all-clean') { Assert ($d.profile.registry.hive_sid_absent -and $d.profile.registry.hive_classes_absent -and $d.profile.registry.profile_list_absent) 'missing explicit registry proof' }
    $raw=[IO.File]::ReadAllText((Join-Path $Evidence 'owned-cleanup.json'))
    Assert (-not $raw.Contains('sentinel-secret')) 'arbitrary error leaked'
}
@{scope='portable disposal only; NO Windows proof';cases=(@($unknown)+@($failed)+@($clean)).Count;passed=$true} | ConvertTo-Json
""".replace('COMMON', str(HERE / 'account_common.ps1').replace("'", "''"))
                 .replace('SOURCE', str(HERE / 'disposable_account.ps1').replace("'", "''"))
                 .replace('ROOT', str(directory).replace("'", "''")))
            assert PWSH is not None
            result = subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(script)],
                                    capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = json.loads(result.stdout)
            self.assertTrue(summary['passed'])
            self.assertEqual(summary['cases'], 34)


    def test_actual_principal_helper_never_treats_unresolved_user_or_group_as_foreign(self):
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            script = Path(directory) / 'principal.ps1'
            script.write_text("$ErrorActionPreference='Stop'\n. '" + str(HERE / 'account_common.ps1') + "'\n" + r"""
$owned='S-1-5-21-111-222-333-1001'
@(
    Get-AccountTaskPrincipalObservation -Principal ([pscustomobject]@{UserId=$owned;GroupId=''}) -OwnedSid $owned
    Get-AccountTaskPrincipalObservation -Principal ([pscustomobject]@{UserId='';GroupId=$owned}) -OwnedSid $owned
    Get-AccountTaskPrincipalObservation -Principal ([pscustomobject]@{UserId='';GroupId=''}) -OwnedSid $owned
    Get-AccountTaskPrincipalObservation -Principal ([pscustomobject]@{UserId='S-1-malformed';GroupId=''}) -OwnedSid $owned
    Get-AccountTaskPrincipalObservation -Principal ([pscustomobject]@{UserId='';GroupId='clover-fixture-unmapped-group-71579'}) -OwnedSid $owned
) | ConvertTo-Json -Depth 5
""")
            assert PWSH is not None
            result = subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(script)],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            observations = json.loads(result.stdout)
            self.assertEqual([v['state'] for v in observations], ['resolved', 'resolved', 'unknown', 'unknown', 'unknown'])
            self.assertEqual([v['kind'] for v in observations], ['user', 'group', 'unknown', 'user', 'group'])
            self.assertEqual(observations[2]['category'], 'missing_principal')
            for value in observations[2:]:
                self.assertIsNone(value['sid'])
                self.assertIn(value['category'], {'missing_principal', 'identity_not_mapped', 'resolution_failed'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
