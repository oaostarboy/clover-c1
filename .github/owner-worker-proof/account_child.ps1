param([Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $IsWindows -or $env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted') {
    throw 'Disposable hosted Windows runner required'
}
$c = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
. (Join-Path $PSScriptRoot 'account_common.ps1')
$exitCode = 1
$script:childStage = [ordered]@{
    phase='credential_isolation'; state='unknown'; predicate_id='credential_isolation'; predicate_passed=$null
    reason='none'; exception_category='none'; hresult_signed=$null; native_code=$null
    startup=@{default_empty=$null; resolved_owned=$null; exists=$null; readback_agrees=$null}
    states=[ordered]@{
        credential_isolation='unknown'; identity='unknown'; token='unknown'; acl_denial='unknown'
        profile_userprofile='unknown'; profile_home='unknown'; profile_known_userprofile='unknown'
        profile_appdata='unknown'; profile_localappdata='unknown'; home_absence='unknown'
        home_create='unknown'; home_write='unknown'; home_read='unknown'; startup_location='unknown'
        startup_create='unknown'; startup_write='unknown'; startup_read='unknown'; startup_remove='unknown'
        access_receipt='unknown'
    }
}
try {
    Set-AccountChildStage -Phase 'credential_isolation' -State 'entered'
    foreach ($name in @('GITHUB_TOKEN','ACTIONS_RUNTIME_TOKEN','ACTIONS_ID_TOKEN_REQUEST_TOKEN',
                         'OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY')) {
        Assert-AccountChildPredicate ($null -eq [Environment]::GetEnvironmentVariable($name))
    }
    Set-AccountChildStage -Phase 'credential_isolation' -State 'passed'
    Set-AccountChildStage -Phase 'identity' -State 'entered'
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $admin = ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    Assert-AccountChildPredicate ($identity.User.Value -eq $c.expected_sid -and $identity.User.Value -ne $c.runner_sid -and -not $admin)
    Set-AccountChildStage -Phase 'identity' -State 'passed'
    Set-AccountChildStage -Phase 'token' -State 'entered'
    & $c.python -I (Join-Path $PSScriptRoot 'restricted_token.py') --evidence (Join-Path $c.evidence 'token.json') --expected-sid $c.expected_sid
    Assert-AccountChildPredicate ($LASTEXITCODE -eq 0)
    $token = Get-Content (Join-Path $c.evidence 'token.json') -Raw | ConvertFrom-Json
    Set-AccountChildStage -Phase 'token' -State 'passed'
    Set-AccountChildStage -Phase 'acl_denial' -State 'entered'
    $denied = $false
    try { [IO.File]::ReadAllText($c.protected) | Out-Null }
    catch [UnauthorizedAccessException] { $denied = $true }
    Assert-AccountChildPredicate $denied
    Set-AccountChildStage -Phase 'acl_denial' -State 'passed'
    Set-AccountChildStage -Phase 'profile_userprofile' -State 'entered'
    Assert-AccountChildPredicate ($env:USERPROFILE -eq $c.profile)
    Set-AccountChildStage -Phase 'profile_userprofile' -State 'passed'
    Set-AccountChildStage -Phase 'profile_home' -State 'entered'
    Assert-AccountChildPredicate ($env:HOME -eq $c.profile)
    Set-AccountChildStage -Phase 'profile_home' -State 'passed'
    Set-AccountChildStage -Phase 'profile_known_userprofile' -State 'entered'
    Assert-AccountChildPredicate ([Environment]::GetFolderPath('UserProfile') -eq $c.profile)
    Set-AccountChildStage -Phase 'profile_known_userprofile' -State 'passed'
    Set-AccountChildStage -Phase 'profile_appdata' -State 'entered'
    Assert-AccountChildPredicate ([Environment]::GetFolderPath('ApplicationData') -eq $env:APPDATA)
    Set-AccountChildStage -Phase 'profile_appdata' -State 'passed'
    Set-AccountChildStage -Phase 'profile_localappdata' -State 'entered'
    Assert-AccountChildPredicate ([Environment]::GetFolderPath('LocalApplicationData') -eq $env:LOCALAPPDATA)
    Set-AccountChildStage -Phase 'profile_localappdata' -State 'passed'
    Set-AccountChildStage -Phase 'home_absence' -State 'entered'
    Assert-AccountChildPredicate (-not (Test-Path -LiteralPath $env:CLOVER_HOME))
    Set-AccountChildStage -Phase 'home_absence' -State 'passed'
    Set-AccountChildStage -Phase 'home_create' -State 'entered'
    $null = New-Item -ItemType Directory -Path $env:CLOVER_HOME
    $owned = Join-Path $env:CLOVER_HOME 'owned-write.txt'
    Set-AccountChildStage -Phase 'home_create' -State 'passed'
    Set-AccountChildStage -Phase 'home_write' -State 'entered'
    [IO.File]::WriteAllText($owned, 'Private non-admin home write control')
    Set-AccountChildStage -Phase 'home_write' -State 'passed'
    Set-AccountChildStage -Phase 'home_read' -State 'entered'
    Assert-AccountChildPredicate ([IO.File]::ReadAllText($owned) -eq 'Private non-admin home write control')
    Set-AccountChildStage -Phase 'home_read' -State 'passed'
    # Registration readiness only: no gateway, installer, update or task is run.
    Set-AccountChildStage -Phase 'startup_location' -State 'entered'
    $startupDefault = [Environment]::GetFolderPath('Startup')
    $script:childStage.startup.default_empty = [string]::IsNullOrEmpty($startupDefault)
    $startup = [Environment]::GetFolderPath('Startup', [Environment+SpecialFolderOption]::DoNotVerify)
    # Earlier stages bind this identity/profile to the exact created SID. Verify
    # the OS location under that profile's APPDATA before any Startup creation.
    $script:childStage.startup.resolved_owned = Test-AccountOwnedStartupLocation -Startup $startup -AppData $env:APPDATA -Profile $c.profile
    Assert-AccountChildPredicate ($script:childStage.startup.resolved_owned -and
        ($script:childStage.startup.default_empty -or $startupDefault -eq $startup))
    Set-AccountChildStage -Phase 'startup_location' -State 'passed'
    Set-AccountChildStage -Phase 'startup_create' -State 'entered'
    $null = New-Item -ItemType Directory -Path $startup -Force
    $script:childStage.startup.exists = Test-Path -LiteralPath $startup -PathType Container
    $script:childStage.startup.readback_agrees = [Environment]::GetFolderPath('Startup') -eq $startup
    Assert-AccountChildPredicate ($script:childStage.startup.exists -and $script:childStage.startup.readback_agrees -and
        (Test-AccountOwnedStartupLocation -Startup $startup -AppData $env:APPDATA -Profile $c.profile))
    $readiness = Join-Path $startup 'clover-fixture-readiness.txt'
    Set-AccountChildStage -Phase 'startup_create' -State 'passed'
    Set-AccountChildStage -Phase 'startup_write' -State 'entered'
    [IO.File]::WriteAllText($readiness, 'Disposable registration directory readiness, not a gateway')
    Set-AccountChildStage -Phase 'startup_write' -State 'passed'
    Set-AccountChildStage -Phase 'startup_read' -State 'entered'
    Assert-AccountChildPredicate ([IO.File]::ReadAllText($readiness) -eq 'Disposable registration directory readiness, not a gateway')
    Set-AccountChildStage -Phase 'startup_read' -State 'passed'
    Set-AccountChildStage -Phase 'startup_remove' -State 'entered'
    Remove-Item -LiteralPath $readiness
    Set-AccountChildStage -Phase 'startup_remove' -State 'passed'
    Set-AccountChildStage -Phase 'access_receipt' -State 'entered'
    @{ exit=0; sid=$identity.User.Value; admin=$admin; is_user_an_admin=$token.is_user_an_admin;
       admin_only_read_denied=$denied; private_home_write=$true; registration_directory_ready=$true;
       home=$env:HOME; userprofile=$env:USERPROFILE; appdata=$env:APPDATA; localappdata=$env:LOCALAPPDATA;
       clover_home=$env:CLOVER_HOME; startup=$startup; pid=$PID } |
        ConvertTo-Json | Set-Content (Join-Path $c.evidence 'access.json') -Encoding utf8
    Set-AccountChildStage -Phase 'access_receipt' -State 'passed'
    $exitCode = 0
} catch {
    $originalFailure = $_.Exception
    $exitCode = 1
    try { Set-AccountChildStage -Phase $script:childStage.phase -State 'failed' -Exception $originalFailure } catch { }
    # Fixed generic fallback survives even diagnostic persistence/classifier failure.
    try {
        [IO.File]::WriteAllText((Join-Path $c.evidence 'child-failure.txt'), 'Actual account child failed; inspect phase-specific nonsecret receipts')
    } catch { }
}
exit $exitCode
