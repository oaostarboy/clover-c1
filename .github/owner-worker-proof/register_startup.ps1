param([Parameter(Mandatory=$true)][string]$Python,
      [Parameter(Mandatory=$true)][string]$Source,
      [Parameter(Mandatory=$true)][string]$Evidence,
      [string]$ExpectedSid)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne 'Win32NT' -or $env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted') {
    throw 'Disposable hosted Windows runner required'
}
if (-not $ExpectedSid) { throw 'Fresh-account SID contract required; full matrix integration is not yet wired' }
$receipt = Join-Path $Evidence 'startup-registration.json'
if (Test-Path -LiteralPath $receipt) { throw 'Refuse previous registration receipt' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$admin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($admin) { throw 'Registration requires the real non-admin fixture account' }
if ($identity.User.Value -ne $ExpectedSid) { throw 'Wrong created account SID' }
& $Python -I (Join-Path $PSScriptRoot 'restricted_token.py') --evidence (Join-Path $Evidence 'registration-token.json') --expected-sid $ExpectedSid
if ($LASTEXITCODE -ne 0) { throw 'Actual registration token failed' }
Set-Location -LiteralPath $Source
$env:CLOVER_HOME = Split-Path -Parent $Source
$env:VIRTUAL_ENV = Split-Path -Parent (Split-Path -Parent $Python)
$argv = @('-m', 'clover_cli.main', 'gateway', 'install', '--no-start-now', '--start-on-login')
# The actual public command receives the actual supported UAC-decline answer.
'n' | & $Python @argv *> (Join-Path $Evidence 'startup-registration-cli.log')
$childExit = $LASTEXITCODE
@{ exit=$childExit; sid=$identity.User.Value; user=$identity.Name; admin=$admin;
   python=$Python; argv=$argv; home=$env:CLOVER_HOME; source=$Source;
   uac_answer='n'; private_helpers=$false } |
    ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $receipt -Encoding utf8
if ($childExit -ne 0) { throw 'Supported non-admin gateway install failed; no fallback helper' }
