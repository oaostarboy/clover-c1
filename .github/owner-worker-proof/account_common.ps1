# Pure helpers are portable-testable; OS account/logon operations live elsewhere.
Set-StrictMode -Version Latest

function New-AccountEnvironment {
    param([hashtable]$Locations, [string]$Profile, [string]$UserName,
          [string]$Domain, [string]$Evidence, [hashtable]$Metadata = @{})
    $result = @{}
    foreach ($name in @('SystemRoot','WINDIR','COMSPEC','PATHEXT','OS',
                         'PROCESSOR_ARCHITECTURE','NUMBER_OF_PROCESSORS',
                         'ProgramFiles','ProgramFiles(x86)','CommonProgramFiles','ProgramData')) {
        if ($Locations.ContainsKey($name)) { $result[$name] = $Locations[$name] }
    }
    # No inherited PATH, profile locations, PSModulePath, Python path, or secrets.
    $result.PATH = $Locations.PATH
    $result.USERPROFILE = $Profile
    $result.HOME = $Profile
    $result.HOMEDRIVE = [IO.Path]::GetPathRoot($Profile).TrimEnd('\')
    $result.HOMEPATH = $Profile.Substring($result.HOMEDRIVE.Length)
    $result.APPDATA = Join-Path $Profile 'AppData/Roaming'
    $result.LOCALAPPDATA = Join-Path $Profile 'AppData/Local'
    $result.TEMP = Join-Path $Profile 'AppData/Local/Temp'
    $result.TMP = $result.TEMP
    $result.CLOVER_HOME = Join-Path $result.LOCALAPPDATA 'clover'
    $result.USERNAME = $UserName
    $result.USERDOMAIN = $Domain
    $result.PYTHONUTF8 = '1'
    $result.PYTHONIOENCODING = 'utf-8'
    $result.PYTHONNOUSERSITE = '1'
    # Only these non-secret CI identity values may cross the credential boundary.
    foreach ($name in @('GITHUB_ACTIONS','RUNNER_ENVIRONMENT','GITHUB_RUN_ID',
                         'GITHUB_RUN_ATTEMPT','GITHUB_SHA','CLOVER_PROOF_HELPERS')) {
        if ($Metadata.ContainsKey($name)) { $result[$name] = $Metadata[$name] }
    }
    $result.RUNNER_TEMP = Join-Path $Profile 'AppData/Local/Temp'
    return $result
}

function Assert-AccountResult {
    param($Result, [int]$ExitCode, [string]$ExpectedSid, [string]$RunnerSid,
          [string]$Profile, [string]$CloverHome)
    if ($ExitCode -ne 0 -or $null -eq $Result -or $Result.exit -ne 0 -or
        $Result.sid -ne $ExpectedSid -or $ExpectedSid -eq $RunnerSid -or
        $Result.admin -ne $false -or $Result.is_user_an_admin -ne $false -or
        $Result.admin_only_read_denied -ne $true -or $Result.private_home_write -ne $true -or
        $Result.registration_directory_ready -ne $true -or
        $Result.userprofile -ne $Profile -or $Result.home -ne $Profile -or
        $Result.appdata -ne (Join-Path $Profile 'AppData/Roaming') -or
        $Result.localappdata -ne (Join-Path $Profile 'AppData/Local') -or
        $Result.clover_home -ne $CloverHome) {
        throw 'Real-account child identity/access/profile or exit contract failed'
    }
}

# Deliberately finite and non-secret. Never inspect Message/StackTrace or serialize arbitrary Data.
function Get-AccountExceptionObservation {
    param([Exception]$Exception)
    $actual = $Exception
    $wrappers = @('System.Management.Automation.MethodInvocationException',
                  'System.Management.Automation.RuntimeException',
                  'System.Reflection.TargetInvocationException')
    for ($depth = 0; $depth -lt 4; $depth++) {
        if ($actual.GetType().FullName -notin $wrappers -or $null -eq $actual.InnerException) { break }
        $actual = $actual.InnerException
    }
    $category = switch ($actual.GetType().FullName) {
        'System.ComponentModel.Win32Exception' { 'win32' }
        'System.UnauthorizedAccessException' { 'access_denied' }
        'System.IO.IOException' { 'io' }
        'System.IO.FileNotFoundException' { 'file_not_found' }
        'System.IO.DirectoryNotFoundException' { 'directory_not_found' }
        'System.Security.SecurityException' { 'security' }
        'System.Security.Principal.IdentityNotMappedException' { 'identity_not_mapped' }
        'System.InvalidOperationException' { 'invalid_operation' }
        'System.ArgumentException' { 'argument' }
        'System.ArgumentNullException' { 'argument' }
        'System.ArgumentOutOfRangeException' { 'argument' }
        'System.Management.Automation.ItemNotFoundException' { 'item_not_found' }
        'Microsoft.Management.Infrastructure.CimException' { 'cim' }
        default { 'unknown' }
    }
    $observation = @{ exception_category=$category; hresult_signed=$null; native_code=$null; reason='exception' }
    if ($actual.GetType() -eq [InvalidOperationException] -and
        $actual.Data.Contains('CloverAccountPredicateMismatch') -and
        $actual.Data['CloverAccountPredicateMismatch'] -eq $true) {
        $observation.exception_category = 'predicate_mismatch'
        $observation.reason = 'predicate_mismatch'
    } elseif ($category -ne 'unknown') {
        $observation.hresult_signed = [int]$actual.HResult
        $unsigned = [BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$actual.HResult), 0)
        if ($actual -is [ComponentModel.Win32Exception]) {
            $observation.native_code = $actual.NativeErrorCode
        } elseif ((($unsigned -shr 16) -band 0x1fff) -eq 7) {
            $observation.native_code = $unsigned -band 0xffff
        }
    }
    return $observation
}

