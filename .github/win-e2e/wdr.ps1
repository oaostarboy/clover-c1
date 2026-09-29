# Probes and pass criteria for the Windows update e2e (dot-sourced by steps).

function Get-GatewayRoots {
  # One entry per gateway: the venv python.exe launcher and the interpreter it
  # starts share one command line, so count only processes whose parent is
  # not itself a gateway process. Relaunch helpers (python -c ...) are not
  # gateways.
  $all = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -match 'clover_cli[.]main' -and
    $_.CommandLine -match 'gateway\s+run' -and $_.CommandLine -notmatch '\s-c\s' })
  $ids = @($all | ForEach-Object { $_.ProcessId })
  ,@($all | Where-Object { $ids -notcontains $_.ParentProcessId })
}

function Test-ApiPort {
  $c = New-Object System.Net.Sockets.TcpClient
  try { $ok = $c.ConnectAsync("127.0.0.1", [int]$env:API_PORT).Wait(1500); return ($ok -and $c.Connected) }
  catch { return $false } finally { $c.Dispose() }
}

function Get-GatewayState {
  try { Get-Content "$env:CLOVER_HOME\gateway_state.json" -Raw | ConvertFrom-Json } catch { $null }
}

function Get-LogLines {
  $p = "$env:CLOVER_HOME\logs\gateway.log"
  if (Test-Path $p) { @(Get-Content $p) } else { @() }
}

function Get-UpdateProcesses {
  @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and (
    ($_.CommandLine -match 'clover_cli[.]main' -and $_.CommandLine -match '\supdate(\s|$)') -or
    ($_.CommandLine -match 'clover\.exe"?\s+update(\s|$)') -or
    ($_.CommandLine -match 'update_restart_watcher')) })
}

function Wait-UpdateChainDone([int]$timeoutS = 900) {
  # The Windows shim hands the dependency sync to a child that keeps running
  # after `clover update` returns; the restart watcher then verifies. Measure
  # only once the whole chain has finished.
  $deadline = (Get-Date).AddSeconds($timeoutS)
  while ((Get-Date) -lt $deadline) {
    if ((Get-UpdateProcesses).Count -eq 0) { return }
    Start-Sleep -Seconds 3
  }
  throw "update chain still running after ${timeoutS}s"
}

function Use-TrackingBranch([string]$branch) {
  $d = $env:INSTALL_DIR
  git -C $d remote set-branches --add origin $branch
  git -C $d fetch -q origin "+refs/heads/${branch}:refs/remotes/origin/${branch}"
  if ($LASTEXITCODE -ne 0) { throw "fetch $branch failed" }
  git -C $d checkout -q -B $branch HEAD
  git -C $d branch -q -u "origin/$branch"
  Write-Host "local $branch at $(git -C $d rev-parse --short HEAD), origin/$branch at $(git -C $d rev-parse --short origin/$branch)"
}

function Set-MainIsFix([string]$fixBranch) {
  # `clover update` and the gateway's /update always target main. To run
  # those EXACT commands against an unmerged fix without touching main, the
  # runner's clone replaces main's current commit with the fix tip (git
  # replace): the updater fetches origin/main as usual and git reads the fix.
  # Assert-FixLanded catches main moving underneath.
  $d = $env:INSTALL_DIR
  git -C $d fetch -q origin "+refs/heads/main:refs/remotes/origin/main" "+refs/heads/${fixBranch}:refs/remotes/origin/${fixBranch}"
  if ($LASTEXITCODE -ne 0) { throw "fetch failed" }
  $mainSha = (git -C $d rev-parse refs/remotes/origin/main).Trim()
  $fixSha = (git -C $d rev-parse "refs/remotes/origin/$fixBranch").Trim()
  git -C $d replace -f $mainSha $fixSha
  if ($LASTEXITCODE -ne 0) { throw "git replace failed" }
  git -C $d checkout -q -B main HEAD
  git -C $d branch -q -u origin/main
  "MAIN_SHA=$mainSha" | Out-File -FilePath $env:GITHUB_ENV -Append -Encoding utf8
  Write-Host "main $mainSha now reads as $fixBranch $fixSha; local main at $(git -C $d rev-parse --short HEAD)"
}

function Test-CleanupProgress {
  $py = "$env:INSTALL_DIR\venv\Scripts\python.exe"
  $code = "from clover_cli.config import read_raw_config; from gateway.display_config import resolve_display_setting; print(resolve_display_setting(read_raw_config(), 'telegram', 'cleanup_progress'))"
  Push-Location $env:INSTALL_DIR
  try { (& $py -c $code 2>$null | Select-Object -Last 1) } finally { Pop-Location }
}

