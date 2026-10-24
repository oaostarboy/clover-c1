param([Parameter(Mandatory=$true)][string]$Python,
      [Parameter(Mandatory=$true)][string]$Evidence)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'account_common.ps1')
# No account operations outside this explicitly disposable CI boundary.
if (-not $IsWindows -or $env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted') {
    throw 'Disposable hosted Windows runner required'
}
$runner = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]::new($runner)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Hosted admin parent required to create the test account'
}
if (-not [IO.Path]::IsPathFullyQualified($Python) -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'One absolute existing Python interpreter required'
}
if (Test-Path -LiteralPath $Evidence) { throw 'Refuse existing account evidence root' }
$null = New-Item -ItemType Directory -Path $Evidence
$Evidence = (Resolve-Path -LiteralPath $Evidence).Path
$hostExe = (Get-Process -Id $PID).Path
$tag = [Guid]::NewGuid().ToString('N')
$name = 'clv' + $tag.Substring(0,16)
$description = 'Clover CI ' + $tag
$account = $null; $sid = $null; $profile = $null; $secret = $null; $process = $null
$childExit = -1; $passed = $false; $cleanupPassed = $false
$childEvidence = Join-Path $Evidence 'child'
$protected = Join-Path $Evidence 'admin-only.txt'

function Set-PrivateDirectory {
    param([string]$Path, [string]$OwnedSid)
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($allowed in @('S-1-5-18','S-1-5-32-544',$OwnedSid) | Select-Object -Unique) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($allowed), 'FullControl',
            'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

try {
    # A fresh random runtime secret only. Mask BEFORE any possible credential use.
    # It is never a process argument, request field, environment value or artifact.
    $bytes = [byte[]]::new(48)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $plain = [Convert]::ToBase64String($bytes) + '!aA7'
    Write-Output ('::add-mask::' + $plain)
    $secret = ConvertTo-SecureString -String $plain -AsPlainText -Force
    [Array]::Clear($bytes, 0, $bytes.Length)
    $plain = $null
    $account = New-LocalUser -Name $name -Password $secret -Description $description -AccountNeverExpires -PasswordNeverExpires
    $sid = $account.SID.Value
    if ($sid -eq $runner.User.Value) { throw 'Created account must differ from runner' }
    $users = Get-LocalGroup -SID 'S-1-5-32-545'
    if (-not (Get-LocalGroupMember -Group $users | Where-Object { $_.SID.Value -eq $sid })) {
        Add-LocalGroupMember -Group $users -Member $account
    }
    if (-not (Get-LocalGroupMember -Group $users | Where-Object { $_.SID.Value -eq $sid }) -or
        (Get-LocalGroupMember -SID 'S-1-5-32-544' | Where-Object { $_.SID.Value -eq $sid })) {
        throw 'Created account must belong to Users and not Administrators'
    }
    # Standard userenv profile provisioning; no hand-made token, policy or UAC edits.
    Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class CloverDisposableProfile {
    [DllImport("userenv.dll", CharSet=CharSet.Unicode)]
    public static extern int CreateProfile(string sid, string name, StringBuilder path, uint length);
}
'@
    $buffer = [Text.StringBuilder]::new(1024)
    $hr = [CloverDisposableProfile]::CreateProfile($sid, $name, $buffer, $buffer.Capacity)
    if ($hr -ne 0) { throw 'OS CreateProfile failed' }
    $profile = $buffer.ToString()
    $profileRecord = Get-CimInstance Win32_UserProfile -Filter "SID='$sid'"
    if (-not $profileRecord -or $profileRecord.LocalPath -ne $profile -or $profileRecord.Special) {
        throw 'OS profile is not bound to the exact created SID'
    }
    Set-PrivateDirectory -Path $Evidence -OwnedSid $sid
    Set-PrivateDirectory -Path $profile -OwnedSid $sid
    $null = New-Item -ItemType Directory -Path $childEvidence
    [IO.File]::WriteAllText($protected, 'Nonsecret private ACL denial control')
    $fileAcl = [Security.AccessControl.FileSecurity]::new()
    $fileAcl.SetAccessRuleProtection($true, $false)
    foreach ($allowed in @('S-1-5-18','S-1-5-32-544')) {
        $fileAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($allowed), 'FullControl', 'Allow'))
    }
    Set-Acl -LiteralPath $protected -AclObject $fileAcl
    [IO.File]::ReadAllText($protected) | Out-Null
    (Get-Acl -LiteralPath $protected).Sddl | Set-Content (Join-Path $Evidence 'admin-only.sddl') -Encoding utf8
    $locations = @{}
    foreach ($entry in [Environment]::GetEnvironmentVariables().GetEnumerator()) { $locations[$entry.Key] = $entry.Value }
    # Only installed machine tools, never the runner's user PATH or secret env.
    $locations.PATH = (@((Split-Path -Parent $Python), (Split-Path -Parent $hostExe),
        "$env:SystemRoot/System32", "$env:SystemRoot", "$env:SystemRoot/System32/WindowsPowerShell/v1.0",
        "$env:ProgramFiles/Git/cmd", "$env:ProgramFiles/Git/bin", "$env:ProgramFiles/nodejs") -join ';')
    $metadata = @{}
    foreach ($key in @('GITHUB_ACTIONS','RUNNER_ENVIRONMENT','GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_SHA')) {
        $metadata[$key] = [Environment]::GetEnvironmentVariable($key)
    }
    $childEnv = New-AccountEnvironment -Locations $locations -Profile $profile -UserName $name -Domain $env:COMPUTERNAME -Evidence $childEvidence -Metadata $metadata
    foreach ($folder in @($childEnv.APPDATA, $childEnv.LOCALAPPDATA, $childEnv.TEMP)) {
        $null = New-Item -ItemType Directory -Path $folder -Force
    }
    # The child creates and tests its own per-test Clover home.
    $config = @{ expected_sid=$sid; runner_sid=$runner.User.Value; profile=$profile;
        python=$Python; evidence=$childEvidence; protected=$protected }
    $configPath = Join-Path $Evidence 'request.json'
    $config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $configPath -Encoding utf8
    @{ sid=$sid; runner_sid=$runner.User.Value; name=$name; profile=$profile;
       users_member=$true; administrators_member=$false; credential_material_recorded=$false } |
        ConvertTo-Json | Set-Content (Join-Path $Evidence 'account.json') -Encoding utf8
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $hostExe
    $psi.UseShellExecute = $false
    $psi.UserName = $name
    $psi.Domain = $env:COMPUTERNAME
    $psi.Password = $secret
    $psi.LoadUserProfile = $true
    $psi.WorkingDirectory = $childEvidence
    foreach ($arg in @('-NoLogo','-NoProfile','-NonInteractive','-File',
                       (Join-Path $PSScriptRoot 'account_child.ps1'),'-Config',$configPath)) { $psi.ArgumentList.Add($arg) }
    $psi.Environment.Clear()
    foreach ($entry in $childEnv.GetEnumerator()) { $psi.Environment[$entry.Key] = $entry.Value }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $psi
    # CreateProcessWithLogonW via supported .NET credentials + LoadUserProfile.
    # Never capture/render ProcessStartInfo or a native exception with credentials.
    try { if (-not $process.Start()) { throw 'start returned false' } }
    catch { throw 'Credentialed process launch failed (details deliberately redacted)' }
    $limit = 90000
    if (-not $process.WaitForExit($limit)) { throw 'Owned account child timed out' }
    $childExit = $process.ExitCode
    $receiptPath = Join-Path $childEvidence 'access.json'
    if (-not (Test-Path -LiteralPath $receiptPath)) { throw 'No actual child receipt; never infer logon from launch success' }
    $result = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    Assert-AccountResult -Result $result -ExitCode $childExit -ExpectedSid $sid -RunnerSid $runner.User.Value -Profile $profile -CloverHome $childEnv.CLOVER_HOME
    $passed = $true
} finally {
    # Continue all cleanup steps on failure. Every target is the unique created SID.
    try {
    if ($sid) {
        Invoke-AccountCleanup -Steps @(
            {
                # Account is newly created; every process with this SID is owned.
                foreach ($candidate in Get-CimInstance Win32_Process) {
                    $owner = Invoke-CimMethod -InputObject $candidate -MethodName GetOwnerSid -ErrorAction Ignore
                    if ($owner -and $owner.ReturnValue -eq 0 -and $owner.Sid -eq $sid) {
                        $ownedProcess = $null
                        try {
                            $ownedProcess = [Diagnostics.Process]::GetProcessById($candidate.ProcessId)
                            # Retain the native handle, then recheck SID. A retained
                            # process object prevents PID reuse during the kill.
                            $null = $ownedProcess.SafeHandle
                            $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($candidate.ProcessId)"
                            if ($current) {
                                $ownerAgain = Invoke-CimMethod -InputObject $current -MethodName GetOwnerSid
                                if ($ownerAgain.ReturnValue -ne 0 -or $ownerAgain.Sid -ne $sid) { throw 'Owned process identity changed' }
                                if (-not $ownedProcess.HasExited) {
                                    $ownedProcess.Kill()
                                    if (-not $ownedProcess.WaitForExit(10000)) { throw 'Owned process remains alive' }
                                }
                            }
                        } catch [ArgumentException] {
                            # It exited before handle acquisition. A fresh SID scan
                            # below still rejects any remaining owned process.
                        } finally {
                            if ($ownedProcess) { $ownedProcess.Dispose() }
                        }
                    }
                }
                foreach ($candidate in Get-CimInstance Win32_Process) {
                    $owner = Invoke-CimMethod -InputObject $candidate -MethodName GetOwnerSid -ErrorAction Ignore
                    if ($owner -and $owner.ReturnValue -eq 0 -and $owner.Sid -eq $sid) { throw 'Owned SID process remains' }
                }
            },
            {
                # Never guess a name or sweep tasks. Check exact principal SID.
                foreach ($task in Get-ScheduledTask) {
                    $taskSid = $null
                    try { $taskSid = ([Security.Principal.NTAccount]::new($task.Principal.UserId)).Translate([Security.Principal.SecurityIdentifier]).Value }
                    catch { if ($task.Principal.UserId -eq $sid) { $taskSid = $sid } }
                    if ($taskSid -eq $sid) {
                        Stop-ScheduledTask -InputObject $task -ErrorAction SilentlyContinue
                        Unregister-ScheduledTask -InputObject $task -Confirm:$false
                        if (Get-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -ErrorAction SilentlyContinue) { throw 'Owned task remains' }
                    }
                }
            },
            {
                $record = Get-CimInstance Win32_UserProfile -Filter "SID='$sid'"
                if ($record) {
                    if (-not $profile -or $record.LocalPath -ne $profile -or $record.Special -or $record.Loaded) { throw 'Refuse unrelated or loaded profile removal' }
                    $record | Remove-CimInstance
                }
                if ((Get-CimInstance Win32_UserProfile -Filter "SID='$sid'") -or ($profile -and (Test-Path -LiteralPath $profile))) { throw 'Owned profile remains' }
            },
            {
                $owned = Get-LocalUser -SID $account.SID -ErrorAction SilentlyContinue
                if ($owned) {
                    if ($owned.Description -ne $description -or $owned.Name -ne $name) { throw 'Refuse unrelated user removal' }
                    $owned | Remove-LocalUser
                }
                if (Get-LocalUser -SID $account.SID -ErrorAction SilentlyContinue) { throw 'Owned account remains' }
            }
        )
    }
    $cleanupPassed = $true
    } finally {
        if ($process) { $process.Dispose() }
        if ($secret) { $secret.Dispose() }
        @{ passed=($passed -and $cleanupPassed); exit=$childExit; cleanup_passed=$cleanupPassed;
           account_created=($null -ne $sid); profile_created=($null -ne $profile);
           credentials_recorded=$false; installer_runs=0; update_cases=0 } |
            ConvertTo-Json | Set-Content (Join-Path $Evidence 'RESULT.json') -Encoding utf8
    }
}
if (-not $passed -or -not $cleanupPassed) { throw 'Account prerequisite did not pass' }