# DoNotVerify supplies only an OS registration, never ownership or write authority.
# Reject traversal and existing redirects before creating anything at that location.
function Test-AccountOwnedStartupLocation {
    param([string]$Startup, [string]$AppData, [string]$Profile)
    try {
        foreach ($path in @($Startup,$AppData,$Profile)) {
            if ([string]::IsNullOrWhiteSpace($path) -or -not [IO.Path]::IsPathFullyQualified($path) -or
                [IO.Path]::GetFullPath($path) -cne $path) { return $false }
        }
        if ($AppData -ne (Join-Path $Profile 'AppData/Roaming') -or
            -not $Startup.StartsWith($AppData + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { return $false }
        $cursor = $Startup
        while ($true) {
            if (Test-Path -LiteralPath $cursor -ErrorAction Stop) {
                $entry = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
                if (-not $entry.PSIsContainer -or ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return $false }
            }
            if ($cursor -eq $Profile) { break }
            $cursor = [IO.Path]::GetDirectoryName($cursor)
            if ([string]::IsNullOrEmpty($cursor)) { return $false }
        }
        return $true
    } catch { return $false }
}

# Observer only: fixed version/architecture vocabulary, never paths or environment.
function Get-AccountChildRuntimeObservation {
    $result = @{ps_version='unknown'; framework_description='unknown'; process_architecture='unknown'; os_architecture='unknown'}
    try {
        $value = $PSVersionTable.PSVersion.ToString()
        if ($value -cmatch '\A[0-9]{1,3}(\.[0-9]{1,5}){1,3}(-[A-Za-z0-9.-]{1,40})?\z') { $result.ps_version = $value }
    } catch { }
    try {
        $value = [Runtime.InteropServices.RuntimeInformation]::FrameworkDescription
        if ($value -cmatch '\A\.NET( Framework| Core)? [0-9]{1,3}(\.[0-9]{1,5}){1,3}(-[A-Za-z0-9.-]{1,40})?\z') { $result.framework_description = $value }
    } catch { }
    foreach ($key in @('process_architecture','os_architecture')) {
        try {
            $value = if ($key -eq 'process_architecture') { [Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture.ToString() } else { [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() }
            if ($value -cin @('X86','X64','Arm','Arm64','Wasm','S390x','LoongArch64','Armv6','Ppc64le','RiscV64')) { $result[$key] = $value }
        } catch { }
    }
    return $result
}

# Post-failure only; no Create flag. These later calls cannot explain the original
# managed result at the same instant. GUID verified in dotnet/runtime v10.0.0,
# Interop.SHGetKnownFolderPath.cs blob 740ea18cc5526f899088b7a2343f69dfb3d99aa0 (KnownFolders.Startup).
function Get-AccountStartupNativeObservation {
    param([ValidateSet(0,16384)][uint32]$Flags, $ValidatedStartup, $RecordedDefaultNormal,
          [string]$AppData, [string]$Profile)
    $result = @{flags=$Flags; reason='observer_unavailable'; category='unknown'; exception_category='none'; hresult_signed=$null; native_code=$null;
        path_empty=$null; owned_path=$null; equals_validated_os_path=$null; equals_recorded_default_normal=$null}
    $buffer = [IntPtr]::Zero
    try {
        if (-not ('CloverStartupDiagnosticNative' -as [type])) {
            Add-Type -ErrorAction Stop -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CloverStartupDiagnosticNative {
    [DllImport("shell32.dll", CharSet=CharSet.Unicode, ExactSpelling=true, PreserveSig=true, SetLastError=false)]
    public static extern int SHGetKnownFolderPath(ref Guid folder, uint flags, IntPtr token, out IntPtr path);
}
'@ | Out-Null
        }
        $folder = [Guid]::new('B97D20BB-F46A-4C97-BA10-5E3608430854')
        $hr = [CloverStartupDiagnosticNative]::SHGetKnownFolderPath([ref]$folder, $Flags, [IntPtr]::Zero, [ref]$buffer)
        $result.hresult_signed = [int]$hr
        $unsigned = [BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$hr), 0)
        $result.category = if ($hr -eq 0) { 's_ok' } elseif ((($unsigned -shr 16) -band 0x1fff) -eq 7) { 'win32_hresult' } else { 'other_hresult' }
        if ((($unsigned -shr 16) -band 0x1fff) -eq 7) { $result.native_code = $unsigned -band 0xffff }
        $result.reason = 'returned'
        if ($hr -eq 0) {
            $path = [Runtime.InteropServices.Marshal]::PtrToStringUni($buffer)
            $result.path_empty = [string]::IsNullOrEmpty($path)
            $result.owned_path = Test-AccountOwnedStartupLocation -Startup $path -AppData $AppData -Profile $Profile
            if ($null -ne $ValidatedStartup) { $result.equals_validated_os_path = $path -eq $ValidatedStartup }
            if ($null -ne $RecordedDefaultNormal) { $result.equals_recorded_default_normal = $path -eq $RecordedDefaultNormal }
        }
    } catch {
        $result.reason = 'observer_unavailable'
        try { $result.exception_category = (Get-AccountExceptionObservation $_.Exception).exception_category } catch { }
    } finally {
        if ($buffer -ne [IntPtr]::Zero) { try { [Runtime.InteropServices.Marshal]::FreeCoTaskMem($buffer) } catch { } }
    }
    return $result
}

function Assert-AccountChildPredicate {
    param([bool]$Condition)
    if (-not $Condition) {
        $failure = [InvalidOperationException]::new('Account child predicate mismatch')
        $failure.Data['CloverAccountPredicateMismatch'] = $true
        throw $failure
    }
}

function Set-AccountChildStage {
    param([ValidateSet('credential_isolation','identity','token','acl_denial',
        'profile_userprofile','profile_home','profile_known_userprofile','profile_appdata',
        'profile_localappdata','home_absence','home_create','home_write','home_read',
        'startup_location','startup_create','startup_write','startup_read','startup_remove',
        'access_receipt')][string]$Phase,
        [ValidateSet('entered','passed','failed','unknown')][string]$State,
        [Exception]$Exception)
    # Keep in-memory state even when the diagnostic file is unwritable. Persistence
    # is best-effort: it must not replace an original failure or gate access success.
    $script:childStage.phase = $Phase
    $script:childStage.state = $State
    $script:childStage.predicate_id = $Phase
    $script:childStage.states[$Phase] = $State
    $script:childStage.predicate_passed = if ($State -eq 'passed') { $true } else { $null }
    $script:childStage.reason = 'none'
    $script:childStage.exception_category = 'none'
    $script:childStage.hresult_signed = $null
    $script:childStage.native_code = $null
    if ($Exception) {
        $observed = Get-AccountExceptionObservation $Exception
        foreach ($key in $observed.Keys) { $script:childStage[$key] = $observed[$key] }
        if ($observed.reason -eq 'predicate_mismatch') { $script:childStage.predicate_passed = $false }
    }
    try {
        $script:childStage | ConvertTo-Json -Depth 4 |
            Set-Content -LiteralPath (Join-Path $c.evidence 'child-stage.json') -Encoding utf8 -ErrorAction Stop
    } catch { # Never serialize a diagnostic-write exception or propagate it.
    }
}

# Observation helpers do not grant deletion authority. A failed query is unknown,
# never evidence that a live process or task belongs to somebody else.
# Reserved Windows kernel roles cannot be user-created processes. Require both
# native CIM metadata and independent .NET process metadata; denial alone proves
# nothing. Unsupported/missing/changed identity stays unknown, without privilege.
function Get-AccountKernelProcessRole {
    param($Candidate)
    $native = $null
    try {
        $platform = [Environment]::OSVersion
        if (-not $IsWindows -or $platform.Platform -ne [PlatformID]::Win32NT -or $platform.Version.Major -lt 10) { return 'unknown' }
        foreach ($field in @('ProcessId','Name','SessionId','CimClass','CimSystemProperties')) {
            if (-not $Candidate.PSObject.Properties[$field] -or $null -eq $Candidate.$field) { return 'unknown' }
        }
        if ($Candidate.CimClass.CimClassName -cne 'Win32_Process' -or
            $Candidate.CimSystemProperties.Namespace -ine 'root/cimv2' -or
            $Candidate.SessionId -ne 0) { return 'unknown' }
        $role = switch ($Candidate.ProcessId) {
            0 { if ($Candidate.Name -ceq 'System Idle Process') { 'windows_reserved_idle' } else { 'unknown' } }
            4 { if ($Candidate.Name -ceq 'System') { 'windows_reserved_system' } else { 'unknown' } }
            default { 'unknown' }
        }
        if ($role -eq 'unknown') { return 'unknown' }
        $native = [Diagnostics.Process]::GetProcessById([int]$Candidate.ProcessId)
        $expectedName = if ($Candidate.ProcessId -eq 0) { 'Idle' } else { 'System' }
        if ($native.Id -ne $Candidate.ProcessId -or $native.ProcessName -cne $expectedName -or $native.SessionId -ne 0) { return 'unknown' }
        return $role
    } catch { return 'unknown' }
    finally { if ($native) { $native.Dispose() } }
}

function Get-AccountProcessOwnerObservation {
    param($Candidate)
    $result = @{ category='unknown'; return_code=$null; sid=$null; exception_category='none'; hresult_signed=$null; native_code=$null;
        classification='unknown'; classification_reason='unresolved_owner' }
    try {
        $owner = Invoke-CimMethod -InputObject $Candidate -MethodName GetOwnerSid -ErrorAction Stop
        if ($null -eq $owner) { $result.category = 'no_result' }
        else {
            $result.return_code = [uint32]$owner.ReturnValue
            $result.category = switch ($result.return_code) {
                0 { if ([string]::IsNullOrWhiteSpace($owner.Sid)) { 'missing_sid' } else { 'resolved' } }
                2 { 'access_denied' }
                3 { 'insufficient_privilege' }
                8 { 'unknown_failure' }
                9 { 'path_not_found' }
                21 { 'invalid_parameter' }
                default { 'other_return' }
            }
            if ($result.category -eq 'resolved') {
                $result.sid = $owner.Sid
                $result.classification = 'sid'; $result.classification_reason = 'owner_sid_query'
            } elseif ($result.category -eq 'access_denied') {
                $role = Get-AccountKernelProcessRole $Candidate
                if ($role -ne 'unknown') {
                    $result.classification = 'kernel_pseudo_process'; $result.classification_reason = $role
                }
            }
        }
    } catch {
        $result.category = 'query_failed'
        $errorObservation = Get-AccountExceptionObservation $_.Exception
        foreach ($key in @('exception_category','hresult_signed','native_code')) { $result[$key] = $errorObservation[$key] }
    }
    return $result
}

function Get-AccountTaskPrincipalObservation {
    param($Principal, [string]$OwnedSid)
    $result = @{ state='unknown'; sid=$null; kind='unknown'; category='missing_principal'; hresult_signed=$null; native_code=$null }
    $value = $null
    if ($Principal.PSObject.Properties['UserId'] -and -not [string]::IsNullOrWhiteSpace($Principal.UserId)) {
        $result.kind = 'user'; $value = $Principal.UserId
    } elseif ($Principal.PSObject.Properties['GroupId'] -and -not [string]::IsNullOrWhiteSpace($Principal.GroupId)) {
        $result.kind = 'group'; $value = $Principal.GroupId
    }
    if ($null -eq $value) { return $result }
    try {
        if ($value -eq $OwnedSid) { $result.sid = $OwnedSid; $result.category = 'owned_sid' }
        elseif ($value -match '^S-1-') {
            $result.sid = ([Security.Principal.SecurityIdentifier]::new($value)).Value
            $result.category = 'resolved_sid'
        } else {
            $result.sid = ([Security.Principal.NTAccount]::new($value)).Translate([Security.Principal.SecurityIdentifier]).Value
            $result.category = 'resolved_account'
        }
        $result.state = 'resolved'
    } catch {
        $observed = Get-AccountExceptionObservation $_.Exception
        $result.category = if ($observed.exception_category -eq 'identity_not_mapped') { 'identity_not_mapped' } else { 'resolution_failed' }
        $result.hresult_signed = $observed.hresult_signed
        $result.native_code = $observed.native_code
    }
    return $result
}

function Get-AccountRegistryAbsence {
    param([string]$OwnedSid)
    $result = @{ query_status='failed'; hive_sid_absent=$null; hive_classes_absent=$null;
        profile_list_absent=$null; exception_category='none'; hresult_signed=$null; native_code=$null }
    $opened = [Collections.Generic.List[IDisposable]]::new()
    try {
        # Read-only exact SID queries. No unload, deletion, privilege or ACL change.
        $hive = [Microsoft.Win32.Registry]::Users.OpenSubKey($OwnedSid, $false)
        if ($hive) { $opened.Add($hive) }
        $classes = [Microsoft.Win32.Registry]::Users.OpenSubKey($OwnedSid + '_Classes', $false)
        if ($classes) { $opened.Add($classes) }
        $profileKey = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey(
            'SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\' + $OwnedSid, $false)
        if ($profileKey) { $opened.Add($profileKey) }
        $result.hive_sid_absent = $null -eq $hive
        $result.hive_classes_absent = $null -eq $classes
        $result.profile_list_absent = $null -eq $profileKey
        $result.query_status = 'success'
    } catch {
        $observed = Get-AccountExceptionObservation $_.Exception
        foreach ($key in @('exception_category','hresult_signed','native_code')) { $result[$key] = $observed[$key] }
    } finally { foreach ($key in $opened) { $key.Dispose() } }
    return $result
}

function Invoke-AccountCleanup {
    param([scriptblock[]]$Steps, [Collections.IDictionary]$Inventory)
    $failed = @()
    $resourceNames = @('processes','tasks','profile','account')
    $index = 0
    foreach ($step in $Steps) {
        try { & $step | Out-Null } catch {
            $observed = Get-AccountExceptionObservation $_.Exception
            $failed += $observed.exception_category
            if ($Inventory -and $index -lt $resourceNames.Count) {
                $Inventory[$resourceNames[$index]].error = $observed
            }
        }
        $index++
    }
    if ($failed.Count) { throw ('Owned cleanup failed: ' + ($failed -join ',')) }
}
