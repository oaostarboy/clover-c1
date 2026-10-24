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
$runnerIsAdmin = ([Security.Principal.WindowsPrincipal]::new($runner)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $runnerIsAdmin) {
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
# Diagnostic inventory is not an access receipt. Null means unknown; incomplete
# independent absence proof must also prevent cleanup_passed/RESULT acceptance.
$cleanupInventory = [ordered]@{
    phase='owned_cleanup'; owned_sid=$null; state='incomplete'
    processes=@{ state='not_checked'; found=$null; removed=$null; verified_absent=$null; unresolved_owners=$null;
        query_status='not_checked'; query_count=0; observed_count=0; foreign_sid_count=0; observations=@(); owned_processes=@() }
    tasks=@{ state='not_checked'; found=$null; removed=$null; verified_absent=$null; unresolved_principals=$null;
        query_status='not_checked'; query_count=0; observed_count=0; foreign_sid_count=0; observations=@(); owned_tasks=@() }
    profile=@{ state='not_checked'; found=$null; removed=$null; verified_absent=$null;
        query_status='not_checked'; query_count=0; cim_record_absent=$null; directory_absent=$null; registry=$null }
    account=@{ state='not_checked'; found=$null; removed=$null; verified_absent=$null;
        query_status='not_checked'; query_count=0; observed_count=0; owned_match_count=$null }
}
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
    $buffer = [Text.StringBuilder]::new(260)
    # Whitelisted metadata only: never serialize exceptions, credentials or PSI.
    $profileDiagnostic = [ordered]@{
        phase='create_profile'; state='calling'; expected_sid=$sid; expected_name=$name
        caller_sid=$runner.User.Value; caller_is_admin=$runnerIsAdmin
        native_call_returned=$false; hresult_signed=$null; hresult_hex=$null
        facility=$null; win32_code=$null; reason='unknown'; error_category='unknown'
        buffer_capacity=$buffer.Capacity; buffer_length=$buffer.Length; profile_sid_bound=$false
    }
    $diagnosticPath = Join-Path $Evidence 'profile-creation.json'
    $profileDiagnostic | ConvertTo-Json | Set-Content -LiteralPath $diagnosticPath -Encoding utf8
    $hr = [CloverDisposableProfile]::CreateProfile($sid, $name, $buffer, $buffer.Capacity)
    # Reinterpret the signed native int's bytes: [uint32]$hr overflows for negatives.
    $unsignedHr = [BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$hr), 0)
    $profileDiagnostic.native_call_returned = $true
    $profileDiagnostic.hresult_signed = $hr
    $profileDiagnostic.hresult_hex = '0x{0:X8}' -f $unsignedHr
    $profileDiagnostic.facility = ($unsignedHr -shr 16) -band 0x1fff
    $profileDiagnostic.buffer_length = $buffer.Length
    $profileDiagnostic.error_category = 'nonzero_hresult'
    if ($profileDiagnostic.facility -eq 7) {
        $profileDiagnostic.win32_code = $unsignedHr -band 0xffff
        $profileDiagnostic.error_category = 'win32_hresult'
    }
    # Symbols describe the returned code, NOT an inferred underlying OS cause.
    $profileDiagnostic.reason = switch ($profileDiagnostic.hresult_hex) {
        '0x00000000' { 'S_OK' }
        '0x00000001' { 'S_FALSE' }
        '0x80004005' { 'E_FAIL' }
        '0x80070005' { 'ERROR_ACCESS_DENIED' }
        '0x80070057' { 'ERROR_INVALID_PARAMETER' }
        '0x800700B7' { 'ERROR_ALREADY_EXISTS' }
        default { 'unknown' }
    }
    if ($hr -eq 0) { $profileDiagnostic.error_category = 'success' }
    $profileDiagnostic.state = if ($hr -ne 0) { 'native_failure' } else { 'binding' }
    $profileDiagnostic | ConvertTo-Json | Set-Content -LiteralPath $diagnosticPath -Encoding utf8
    if ($hr -ne 0) { throw ('OS CreateProfile failed: HRESULT {0} ({1})' -f $hr, $profileDiagnostic.hresult_hex) }
    $profile = $buffer.ToString()
    $profileRecord = Get-CimInstance Win32_UserProfile -Filter "SID='$sid'"
    if (-not $profileRecord -or $profileRecord.LocalPath -ne $profile -or $profileRecord.Special) {
        $profileDiagnostic.state = 'binding_rejected'
        $profileDiagnostic | ConvertTo-Json | Set-Content -LiteralPath $diagnosticPath -Encoding utf8
        throw 'OS profile is not bound to the exact created SID'
    }
    $profileDiagnostic.state = 'sid_bound'
    $profileDiagnostic.profile_sid_bound = $true
    $profileDiagnostic | ConvertTo-Json | Set-Content -LiteralPath $diagnosticPath -Encoding utf8
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
    $cleanupInventory.owned_sid = $sid
    try {
    if ($sid) {
        Invoke-AccountCleanup -Inventory $cleanupInventory -Steps @(
            {
                $cleanupInventory.processes.state = 'checking'
                $cleanupInventory.processes.found = $false
                $cleanupInventory.processes.removed = $false
                $cleanupInventory.processes.unresolved_owners = 0
                $cleanupInventory.processes.query_status = 'failed'
                $cleanupInventory.processes.query_count++
                # Account is newly created; every process with this SID is owned.
                $candidates = @(Get-CimInstance Win32_Process -ErrorAction Stop)
                $cleanupInventory.processes.query_status = 'success'
                foreach ($candidate in $candidates) {
                    $owner = Get-AccountProcessOwnerObservation $candidate
                    if ($owner.category -eq 'resolved' -and $owner.sid -eq $sid) {
                        $cleanupInventory.processes.found = $true
                        $ownedProcess = $null
                        try {
                            $ownedProcess = [Diagnostics.Process]::GetProcessById($candidate.ProcessId)
                            # Retain handle and compare captured birth identity before
                            # the second independent SID read. Never kill by PID alone.
                            $null = $ownedProcess.SafeHandle
                            $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($candidate.ProcessId)" -ErrorAction Stop
                            if ($current) {
                                $ownerAgain = Get-AccountProcessOwnerObservation $current
                                if ($ownerAgain.category -ne 'resolved' -or $ownerAgain.sid -ne $sid -or
                                    $null -eq $candidate.CreationDate -or $current.CreationDate -ne $candidate.CreationDate) {
                                    throw 'Owned process identity changed or is unresolved'
                                }
                                $cleanupInventory.processes.owned_processes += @{process_id=$candidate.ProcessId;
                                    creation_ticks=$candidate.CreationDate.ToUniversalTime().Ticks; handle_retained=$true; principal_sid=$sid}
                                if (-not $ownedProcess.HasExited) {
                                    $ownedProcess.Kill()
                                    if (-not $ownedProcess.WaitForExit(10000)) { throw 'Owned process remains alive' }
                                    $cleanupInventory.processes.removed = $true
                                }
                            }
                        } catch [ArgumentException] {
                            # Exit before handle acquisition: the independent final
                            # SID scan must still resolve/exclude every live process.
                        } finally {
                            if ($ownedProcess) { $ownedProcess.Dispose() }
                        }
                    }
                }
                $cleanupInventory.processes.query_count++
                $cleanupInventory.processes.query_status = 'failed'
                $candidates = @(Get-CimInstance Win32_Process -ErrorAction Stop)
                $cleanupInventory.processes.query_status = 'success'
                foreach ($candidate in $candidates) {
                    $cleanupInventory.processes.observed_count++
                    $owner = Get-AccountProcessOwnerObservation $candidate
                    if ($owner.category -ne 'resolved') {
                        $cleanupInventory.processes.unresolved_owners++
                        $cleanupInventory.processes.observations += @{
                            process_id=$candidate.ProcessId; category=$owner.category; return_code=$owner.return_code
                            exception_category=$owner.exception_category; hresult_signed=$owner.hresult_signed; native_code=$owner.native_code
                        }
                    } elseif ($owner.sid -eq $sid) {
                        $cleanupInventory.processes.found = $true
                        $cleanupInventory.processes.observations += @{process_id=$candidate.ProcessId;category='owned_sid_remaining';return_code=0}
                        throw 'Owned SID process remains'
                    } else { $cleanupInventory.processes.foreign_sid_count++ }
                }
                $cleanupInventory.processes.query_status = 'success'
                if ($cleanupInventory.processes.unresolved_owners -eq 0) {
                    $cleanupInventory.processes.verified_absent = $true
                    $cleanupInventory.processes.state = 'verified_absent'
                } else { $cleanupInventory.processes.state = 'unknown' }
            },
            {
                $cleanupInventory.tasks.state = 'checking'
                $cleanupInventory.tasks.found = $false
                $cleanupInventory.tasks.removed = $false
                $cleanupInventory.tasks.unresolved_principals = 0
                $cleanupInventory.tasks.query_status = 'failed'
                $cleanupInventory.tasks.query_count++
                # Never guess names or sweep tasks. User and group principals must
                # resolve by OS identity APIs; only the exact created SID is owned.
                $tasks = @(Get-ScheduledTask -ErrorAction Stop)
                $cleanupInventory.tasks.query_status = 'success'
                foreach ($task in $tasks) {
                    $principal = Get-AccountTaskPrincipalObservation -Principal $task.Principal -OwnedSid $sid
                    if ($principal.state -eq 'resolved' -and $principal.sid -eq $sid) {
                        $cleanupInventory.tasks.found = $true
                        $cleanupInventory.tasks.owned_tasks += @{task_name=$task.TaskName; task_path=$task.TaskPath; principal_sid=$sid}
                        Stop-ScheduledTask -InputObject $task -ErrorAction SilentlyContinue
                        Unregister-ScheduledTask -InputObject $task -Confirm:$false
                        $cleanupInventory.tasks.removed = $true
                    }
                }
                # A successful full enumeration has an unambiguous empty result;
                # targeted cmdlet not-found exceptions are not absence evidence.
                $cleanupInventory.tasks.query_count++
                $cleanupInventory.tasks.query_status = 'failed'
                $tasks = @(Get-ScheduledTask -ErrorAction Stop)
                $cleanupInventory.tasks.query_status = 'success'
                foreach ($task in $tasks) {
                    $cleanupInventory.tasks.observed_count++
                    $principal = Get-AccountTaskPrincipalObservation -Principal $task.Principal -OwnedSid $sid
                    if ($principal.state -ne 'resolved') {
                        $cleanupInventory.tasks.unresolved_principals++
                        $cleanupInventory.tasks.observations += @{kind=$principal.kind;category=$principal.category;
                            hresult_signed=$principal.hresult_signed;native_code=$principal.native_code}
                    } elseif ($principal.sid -eq $sid) { throw 'Owned task remains' }
                    else { $cleanupInventory.tasks.foreign_sid_count++ }
                }
                $cleanupInventory.tasks.query_status = 'success'
                if ($cleanupInventory.tasks.unresolved_principals -eq 0) {
                    $cleanupInventory.tasks.verified_absent = $true
                    $cleanupInventory.tasks.state = 'verified_absent'
                } else { $cleanupInventory.tasks.state = 'unknown' }
            },
            {
                $cleanupInventory.profile.state = 'checking'
                $cleanupInventory.profile.query_status = 'failed'
                $cleanupInventory.profile.query_count++
                $record = Get-CimInstance Win32_UserProfile -Filter "SID='$sid'" -ErrorAction Stop
                $cleanupInventory.profile.query_status = 'success'
                $cleanupInventory.profile.found = [bool]$record
                if ($record) {
                    if (-not $profile -or $record.LocalPath -ne $profile -or $record.Special -or $record.Loaded) { throw 'Refuse unrelated or loaded profile removal' }
                    $record | Remove-CimInstance
                }
                $cleanupInventory.profile.query_count++
                $cleanupInventory.profile.query_status = 'failed'
                $remaining = Get-CimInstance Win32_UserProfile -Filter "SID='$sid'" -ErrorAction Stop
                $cleanupInventory.profile.query_status = 'success'
                $cleanupInventory.profile.cim_record_absent = -not [bool]$remaining
                # If profile provisioning never returned a path, do not infer its
                # directory absence merely from a missing CIM record.
                if ($profile) { $cleanupInventory.profile.directory_absent = -not (Test-Path -LiteralPath $profile -ErrorAction Stop) }
                $cleanupInventory.profile.registry = Get-AccountRegistryAbsence -OwnedSid $sid
                $registry = $cleanupInventory.profile.registry
                if ($remaining -or $cleanupInventory.profile.directory_absent -eq $false -or
                    ($registry.query_status -eq 'success' -and
                     ($registry.hive_sid_absent -ne $true -or $registry.hive_classes_absent -ne $true -or $registry.profile_list_absent -ne $true))) {
                    throw 'Owned profile or SID registry resource remains'
                }
                $cleanupInventory.profile.removed = [bool]$record
                $cleanupInventory.profile.query_status = 'success'
                if ($cleanupInventory.profile.directory_absent -eq $true -and $registry.query_status -eq 'success') {
                    $cleanupInventory.profile.verified_absent = $true
                    $cleanupInventory.profile.state = 'verified_absent'
                } else { $cleanupInventory.profile.state = 'unknown' }
            },
            {
                $cleanupInventory.account.state = 'checking'
                $cleanupInventory.account.query_status = 'failed'
                $cleanupInventory.account.query_count++
                $users = @(Get-LocalUser -ErrorAction Stop)
                $cleanupInventory.account.query_status = 'success'
                $owned = @($users | Where-Object { $_.SID.Value -eq $sid })
                $cleanupInventory.account.found = $owned.Count -ne 0
                if ($owned.Count -gt 1) { throw 'Ambiguous exact SID account enumeration' }
                if ($owned.Count -eq 1) {
                    if ($owned[0].SID.Value -ne $sid -or $owned[0].Description -ne $description -or $owned[0].Name -ne $name) { throw 'Refuse unrelated user removal' }
                    $owned[0] | Remove-LocalUser
                }
                $cleanupInventory.account.query_count++
                $cleanupInventory.account.query_status = 'failed'
                $users = @(Get-LocalUser -ErrorAction Stop)
                $cleanupInventory.account.query_status = 'success'
                $cleanupInventory.account.observed_count = $users.Count
                $remaining = @($users | Where-Object { $_.SID.Value -eq $sid })
                $cleanupInventory.account.owned_match_count = $remaining.Count
                $cleanupInventory.account.query_status = 'success'
                if ($remaining.Count) { throw 'Owned account remains' }
                $cleanupInventory.account.removed = $owned.Count -eq 1
                $cleanupInventory.account.verified_absent = $true
                $cleanupInventory.account.state = 'verified_absent'
            }
        )
    }
    $cleanupPassed = $true
    } finally {
        if ($process) { $process.Dispose() }
        if ($secret) { $secret.Dispose() }
        try {
            $resources = @('processes','tasks','profile','account')
            foreach ($resource in $resources) {
                if ($cleanupInventory[$resource].state -eq 'checking') {
                    $cleanupInventory[$resource].state = 'failed_or_unknown'
                    if ($cleanupInventory[$resource].found -ne $true) { $cleanupInventory[$resource].found = $null }
                    if ($cleanupInventory[$resource].removed -ne $true) { $cleanupInventory[$resource].removed = $null }
                }
            }
            if (-not $sid) { $cleanupInventory.state = 'no_owned_sid' }
            elseif (@($resources | Where-Object { $cleanupInventory[$_].state -ne 'verified_absent' }).Count -eq 0) {
                $cleanupInventory.state = 'verified_absent'
            }
            $cleanupPassed = $cleanupPassed -and $cleanupInventory.state -eq 'verified_absent'
            $cleanupInventory | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $Evidence 'owned-cleanup.json') -Encoding utf8
        } finally {
            @{ passed=($passed -and $cleanupPassed); exit=$childExit; cleanup_passed=$cleanupPassed;
               account_created=($null -ne $sid); profile_created=($null -ne $profile);
               credentials_recorded=$false; installer_runs=0; update_cases=0 } |
                ConvertTo-Json | Set-Content (Join-Path $Evidence 'RESULT.json') -Encoding utf8
        }
    }
}
if (-not $passed -or -not $cleanupPassed) { throw 'Account prerequisite did not pass' }