function Measure-SingleGateway([string]$label, [int]$logStart, [bool]$strictLog) {
  $bad = @()
  Wait-UpdateChainDone 1800
  # Up within 120 s after the chain finished...
  $deadline = (Get-Date).AddSeconds(120)
  while ((Get-Date) -lt $deadline -and -not ((Get-GatewayRoots).Count -eq 1 -and (Test-ApiPort))) { Start-Sleep -Seconds 2 }
  # ...then exactly one, bound, healthy state for 90 s straight.
  $end = (Get-Date).AddSeconds(90); $n = 0
  while ((Get-Date) -lt $end) {
    $roots = Get-GatewayRoots; $port = Test-ApiPort; $st = Get-GatewayState
    $n++
    $line = "[$label] t=$n roots=$($roots.Count) pids=$(($roots | ForEach-Object ProcessId) -join ',') port=$port state=$($st.gateway_state) restart_requested=$($st.restart_requested)"
    Write-Host $line
    if ($roots.Count -ne 1 -or -not $port -or $st.restart_requested -eq $true -or $st.gateway_state -ne 'running') { $bad += $line }
    Start-Sleep -Seconds 3
  }
  $lines = Get-LogLines
  $new = if ($lines.Count -gt $logStart) { $lines[$logStart..($lines.Count - 1)] } else { @() }
  $first = -1
  for ($i = 0; $i -lt $new.Count; $i++) { if ($new[$i] -match 'Starting Clover Gateway') { $first = $i; break } }
  $starts = @($new | Where-Object { $_ -match 'Starting Clover Gateway' }).Count
  $after = if ($first -ge 0) { @($new[$first..($new.Count - 1)] | Where-Object { $_ -match 'Gateway restart requested|Stopping gateway|service-restart requested|planned --replace takeover|Replacing existing gateway' }) } else { @() }
  Write-Host "[$label] post-update gateway starts=$starts; restart/takeover lines after first post-update start=$($after.Count)"
  $after | ForEach-Object { Write-Host "  $_" }
  if ($first -lt 0) { $bad += "no post-update 'Starting Clover Gateway' in gateway.log" }
  if ($strictLog -and $after.Count -gt 0) { $bad += "restart/takeover after the post-update start" }
  if ($strictLog -and $starts -ne 1) { $bad += "expected exactly 1 post-update gateway start, saw $starts" }
  ,@($bad)
}

function Assert-SingleGateway([string]$label, [int]$logStart, [bool]$strictLog) {
  $bad = Measure-SingleGateway $label $logStart $strictLog
  if ($bad.Count) {
    Write-Host "FAIL [$label]:"; $bad | Select-Object -First 20 | ForEach-Object { Write-Host "  $_" }
    Write-Host "--- gateway.log tail ---"; Get-LogLines | Select-Object -Last 120
    throw "[$label] not exactly one healthy gateway"
  }
  Write-Host "PASS [$label]: exactly one gateway for 90s, api port bound, state running, no stale restart_requested"
}

function Measure-UpdateOutcome([string]$label, $exitCode, [string]$outputText) {
  # Everything except gateway liveness: exit code, receipt, outcome sidecar,
  # the Telegram summary-card setting, and that the fix code actually landed.
  $bad = @()
  if ("$exitCode" -ne "0") { $bad += "update exit=$exitCode" }
  $receiptPath = "$env:CLOVER_HOME\logs\update_receipts\latest.json"
  $receipt = $null
  try { $receipt = Get-Content $receiptPath -Raw | ConvertFrom-Json } catch { }
  if ($receipt) {
    Write-Host "[$label] receipt outcome=$($receipt.outcome) exit_code=$($receipt.exit_code)"
    $failedBackup = @($receipt.steps | Where-Object { $_.name -eq 'pre_update_backup' -and -not $_.ok })
    if ($failedBackup.Count) { $bad += "receipt: pre-update backup step failed ($($failedBackup[0].detail))" }
    $failedSteps = @($receipt.steps | Where-Object { -not $_.ok } | ForEach-Object { $_.name })
    if ($failedSteps.Count) { Write-Host "[$label] receipt failed steps: $($failedSteps -join ', ')" }
  } else { Write-Host "[$label] no update receipt" }
  if ($outputText -match '(?i)backup step failed|backup failed') { $bad += "update output mentions a failed backup" }
  $sidecar = $null
  try { $sidecar = Get-Content "$env:CLOVER_HOME\.clover-last-update" -Raw | ConvertFrom-Json } catch { }
  Write-Host "[$label] .clover-last-update: $($sidecar | ConvertTo-Json -Compress)"
  if ($sidecar -and $sidecar.outcome -eq 'failed') { $bad += ".clover-last-update outcome=failed ($($sidecar.detail))" }
  $cp = Test-CleanupProgress
  Write-Host "[$label] telegram cleanup_progress resolves to: $cp"
  if ("$cp" -ne "True") { $bad += "telegram cleanup_progress resolves to '$cp', not True" }
  $landed = Select-String -Path "$env:INSTALL_DIR\clover_cli\update_cmd.py" -Pattern '_hand_off_windows_gateway_resume' -Quiet
  if (-not $landed) { $bad += "fix code did not land (main moved during the run?)" }
  ,@($bad)
}
