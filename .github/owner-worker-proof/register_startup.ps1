param([Parameter(Mandatory=$true)][string]$Python,
      [Parameter(Mandatory=$true)][string]$Source,
      [Parameter(Mandatory=$true)][string]$Evidence,
      [switch]$Worker)
$ErrorActionPreference = 'Stop'
# The parent validates the hosted environment before creating the helper task.
# Scheduler children do not inherit its process environment.
if (-not $Worker -and ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted')) {
    throw 'Disposable hosted runner required'
}
$receipt = Join-Path $Evidence 'startup-registration.json'
$taskName = 'Clover_OwnerWorker_StartupRegistration'
if ($Worker) {
    $code = 1
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = [Security.Principal.WindowsPrincipal]::new($identity)
        $admin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if ($admin) { throw 'Registration must use a real limited token, not mock admin detection' }
        Set-Location -LiteralPath $Source
        # Same account, default installed home; Task Scheduler does not inherit
        # the orchestrator's process env. Nothing here changes product code.
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
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'Refuse pre-existing registration task' }
# S4U/Limited gives the CURRENT account's actual filtered token. It is only a
# registration helper, never a gateway supervisor or restart/recovery path.
$hostExe = (Get-Process -Id $PID).Path
$arguments = '-NoProfile -File "' + $PSCommandPath + '" -Worker -Python "' + $Python + '" -Source "' + $Source + '" -Evidence "' + $Evidence + '"'
$action = New-ScheduledTaskAction -Execute $hostExe -Argument $arguments -WorkingDirectory $Source
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings | Out-Null
    Export-ScheduledTask -TaskName $taskName | Set-Content -LiteralPath (Join-Path $Evidence 'startup-registration-task.xml') -Encoding utf8
    Start-ScheduledTask -TaskName $taskName
    $until = [DateTime]::UtcNow.AddSeconds(120)
    while (-not (Test-Path -LiteralPath $receipt)) {
        if ([DateTime]::UtcNow -gt $until) { throw 'Limited-token CLI registration timed out' }
        Start-Sleep -Milliseconds 500
    }
    # A task may still be flushing the receipt after the path appears.
    while ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') {
        if ([DateTime]::UtcNow -gt $until) { throw 'Registration task did not exit' }
        Start-Sleep -Milliseconds 500
    }
    $result = Get-Content -LiteralPath $receipt -Raw | ConvertFrom-Json
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    $info | Select-Object TaskName, LastRunTime, LastTaskResult | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $Evidence 'startup-registration-task-result.json') -Encoding utf8
    if ($result.exit -ne 0 -or $info.LastTaskResult -ne 0 -or $result.admin -ne $false) {
        throw 'Supported limited-token gateway install failed; retain logs, no helper fallback'
    }
} finally {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
}
