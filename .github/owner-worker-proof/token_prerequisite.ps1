param([Parameter(Mandatory=$true)][string]$Python,
      [Parameter(Mandatory=$true)][string]$Evidence)
$ErrorActionPreference = 'Stop'
# Only account/profile/logon/token/access/Startup-directory readiness. No installer
# or product import, no updater rows, and no synthetic token or scheduler task.
& (Join-Path $PSScriptRoot 'disposable_account.ps1') -Python $Python -Evidence $Evidence
