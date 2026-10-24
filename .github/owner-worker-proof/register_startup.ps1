param([Parameter(Mandatory=$true)][string]$Python,
      [Parameter(Mandatory=$true)][string]$Source,
      [Parameter(Mandatory=$true)][string]$Evidence,
      [switch]$Worker)
$ErrorActionPreference = 'Stop'
# The parent permits OS token creation only on a disposable hosted runner.
if (-not $Worker -and ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted')) {
    throw 'Disposable hosted runner required'
}
$receipt = Join-Path $Evidence 'startup-registration.json'

if ($Worker) {
    $code = 1
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = [Security.Principal.WindowsPrincipal]::new($identity)
        $admin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if ($admin) { throw 'Registration must use a real limited token, not mock admin detection' }
        Set-Location -LiteralPath $Source
        # Same account and default installed home. The launcher passes an explicit
        # credentials-free environment. Nothing here changes product code.
        $env:CLOVER_HOME = Split-Path -Parent $Source
        $env:VIRTUAL_ENV = Split-Path -Parent (Split-Path -Parent $Python)
        $argv = @('-m', 'clover_cli.main', 'gateway', 'install', '--no-start-now', '--start-on-login')
        # Supported answer to the real non-admin UAC question: decline it.
        # This takes the shipped Startup fallback without changing its helpers.
        'n' | & $Python @argv *> (Join-Path $Evidence 'startup-registration-cli.log')
        $code = $LASTEXITCODE
        @{ exit=$code; user=$identity.Name; admin=$admin; python=$Python; argv=$argv;
           home=$env:CLOVER_HOME; source=$Source; uac_answer='n'; private_helpers=$false } |
            ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $receipt -Encoding utf8
    } catch {
        @{ exit=1; error="$($_.Exception.Message)" } |
            ConvertTo-Json | Set-Content -LiteralPath $receipt -Encoding utf8
        throw
    }
    exit $code
}
if (Test-Path -LiteralPath $receipt) { throw 'Refuse previous registration receipt' }
# Task Scheduler's Limited is a requested run level, not token proof (notably
# for the built-in Administrator). Restrict the OS token, preserving SID/profile.
$hostExe = (Get-Process -Id $PID).Path
$launcher = Join-Path $PSScriptRoot 'restricted_token.py'
& $Python -I $launcher launch --evidence $Evidence --cwd $Source -- $hostExe -NoProfile -File $PSCommandPath -Worker -Python $Python -Source $Source -Evidence $Evidence
$childExit = $LASTEXITCODE
if (-not (Test-Path -LiteralPath $receipt)) { throw 'Limited-token CLI registration produced no receipt' }
$result = Get-Content -LiteralPath $receipt -Raw | ConvertFrom-Json
if ($childExit -ne 0 -or $result.exit -ne 0 -or $result.admin -ne $false) {
    throw 'Supported limited-token gateway install failed; retain logs, no helper fallback'
}
