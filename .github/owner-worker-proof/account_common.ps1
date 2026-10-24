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

function Invoke-AccountCleanup {
    param([scriptblock[]]$Steps)
    $failed = @()
    foreach ($step in $Steps) {
        try { & $step | Out-Null } catch { $failed += $_.Exception.GetType().Name }
    }
    if ($failed.Count) { throw ('Owned cleanup failed: ' + ($failed -join ',')) }
}
