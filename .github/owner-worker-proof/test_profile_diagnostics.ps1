# Portable diagnostic contracts only. NO Windows account/profile/logon/ACL proof.
param([Parameter(Mandatory=$true)][string]$EvidenceRoot,
      [string]$Source = (Join-Path $PSScriptRoot 'disposable_account.ps1'))
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$null = New-Item -ItemType Directory -Path $EvidenceRoot -Force
. (Join-Path $PSScriptRoot 'account_common.ps1')
function Assert($Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
$tokens = $null; $parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$parseErrors)
Assert ($parseErrors.Count -eq 0) 'PowerShell source parser failed'
# Compile the exact native declaration (renamed only to avoid the fake's type).
$declaration = $ast.Find({param($n) $n -is [Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Add-Type'}, $true)
$nativeDefinition = $declaration.CommandElements[-1].Value
Add-Type -TypeDefinition ($nativeDefinition.Replace('class CloverDisposableProfile', 'class CloverDisposableProfileDeclarationCheck'))
Add-Type -TypeDefinition @'
using System.Text;
public static class CloverDisposableProfile {
    public static int Result;
    public static int Calls;
    public static int CreateProfile(string sid, string name, StringBuilder path, uint length) {
        Calls++;
        path.Append("C:/portable-fake-profile");
        return Result;
    }
}
'@
$text = [IO.File]::ReadAllText($Source)
# Execute the actual native call, error handler and SID-binding guard, not a copy.
$start = $text.IndexOf('    $buffer = [Text.StringBuilder]::new(')
$end = $text.IndexOf('    Set-PrivateDirectory -Path $Evidence', $start)
Assert ($start -ge 0 -and $end -gt $start) 'Actual profile boundary not found'
$boundary = [scriptblock]::Create($text.Substring($start, $end - $start))
$sid = 'S-1-5-21-111-222-333-1001'; $name = 'clvportableowned'
$runner = [pscustomobject]@{ User = [pscustomobject]@{ Value = 'S-1-5-21-111-222-333-500' } }
$runnerIsAdmin = $true
$script:bindingMismatch = $false
function Get-CimInstance {
    param($ClassName, $Filter)
    Assert ($ClassName -eq 'Win32_UserProfile' -and $Filter -eq "SID='$sid'") 'Profile query must keep exact SID'
    [pscustomobject]@{ LocalPath = $(if ($script:bindingMismatch) {'C:/foreign'} else {'C:/portable-fake-profile'}); Special=$false }
}
# A secret in scope must not leak through native failure diagnostics or errors.
$plain = 'portable-synthetic-secret-never-record'; $secret = ConvertTo-SecureString $plain -AsPlainText -Force
$cases = @(
    @{ hr=-2147024713; hex='0x800700B7'; code=183; reason='ERROR_ALREADY_EXISTS' },
    @{ hr=-2147024891; hex='0x80070005'; code=5; reason='ERROR_ACCESS_DENIED' },
    @{ hr=-2147024809; hex='0x80070057'; code=87; reason='ERROR_INVALID_PARAMETER' },
    @{ hr=-2147467259; hex='0x80004005'; code=$null; reason='E_FAIL' },
    @{ hr=-2147483648; hex='0x80000000'; code=$null; reason='unknown' },
    @{ hr=1; hex='0x00000001'; code=$null; reason='S_FALSE' },
    @{ hr=2147483647; hex='0x7FFFFFFF'; code=$null; reason='unknown' },
    @{ hr=0; hex='0x00000000'; code=$null; reason='S_OK' }
)
foreach ($case in $cases) {
    $Evidence = Join-Path $EvidenceRoot $case.hex
    $null = New-Item -ItemType Directory -Path $Evidence -Force
    [CloverDisposableProfile]::Result = $case.hr
    $profile = $null; $caught = $null
    try { . $boundary } catch { $caught = $_.Exception.Message }
    Assert (([bool]$caught) -eq ($case.hr -ne 0)) 'Every nonzero HRESULT must still fail'
    $path = Join-Path $Evidence 'profile-creation.json'
    Assert (Test-Path -LiteralPath $path) 'Missing numeric CreateProfile diagnostic artifact'
    $raw = [IO.File]::ReadAllText($path); $d = $raw | ConvertFrom-Json
    Assert ($d.hresult_signed -eq $case.hr -and $d.hresult_hex -ceq $case.hex) 'Signed/unsigned HRESULT mismatch or overflow'
    Assert ($d.win32_code -eq $case.code -and $d.reason -ceq $case.reason) 'Unsafe HRESULT decoding'
    Assert ($d.native_call_returned -eq $true -and $d.buffer_capacity -eq 260 -and $d.buffer_length -eq 24) 'Missing native/buffer evidence'
    Assert ($d.expected_sid -eq $sid -and $d.expected_name -eq $name -and $d.caller_sid -eq $runner.User.Value -and $d.caller_is_admin -eq $true) 'Missing safe identity evidence'
    Assert ($d.state -eq $(if ($case.hr) {'native_failure'} else {'sid_bound'})) 'Failure and positive states must differ'
    Assert ($d.profile_sid_bound -eq ($case.hr -eq 0)) 'Native zero alone must not imply SID binding'
    Assert (-not $raw.Contains($plain) -and -not ([string]$caught).Contains($plain) -and -not $raw.Contains('add-mask')) 'Credential leaked'
    if ($case.hr -ne 0) { Assert ($caught.Contains($case.hex)) 'Thrown failure lost HRESULT' }
}
$Evidence = Join-Path $EvidenceRoot 'binding-mismatch'; $null = New-Item -ItemType Directory -Path $Evidence -Force
$script:bindingMismatch = $true; [CloverDisposableProfile]::Result = 0
$rejected = $false
try { . $boundary } catch { $rejected = $_.Exception.Message -eq 'OS profile is not bound to the exact created SID' }
Assert $rejected 'Zero HRESULT must still reject a mismatched CIM profile'
$d = Get-Content (Join-Path $Evidence 'profile-creation.json') -Raw | ConvertFrom-Json
Assert ($d.state -eq 'binding_rejected' -and -not $d.profile_sid_bound) 'Binding failure must stay distinct'
Assert ([CloverDisposableProfile]::Calls -eq ($cases.Count + 1)) 'Unexpected native fallback or retry'
# Execute the actual outer finally, including all cleanup steps and RESULT write.
$outerTry = $ast.Find({param($n) $n -is [Management.Automation.Language.TryStatementAst] -and $null -ne $n.Finally -and $n.Extent.Text.Contains('# A fresh random runtime secret')}, $true)
$cleanup = [scriptblock]::Create($outerTry.Finally.Extent.Text.Trim().Substring(1).TrimEnd().TrimEnd('}'))
$init = $ast.Find({param($n) $n -is [Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$cleanupInventory'}, $true)
Assert ($null -ne $init) 'Missing owned cleanup inventory initialization'
$script:cleanupMode = 'removed'; $script:accountPresent = $true; $script:accountRemoved = $false
function Get-CimInstance {
    param($ClassName, $Filter)
    if ($ClassName -eq 'Win32_Process') {
        $script:processScans++
        if ($script:cleanupMode -eq 'unknown-process' -or ($script:cleanupMode -eq 'process-remains' -and $script:processScans -eq 2)) { return [pscustomobject]@{ ProcessId=123 } }
    }
    if ($ClassName -eq 'Win32_UserProfile') {
        Assert ($Filter -eq "SID='$sid'") 'Cleanup profile SID fence changed'
        if ($script:cleanupMode -eq 'profile-refusal') { return [pscustomobject]@{ LocalPath='C:/unexpected'; Special=$false; Loaded=$false } }
        if ($script:cleanupMode -eq 'owned-profile' -and $script:profilePresent) { return [pscustomobject]@{ LocalPath=$profile; Special=$false; Loaded=$false } }
    }
}
function Invoke-CimMethod { param($InputObject, $MethodName, $ErrorAction) if ($script:cleanupMode -eq 'process-remains') { return [pscustomobject]@{ ReturnValue=0; Sid=$sid } }; return $null }
function Remove-CimInstance { param([Parameter(ValueFromPipeline=$true)]$InputObject) process { $script:profilePresent=$false } }
function Get-ScheduledTask {
    [CmdletBinding()] param($TaskName, $TaskPath)
    if ($TaskName -and $script:cleanupMode -eq 'suppressed-task-query') { Write-Error 'portable suppressed task lookup'; return }
    if ($script:taskPresent) {
        [pscustomobject]@{ TaskName='portable-owned'; TaskPath='/portable/'; Principal=[pscustomobject]@{ UserId=$(if ($script:cleanupMode -eq 'unknown-task') {'portable-unresolved'} else {$sid}) } }
    }
}
function Stop-ScheduledTask { param($InputObject, $ErrorAction) }
function Unregister-ScheduledTask { param($InputObject, [switch]$Confirm) $script:taskPresent=$false }
function Get-LocalUser {
    [CmdletBinding()] param($SID)
    Assert ($SID.Value -eq $account.SID.Value) 'Cleanup account SID fence changed'
    if ($script:cleanupMode -eq 'suppressed-account-query') { Write-Error 'portable suppressed lookup failure'; return }
    if ($script:accountPresent) { [pscustomobject]@{ Name=$name; Description=$description } }
}
function Remove-LocalUser { param([Parameter(ValueFromPipeline=$true)]$InputObject) process { $script:accountPresent=$false; $script:accountRemoved=$true } }
foreach ($mode in @('removed','profile-refusal','unknown-process','process-remains','suppressed-account-query','owned-profile','owned-task','unknown-task','suppressed-task-query','account-query-failure')) {
    $script:cleanupMode = $mode; $script:accountPresent = $true; $script:accountRemoved=$false
    $script:processScans=0
    $script:profilePresent=$true
    $script:taskPresent=$mode -in @('owned-task','unknown-task','suppressed-task-query')
    $Evidence = Join-Path $EvidenceRoot $mode; $null = New-Item -ItemType Directory -Path $Evidence -Force
    $account = [pscustomobject]@{ SID=[pscustomobject]@{ Value=$sid } }
    $description = 'Clover CI portable-owned-tag'; $profile=$null; $process=$null; $secret=$null
    if ($mode -eq 'owned-profile') { $profile = Join-Path $Evidence 'absent-owned-profile' }
    $passed=$false; $cleanupPassed=$false; $childExit=-1
    . ([scriptblock]::Create($init.Extent.Text))
    if ($mode -eq 'account-query-failure') {
        function Get-LocalUser { param($SID, $ErrorAction) throw 'portable query failure' }
    }
    $caught=$null
    try { . $cleanup } catch { $caught=$_.Exception.Message }
    $raw = Get-Content (Join-Path $Evidence 'owned-cleanup.json') -Raw
    $d = $raw | ConvertFrom-Json
    Assert ($d.owned_sid -eq $sid) 'Cleanup must record exact owned SID'
    Assert (-not $raw.Contains($plain) -and -not $raw.Contains('add-mask')) 'Cleanup credential leaked'
    $result = Get-Content (Join-Path $Evidence 'RESULT.json') -Raw | ConvertFrom-Json
    Assert (-not $result.passed) 'Failed provisioning must never become a positive receipt'
    if ($mode -eq 'removed') {
        Assert ($d.account.found -and $d.account.removed -and $d.account.verified_absent -and $d.state -eq 'verified_absent') 'Missing exact account disposal evidence'
    } elseif ($mode -eq 'profile-refusal') {
        Assert ($caught -and $script:accountRemoved -and -not $result.cleanup_passed) 'Cleanup must propagate failure and still remove owned account'
        Assert ($d.profile.found -and $null -eq $d.profile.removed -and $null -eq $d.profile.verified_absent -and $d.profile.state -eq 'failed_or_unknown') 'Failed profile snapshot falsely passed'
    } elseif ($mode -eq 'unknown-process') {
        Assert ($null -eq $d.processes.verified_absent -and $d.processes.state -eq 'unknown' -and $d.state -eq 'incomplete') 'Unknown owner snapshot falsely passed'
    } elseif ($mode -eq 'process-remains') {
        Assert ($caught -and $d.processes.found -and $null -eq $d.processes.verified_absent -and -not $result.cleanup_passed -and $script:accountRemoved) 'Final owned-process detection lost or cleanup skipped'
    } elseif ($mode -eq 'suppressed-account-query') {
        Assert ($null -eq $d.account.found -and $null -eq $d.account.verified_absent -and $d.account.state -eq 'unknown' -and $d.state -eq 'incomplete') 'Suppressed lookup failure falsely passed'
    } elseif ($mode -eq 'owned-profile') {
        Assert ($d.profile.found -and $d.profile.removed -and $d.profile.verified_absent) 'Missing profile removal readback'
    } elseif ($mode -eq 'owned-task') {
        Assert ($d.tasks.found -and $d.tasks.removed -and $d.tasks.verified_absent) 'Missing task removal readback'
    } elseif ($mode -in @('unknown-task','suppressed-task-query')) {
        Assert ($null -eq $d.tasks.verified_absent -and $d.tasks.state -eq 'unknown' -and $d.state -eq 'incomplete') 'Unknown task snapshot falsely passed'
    } else {
        Assert ($caught -and -not $result.cleanup_passed -and $null -eq $d.account.found -and $null -eq $d.account.verified_absent) 'Failed account query falsely passed'
    }
}
@{ scope='portable diagnostics only; NO Windows proof'; hresult_cases=$cases.Count; binding_negative=1; cleanup_cases=10; parser_and_native_declaration_compile=$true; passed=$true } |
    ConvertTo-Json | Set-Content (Join-Path $EvidenceRoot 'PORTABLE-RESULT.json') -Encoding utf8
Write-Output 'PASS portable diagnostics only; NO Windows proof'
