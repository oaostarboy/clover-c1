param([Parameter(Mandatory=$true)][string]$Python,
      [Parameter(Mandatory=$true)][string]$Source)
$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted') { throw 'Disposable hosted runner required' }
if (Get-ScheduledTask -TaskName 'Clover_OwnerWorker_Proof' -ErrorAction SilentlyContinue) { throw 'Refuse pre-existing task' }
# Same native action/principal/settings as the already exercised Troy proof.
$action = New-ScheduledTaskAction -Execute $Python -Argument '-m clover_cli.main gateway run' -WorkingDirectory $Source
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'Clover_OwnerWorker_Proof' -Action $action -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName 'Clover_OwnerWorker_Proof'
Export-ScheduledTask -TaskName 'Clover_OwnerWorker_Proof'
