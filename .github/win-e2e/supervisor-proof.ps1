# Probes and pass criteria for the "Windows system service is not a gateway
# supervisor" update e2e (dot-sourced by the workflow steps).
#
# Two setups, both updated with the gateway RUNNING:
#   troy  gateway launched by a Windows Scheduled Task running
#         <venv>\pythonw.exe -m clover_cli.main gateway run, so its parent
#         chain passes through the svchost.exe that hosts the Schedule service.
#   zyra  default install: the generated Startup-folder VBS launcher starts the
#         gateway (no service, no Task Scheduler ancestor).

function Get-GatewayRoots {
  # One entry per gateway: the venv python.exe launcher and the interpreter it
  # starts share one command line, so count only processes whose parent is not
  # itself a gateway process. Relaunch helpers (python -c ...) are not gateways.
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
  $deadline = (Get-Date).AddSeconds($timeoutS)
  while ((Get-Date) -lt $deadline) {
    if ((Get-UpdateProcesses).Count -eq 0) { return }
    Start-Sleep -Seconds 3
  }
  throw "update chain still running after ${timeoutS}s"
}

function Get-ScheduleServiceInfo {
  $svc = Get-CimInstance Win32_Service -Filter "Name='Schedule'"
  [pscustomobject]@{ State = "$($svc.State)"; ProcessId = [int]$svc.ProcessId }
}

function Get-AncestorChain([int]$procId) {
  # "pid:name:cmdline" for every ancestor, closest first.
  $chain = @()
  $seen = @{}
  $cur = Get-CimInstance Win32_Process -Filter "ProcessId=$procId"
  while ($cur -and -not $seen.ContainsKey([int]$cur.ParentProcessId)) {
    $seen[[int]$cur.ProcessId] = $true
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($cur.ParentProcessId)"
    if (-not $parent) { break }
    $cmd = "$($parent.CommandLine)"
    $chain += "$($parent.ProcessId):$($parent.Name):$($cmd.Substring(0, [Math]::Min(90, $cmd.Length)))"
    $cur = $parent
  }
  ,@($chain)
}

function Start-TroyGateway {
  # Scheduled Task => the process tree hangs off the Schedule svchost.
  $py = "$env:INSTALL_DIR\venv\Scripts\pythonw.exe"
  $action = New-ScheduledTaskAction -Execute $py -Argument '-m clover_cli.main gateway run' -WorkingDirectory $env:INSTALL_DIR
  $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
  $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask -TaskName 'Clover_Troy_Gateway' -Action $action -Principal $principal -Settings $settings -Force | Out-Null
  Start-ScheduledTask -TaskName 'Clover_Troy_Gateway'
  Write-Host "registered and started Scheduled Task Clover_Troy_Gateway: $py -m clover_cli.main gateway run"
  schtasks /Query /TN Clover_Troy_Gateway /FO LIST | Out-String | Write-Host
}

