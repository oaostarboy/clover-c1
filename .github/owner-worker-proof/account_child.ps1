param([Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $IsWindows -or $env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted') {
    throw 'Disposable hosted Windows runner required'
}
$c = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
$exitCode = 1
try {
    foreach ($name in @('GITHUB_TOKEN','ACTIONS_RUNTIME_TOKEN','ACTIONS_ID_TOKEN_REQUEST_TOKEN',
                         'OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY')) {
        if ($null -ne [Environment]::GetEnvironmentVariable($name)) { throw 'Credential isolation failed' }
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $admin = ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($identity.User.Value -ne $c.expected_sid -or $identity.User.Value -eq $c.runner_sid -or $admin) {
        throw 'Credential logon did not run as the intended non-admin SID'
    }
    & $c.python -I (Join-Path $PSScriptRoot 'restricted_token.py') --evidence (Join-Path $c.evidence 'token.json') --expected-sid $c.expected_sid
    if ($LASTEXITCODE -ne 0) { throw 'Real child token observation failed' }
    $token = Get-Content (Join-Path $c.evidence 'token.json') -Raw | ConvertFrom-Json
    $denied = $false
    try { [IO.File]::ReadAllText($c.protected) | Out-Null }
    catch [UnauthorizedAccessException] { $denied = $true }
    if (-not $denied) { throw 'Actual non-admin could read admin-only data' }
    if ($env:USERPROFILE -ne $c.profile -or $env:HOME -ne $c.profile -or
        [Environment]::GetFolderPath('UserProfile') -ne $c.profile -or
        [Environment]::GetFolderPath('ApplicationData') -ne $env:APPDATA -or
        [Environment]::GetFolderPath('LocalApplicationData') -ne $env:LOCALAPPDATA) {
        throw 'OS loaded profile or private environment locations disagree'
    }
    if (Test-Path -LiteralPath $env:CLOVER_HOME) { throw 'Refuse existing Clover home' }
    $null = New-Item -ItemType Directory -Path $env:CLOVER_HOME
    $owned = Join-Path $env:CLOVER_HOME 'owned-write.txt'
    [IO.File]::WriteAllText($owned, 'Private non-admin home write control')
    if ([IO.File]::ReadAllText($owned) -ne 'Private non-admin home write control') { throw 'Own home write failed' }
    # Registration readiness only: the OS-known Startup directory must be writable
    # by this genuine account. No fake gateway, installer, update or task is run.
    $startup = [Environment]::GetFolderPath('Startup')
    if (-not $startup.StartsWith($env:APPDATA + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'OS Startup location is outside private AppData'
    }
    $null = New-Item -ItemType Directory -Path $startup -Force
    $readiness = Join-Path $startup 'clover-fixture-readiness.txt'
    [IO.File]::WriteAllText($readiness, 'Disposable registration directory readiness, not a gateway')
    if ([IO.File]::ReadAllText($readiness) -ne 'Disposable registration directory readiness, not a gateway') { throw 'Startup readiness write failed' }
    Remove-Item -LiteralPath $readiness
    $exitCode = 0
    @{ exit=0; sid=$identity.User.Value; admin=$admin; is_user_an_admin=$token.is_user_an_admin;
       admin_only_read_denied=$denied; private_home_write=$true; registration_directory_ready=$true;
       home=$env:HOME; userprofile=$env:USERPROFILE; appdata=$env:APPDATA; localappdata=$env:LOCALAPPDATA;
       clover_home=$env:CLOVER_HOME; startup=$startup; pid=$PID } |
        ConvertTo-Json | Set-Content (Join-Path $c.evidence 'access.json') -Encoding utf8

} catch {
    # Fixed safe diagnostic only; never serialize arbitrary exceptions/locals/env.
    [IO.File]::WriteAllText((Join-Path $c.evidence 'child-failure.txt'), 'Actual account child failed; inspect phase-specific nonsecret receipts')
    $exitCode = 1
}
exit $exitCode
