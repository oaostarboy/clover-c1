param([Parameter(Mandatory=$true)][string]$Python,
      [Parameter(Mandatory=$true)][string]$Evidence,
      [ValidateSet('Parent','Scheduled','Restricted')][string]$Mode='Parent')
$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'restricted_token.py'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$admin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($Mode -ne 'Parent') {
    @{ sid=$identity.User.Value; admin=$admin; name=$identity.Name } | ConvertTo-Json |
        Set-Content (Join-Path $Evidence ($Mode.ToLower() + '-principal.json')) -Encoding utf8
    if ($Mode -eq 'Scheduled') {
        & $Python -I $launcher identity --evidence (Join-Path $Evidence 'scheduled-token.json')
        exit $LASTEXITCODE
    }
    if ($admin) { throw 'Restricted child WindowsPrincipal is still admin' }
    # Native access check, not an admin-detector mock: deny an admin-only file.
    $denied = $false
    try {
        [IO.File]::ReadAllText((Join-Path $Evidence 'admin-only.txt')) | Out-Null
    } catch [UnauthorizedAccessException] {
        $denied = $true
    }
    if (-not $denied) { throw 'OS allowed restricted child to read admin-only data' }
    $owned = Join-Path $env:CLOVER_HOME 'owned-write.txt'
    [IO.File]::WriteAllText($owned, 'same SID and private home remain writable')
    if ([IO.File]::ReadAllText($owned) -ne 'same SID and private home remain writable') { throw 'Owned write failed' }
    @{ admin_only_read_denied=$denied; private_home_write=$true; sid=$identity.User.Value;
       admin=$admin; home=$env:HOME; clover_home=$env:CLOVER_HOME; userprofile=$env:USERPROFILE } |
        ConvertTo-Json | Set-Content (Join-Path $Evidence 'restricted-access.json') -Encoding utf8
    exit 0
}
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted') {
    throw 'Disposable hosted runner required'
}
if (-not $admin) { throw 'Probe requires the hosted admin parent to establish the differential' }
New-Item -ItemType Directory -Path $Evidence -ErrorAction Stop | Out-Null
$Evidence = (Resolve-Path $Evidence).Path
$env:HOME = Join-Path $Evidence 'private-home'
$env:CLOVER_HOME = Join-Path $env:HOME '.clover'
New-Item -ItemType Directory -Path $env:CLOVER_HOME -Force | Out-Null
$protected = Join-Path $Evidence 'admin-only.txt'
try {
[IO.File]::WriteAllText($protected, 'owned native ACL denial control; no credentials')
# Private file only: preserve no inherited or owner-user data grants.
& icacls.exe $protected /inheritance:r /grant:r '*S-1-5-32-544:F' > (Join-Path $Evidence 'private-acl.log')
if ($LASTEXITCODE -ne 0) { throw 'Private ACL setup failed' }
[IO.File]::ReadAllText($protected) | Out-Null
(Get-Acl -LiteralPath $protected).Sddl | Set-Content (Join-Path $Evidence 'private-acl-sddl.txt') -Encoding utf8
$hostExe = (Get-Process -Id $PID).Path
# Observe the ORIGINAL S4U/Limited premise without product imports or installers.
$taskName = 'Clover_TokenPrerequisite_' + [Guid]::NewGuid().ToString('N')
$arguments = '-NoProfile -File "' + $PSCommandPath + '" -Mode Scheduled -Python "' + $Python + '" -Evidence "' + $Evidence + '"'
$action = New-ScheduledTaskAction -Execute $hostExe -Argument $arguments -WorkingDirectory $PSScriptRoot
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 30) -MultipleInstances IgnoreNew
$created = $false
try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $taskPrincipal -Settings $settings | Out-Null
    $created = $true
    Export-ScheduledTask -TaskName $taskName | Set-Content (Join-Path $Evidence 'scheduled-task.xml') -Encoding utf8
    Start-ScheduledTask -TaskName $taskName
    $until = [DateTime]::UtcNow.AddSeconds(35)
    while (-not (Test-Path (Join-Path $Evidence 'scheduled-token.json')) -or (Get-ScheduledTask -TaskName $taskName).State -eq 'Running') {
        if ([DateTime]::UtcNow -gt $until) { throw 'Baseline token observation timed out' }
        Start-Sleep -Milliseconds 200
    }
    $taskResult = (Get-ScheduledTaskInfo -TaskName $taskName).LastTaskResult
    if ($taskResult -ne 0) { throw "Baseline task exited $taskResult" }
} finally {
    if ($created) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'Owned task cleanup failed' }
    }
}
    & $Python -I $launcher launch --evidence $Evidence --cwd $PSScriptRoot -- $hostExe -NoProfile -File $PSCommandPath -Mode Restricted -Python $Python -Evidence $Evidence
    if ($LASTEXITCODE -ne 0) { throw "Restricted-token launch failed: $LASTEXITCODE" }
    $parent = Get-Content (Join-Path $Evidence 'token-parent.json') -Raw | ConvertFrom-Json
    $child = Get-Content (Join-Path $Evidence 'token-child.json') -Raw | ConvertFrom-Json
    $scheduled = Get-Content (Join-Path $Evidence 'scheduled-token.json') -Raw | ConvertFrom-Json
    $access = Get-Content (Join-Path $Evidence 'restricted-access.json') -Raw | ConvertFrom-Json
    if ($access.admin_only_read_denied -ne $true -or $access.private_home_write -ne $true -or $access.admin -ne $false -or $access.sid -ne $parent.sid) {
        throw 'Native capability differential did not pass'
    }
    @{ result='PASS'; parent_sid=$parent.sid; child_sid=$child.sid;
       original_s4u_admin=$scheduled.admin_member; child_admin=$child.admin_member;
       child_elevated=$child.elevated; child_integrity=$child.integrity_rid;
       admin_only_read_denied=$access.admin_only_read_denied; private_home_write=$access.private_home_write;
       product_imports=$false; installer_runs=0; update_cases=0; accounts_created=0; task_removed=$true } |
        ConvertTo-Json | Tee-Object -FilePath (Join-Path $Evidence 'RESULT.json')
} finally {
    # Remove only this job's own private control and written payload.
    Remove-Item -LiteralPath $protected -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $env:CLOVER_HOME 'owned-write.txt') -Force -ErrorAction SilentlyContinue
}