function Start-ZyraGateway {
  # Default install path: the Startup-folder VBS launcher Clover generates.
  $py = "$env:INSTALL_DIR\venv\Scripts\python.exe"
  $code = "from clover_cli import gateway_windows as g; p = g._write_task_script(); print(g._install_startup_entry(p))"
  Push-Location $env:INSTALL_DIR
  try { $entry = (& $py -c $code | Select-Object -Last 1) } finally { Pop-Location }
  if (-not $entry -or -not (Test-Path $entry)) { throw "Startup VBS was not written ($entry)" }
  Write-Host "Startup-folder launcher: $entry"
  Start-Process -FilePath wscript.exe -ArgumentList @('//B', '//Nologo', "`"$entry`"") -WindowStyle Hidden
}

function Wait-GatewayUp([int]$timeoutS = 150) {
  $deadline = (Get-Date).AddSeconds($timeoutS)
  while ((Get-Date) -lt $deadline -and -not ((Get-GatewayRoots).Count -eq 1 -and (Test-ApiPort))) { Start-Sleep -Seconds 2 }
  if (-not ((Get-GatewayRoots).Count -eq 1 -and (Test-ApiPort))) {
    Get-LogLines | Select-Object -Last 80 | Out-Host
    throw "gateway did not come up (roots=$((Get-GatewayRoots).Count) port=$(Test-ApiPort))"
  }
}

function Show-GatewayOwnership([string]$label) {
  # Evidence: where the gateway sits in the process tree, which service owns
  # that tree, and what the updater's own SCM probe says about it.
  $roots = Get-GatewayRoots
  $gw = [int]($roots | Select-Object -First 1).ProcessId
  $sched = Get-ScheduleServiceInfo
  $chain = Get-AncestorChain $gw
  Write-Host "[$label] gateway pid=$gw; Schedule service state=$($sched.State) host pid=$($sched.ProcessId)"
  $chain | ForEach-Object { Write-Host "[$label]   ancestor $_" }
  $viaSchedule = @($chain | Where-Object { $_ -like "$($sched.ProcessId):*" }).Count -gt 0
  Write-Host "[$label] gateway descends from the Schedule service host: $viaSchedule"
  $probe = @'
import sys
from clover_cli.gateway import find_windows_gateway_services
try:
    found = find_windows_gateway_services()
    print("find_windows_gateway_services ->", [(s.name, s.service_pid, s.gateway_pid) for s in found])
except Exception as exc:
    print("find_windows_gateway_services raised:", type(exc).__name__, exc)
'@
  $probeFile = "$env:RUNNER_TEMP\scm_probe.py"
  Set-Content -Path $probeFile -Value $probe -Encoding utf8
  Push-Location $env:INSTALL_DIR
  try { & "$env:INSTALL_DIR\venv\Scripts\python.exe" $probeFile 2>&1 | ForEach-Object { Write-Host "[$label] [scm-probe] $_" } } finally { Pop-Location }
  # Pre-update identity, for the post-update proof that the RUNNING gateway changed.
  $pre = Get-GatewayIdentity
  Write-Host "[$label] pre-update identity: statePid=$($pre.StatePid) start=$($pre.StartTime) listener=$($pre.ListenerPid) code_sha='$($pre.CodeSha)' pidIsGateway=$($pre.PidIsGateway)"
  "OLD_GATEWAY_CODE_SHA=$($pre.CodeSha)" | Out-File -FilePath $env:GITHUB_ENV -Append -Encoding utf8
  "OLD_GATEWAY_START_TIME=$($pre.StartTime)" | Out-File -FilePath $env:GITHUB_ENV -Append -Encoding utf8
  return [pscustomobject]@{ Pid = $gw; ViaSchedule = $viaSchedule }
}

function Remove-MainFromHistory([string]$target, [string]$mainSha) {
  $d = $env:INSTALL_DIR
  foreach ($line in @(git -C $d rev-list --parents $target)) {
    $ids = @($line -split ' ')
    if ($ids.Count -gt 1 -and ($ids[1..($ids.Count - 1)] -contains $mainSha)) {
      $keep = @($ids[1..($ids.Count - 1)] | Where-Object { $_ -ne $mainSha })
      git -C $d replace -f --graft $ids[0] @keep
      if ($LASTEXITCODE -ne 0) { throw "graft of $($ids[0]) failed" }
    }
  }
}

function Set-MainIsTarget([string]$targetBranch) {
  # `clover update` / the gateway's /update always target origin/main. To run
  # those EXACT commands against an unmerged branch without touching main, the
  # runner's clone replaces main's current commit with the target tip (git
  # replace): the updater fetches origin/main as usual and git reads the target.
  $d = $env:INSTALL_DIR
  git -C $d fetch -q origin "+refs/heads/main:refs/remotes/origin/main" "+refs/heads/${targetBranch}:refs/remotes/origin/${targetBranch}"
  if ($LASTEXITCODE -ne 0) { throw "fetch failed" }
  $mainSha = (git -C $d rev-parse refs/remotes/origin/main).Trim()
  $targetSha = (git -C $d rev-parse "refs/remotes/origin/$targetBranch").Trim()
  Remove-MainFromHistory $targetSha $mainSha
  git -C $d replace -f $mainSha $targetSha
  if ($LASTEXITCODE -ne 0) { throw "git replace failed" }
  git -C $d checkout -q -B main HEAD
  git -C $d branch -q -u origin/main
  Write-Host "main $mainSha now reads as $targetBranch $targetSha; local main at $(git -C $d rev-parse --short HEAD)"
}

function Invoke-PathUpdate([string]$label) {
  # The user's exact update for $env:UPDATE_PATH; returns the exit code, output
  # in update-output.txt. cli = terminal `clover update`; chat = /update sent to
  # the running gateway over a local IRC server, so the gateway's own handler
  # spawns `clover update --gateway` from the gateway's process tree.
  Remove-Item "$env:RUNNER_TEMP\update-output.txt" -ErrorAction SilentlyContinue
  if ($env:UPDATE_PATH -eq 'cli') {
    Write-Host "[$label] running: clover update --yes"
    & "$env:CLOVER_BIN" update --yes 2>&1 | Tee-Object -FilePath "$env:RUNNER_TEMP\update-output.txt" | Out-Host
    return $LASTEXITCODE
  }
  Write-Host "[$label] sending /update to the gateway over IRC"
  New-Item -ItemType File -Force -Path "$env:RUNNER_TEMP\irc.trigger" | Out-Null
  $deadline = (Get-Date).AddSeconds(120)
  while ((Get-Date) -lt $deadline -and (Get-UpdateProcesses).Count -eq 0) { Start-Sleep -Seconds 1 }
  Write-Host "[$label] updater processes: $((Get-UpdateProcesses | ForEach-Object { "$($_.ProcessId)<-$($_.ParentProcessId)" }) -join ', ')"
  Wait-UpdateChainDone 1800
  $deadline = (Get-Date).AddSeconds(300)
  while ((Get-Date) -lt $deadline -and -not (Test-Path "$env:CLOVER_HOME\.update_exit_code") -and (Test-Path "$env:CLOVER_HOME\.update_pending.json")) { Start-Sleep -Seconds 3 }
  $code = if (Test-Path "$env:CLOVER_HOME\.update_exit_code") { (Get-Content "$env:CLOVER_HOME\.update_exit_code" -Raw).Trim() } else { $null }
  if ($null -eq $code) {
    $side = try { Get-Content "$env:CLOVER_HOME\.clover-last-update" -Raw | ConvertFrom-Json } catch { $null }
    $code = if ($side -and $side.outcome -eq 'success') { 0 } else { "unknown (outcome=$($side.outcome))" }
  }
  $transcript = @("$env:CLOVER_HOME\.update_output.txt", "$env:CLOVER_HOME\logs\update-output.last.txt") | Where-Object { Test-Path $_ } | Select-Object -First 1
  if ($transcript) { Copy-Item $transcript "$env:RUNNER_TEMP\update-output.txt" -Force; Write-Host "[$label] transcript: $transcript" }
  Write-Host "--- [$label] updater output (tail) ---"
  Get-Content "$env:RUNNER_TEMP\update-output.txt" -ErrorAction SilentlyContinue | Select-Object -Last 80 | Out-Host
  return $code
}

function Get-GatewayProcessIds {
  # Every process id belonging to a gateway (launcher + interpreter).
  $all = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -match 'clover_cli[.]main' -and
    $_.CommandLine -match 'gateway\s+run' -and $_.CommandLine -notmatch '\s-c\s' })
  ,@($all | ForEach-Object { [int]$_.ProcessId })
}

function Get-ApiListenerPid {
  # The process that actually owns the API listening socket, or $null.
  try {
    $c = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$env:API_PORT) -ErrorAction Stop | Select-Object -First 1)
    if ($c.Count -eq 1) { return [int]$c[0].OwningProcess }
  } catch { }
  return $null
}

function Get-GatewayIdentity {
  # One consistent observation of "which gateway is this": the PID/start time it
  # wrote to gateway_state.json, the code SHA it stamped, and whether that PID is
  # a live gateway process that also owns the API listener.
  $st = Get-GatewayState
  $ids = Get-GatewayProcessIds
  $statePid = if ($st -and $st.pid) { [int]$st.pid } else { 0 }
  [pscustomobject]@{
    StatePid     = $statePid
    StartTime    = "$($st.start_time)"
    CodeSha      = "$($st.code_sha)"
    State        = "$($st.gateway_state)"
    ListenerPid  = Get-ApiListenerPid
    PidIsGateway = ($statePid -ne 0 -and $ids -contains $statePid)
  }
}

function Test-GatewayRunsTarget($identity, [string]$expectedSha, [string]$oldSha) {
  # Pure check: returns the list of violated criteria (empty = the RUNNING
  # gateway is a live process, owns the API port, and stamped the target SHA).
  $bad = @()
  if (-not $identity.PidIsGateway) { $bad += "gateway_state.json pid $($identity.StatePid) is not a live gateway process" }
  if ($null -eq $identity.ListenerPid -or $identity.ListenerPid -ne $identity.StatePid) { $bad += "API listener pid '$($identity.ListenerPid)' is not the state pid $($identity.StatePid)" }
  if ([string]::IsNullOrEmpty($identity.CodeSha)) { $bad += "running gateway stamped no code_sha" }
  elseif ($identity.CodeSha -ne $expectedSha) { $bad += "running gateway code_sha $($identity.CodeSha) != installed target HEAD $expectedSha" }
  if ($oldSha -and $identity.CodeSha -eq $oldSha) { $bad += "running gateway code_sha is still the pre-update $oldSha" }
  ,@($bad)
}

function Test-InstalledTreeIsTarget([string]$targetBranch) {
  # The checkout the update left behind must hold the target's TREE (HEAD's
  # object id is the replaced main commit by design, so compare trees), with no
  # local modification on top of it.
  $d = $env:INSTALL_DIR
  $bad = @()
  $wantTree = (git -C $d rev-parse "refs/remotes/origin/$targetBranch^{tree}").Trim()
  $haveTree = (git -C $d rev-parse "HEAD^{tree}").Trim()
  if ($wantTree -ne $haveTree) { $bad += "installed tree $haveTree != target tree $wantTree" }
  git -C $d diff --quiet HEAD
  if ($LASTEXITCODE -ne 0) { $bad += "installed working tree differs from HEAD" }
  ,@($bad)
}

function Measure-Liveness90([string]$label) {
  # Up within 120 s after the update chain finished, then exactly one gateway,
  # api port bound, state running, no stale restart request, for 90 s straight.
  $bad = @()
  Wait-UpdateChainDone 1800
  $deadline = (Get-Date).AddSeconds(120)
  while ((Get-Date) -lt $deadline -and -not ((Get-GatewayRoots).Count -eq 1 -and (Test-ApiPort))) { Start-Sleep -Seconds 2 }
  $end = (Get-Date).AddSeconds(90); $n = 0; $firstId = $null
  while ((Get-Date) -lt $end) {
    $roots = Get-GatewayRoots; $port = Test-ApiPort; $st = Get-GatewayState
    $sched = (Get-ScheduleServiceInfo).State
    $n++
    $id = Get-GatewayIdentity
    if ($null -eq $firstId) { $firstId = $id }
    $line = "[$label] t=$n roots=$($roots.Count) pids=$(($roots | ForEach-Object ProcessId) -join ',') port=$port state=$($st.gateway_state) restart_requested=$($st.restart_requested) Schedule=$sched statePid=$($id.StatePid) start=$($id.StartTime) listener=$($id.ListenerPid) sha=$($id.CodeSha)"
    Write-Host $line
    if ($roots.Count -ne 1 -or -not $port -or $st.restart_requested -eq $true -or $st.gateway_state -ne 'running' -or $sched -ne 'Running') { $bad += $line }
    # Identity must be one and the same live gateway for the WHOLE window.
    if (-not $id.PidIsGateway -or $id.ListenerPid -ne $id.StatePid -or $id.StatePid -ne $firstId.StatePid -or $id.StartTime -ne $firstId.StartTime -or $id.CodeSha -ne $firstId.CodeSha) { $bad += "identity drift: $line" }
    Start-Sleep -Seconds 3
  }
  ,@($bad)
}

function Assert-UpdateSucceeded([string]$label, [int]$logStart, $exitCode, [int]$oldGatewayPid) {
  $bad = @()
  if ("$exitCode" -ne "0") { $bad += "update exit=$exitCode" }
  $out = "$(Get-Content "$env:RUNNER_TEMP\update-output.txt" -Raw -ErrorAction SilentlyContinue)"
  if ($out -match 'Could not stop Windows gateway service') { $bad += "update tried to stop a Windows service: $(($out -split "`n" | Select-String 'Could not stop Windows gateway service' | Select-Object -First 1))" }
  $bad += Measure-Liveness90 $label
  $new = @(Get-LogLines | Select-Object -Skip $logStart)
  $starts = @($new | Where-Object { $_ -match 'Starting Clover Gateway' }).Count
  $newRoot = [int](Get-GatewayRoots | Select-Object -First 1).ProcessId
  Write-Host "[$label] post-update gateway starts in gateway.log=$starts; gateway pid before=$oldGatewayPid after=$newRoot"
  if ($starts -lt 1) { $bad += "no post-update 'Starting Clover Gateway' in gateway.log (gateway not respawned)" }
  if ($newRoot -eq $oldGatewayPid) { $bad += "gateway pid unchanged ($newRoot): it was not restarted by the update" }
  git -C "$env:INSTALL_DIR" log --oneline -1 | Out-Host
  # Installed tree is the target's, and the RUNNING gateway stamped that checkout's SHA.
  $suffix = $env:HEAD_BRANCH -replace '^ci/win-update-proof/', ''
  $targetBranch = if ($env:PHASE -eq 'old2new') { $env:HEAD_BRANCH } elseif ($suffix -eq 'pr-head') { 'ci/win-update-proof-next' } else { "ci/win-update-next/$suffix" }
  $bad += Test-InstalledTreeIsTarget $targetBranch
  $installedSha = (git -C "$env:INSTALL_DIR" rev-parse HEAD).Trim()
  $finalId = Get-GatewayIdentity
  $bad += Test-GatewayRunsTarget $finalId $installedSha $env:OLD_GATEWAY_CODE_SHA
  if ($env:OLD_GATEWAY_START_TIME -and $finalId.StartTime -eq $env:OLD_GATEWAY_START_TIME) { $bad += "gateway start_time unchanged ($($finalId.StartTime)): same process as before the update" }
  Write-Host "[$label] running gateway: pid=$($finalId.StatePid) start=$($finalId.StartTime) listener=$($finalId.ListenerPid) code_sha=$($finalId.CodeSha) installed HEAD=$installedSha (was $($env:OLD_GATEWAY_CODE_SHA))"
  # Negative controls: the same checks must REJECT a wrong expectation, or they prove nothing.
  if ((Test-GatewayRunsTarget $finalId ('0' * 40) '').Count -eq 0) { $bad += "negative control: wrong expected sha was accepted" }
  if ((Test-GatewayRunsTarget $finalId $installedSha $finalId.CodeSha).Count -eq 0) { $bad += "negative control: unchanged code_sha was accepted" }
  $wrongListener = $finalId.PSObject.Copy(); $wrongListener.ListenerPid = 1
  if ((Test-GatewayRunsTarget $wrongListener $installedSha '').Count -eq 0) { $bad += "negative control: wrong listener pid was accepted" }
  if ($bad.Count) {
    Write-Host "FAIL [$label]:"; $bad | Select-Object -First 30 | ForEach-Object { Write-Host "  $_" }
    Write-Host "--- gateway.log tail ---"; Get-LogLines | Select-Object -Last 120 | Out-Host
    throw "[$label] failed $($bad.Count) criteria"
  }
  Write-Host "PASS [$label]: update exit 0, installed tree == target tree, running gateway stamped the installed SHA and owns the API port, gateway respawned (pid $oldGatewayPid -> $newRoot), exactly one gateway for 90s, port bound, state running, no stale restart_requested, Schedule service still Running"
}

function Assert-ScheduleStopReproduced([string]$label, $exitCode) {
  # Old-commit repro: the update must abort with the reported failure.
  $out = "$(Get-Content "$env:RUNNER_TEMP\update-output.txt" -Raw -ErrorAction SilentlyContinue)"
  Write-Host "[$label] update exit=$exitCode"
  if ("$exitCode" -eq "0") { throw "[$label] expected the update to fail on the old commit, but it exited 0" }
  if ($out -notmatch 'Could not stop Windows gateway service Schedule') {
    Write-Host "--- update output ---"; $out | Out-Host
    throw "[$label] update failed, but not with 'Could not stop Windows gateway service Schedule'"
  }
  $line = ($out -split "`n" | Select-String 'Could not stop Windows gateway service Schedule' | Select-Object -First 1)
  Write-Host "REPRODUCED [$label]: $line"
}
