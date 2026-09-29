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

function Remove-MainFromHistory([string]$target, [string]$mainSha) {
  # The replace trick needs main's commit OUTSIDE the target's history. Once
  # the fix branch merges main, "main reads as target" loops: the local HEAD
  # already contains the replaced main, `rev-list HEAD..origin/main` is 0 and
  # the updater reports "up to date" without pulling. Graft main out as a
  # parent (runner clone only; trees are unchanged).
  $d = $env:INSTALL_DIR
  foreach ($line in @(git -C $d rev-list --parents $target)) {
    $ids = @($line -split ' ')
    if ($ids.Count -gt 1 -and ($ids[1..($ids.Count - 1)] -contains $mainSha)) {
      $keep = @($ids[1..($ids.Count - 1)] | Where-Object { $_ -ne $mainSha })
      git -C $d replace -f --graft $ids[0] @keep
      if ($LASTEXITCODE -ne 0) { throw "graft of $($ids[0]) failed" }
      Write-Host "grafted main $($mainSha.Substring(0,8)) out of $($ids[0].Substring(0,8))'s parents"
    }
  }
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
  Remove-MainFromHistory $fixSha $mainSha
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

function Measure-Liveness90([string]$label) {
  # Up within 120 s after the update chain finished, then exactly one
  # gateway, api port bound, state running, no stale restart request, for
  # 90 s straight.
  $bad = @()
  Wait-UpdateChainDone 1800
  $deadline = (Get-Date).AddSeconds(120)
  while ((Get-Date) -lt $deadline -and -not ((Get-GatewayRoots).Count -eq 1 -and (Test-ApiPort))) { Start-Sleep -Seconds 2 }
  $end = (Get-Date).AddSeconds(90); $n = 0
  while ((Get-Date) -lt $end) {
    $roots = Get-GatewayRoots; $port = Test-ApiPort; $st = Get-GatewayState
    $n++
    $line = "[$label] t=$n roots=$($roots.Count) pids=$(($roots | ForEach-Object ProcessId) -join ',') port=$port state=$($st.gateway_state) restart_requested=$($st.restart_requested)"
    Write-Host $line
    if ($roots.Count -ne 1 -or -not $port -or $st.restart_requested -eq $true -or $st.gateway_state -ne 'running') { $bad += $line }
    Start-Sleep -Seconds 3
  }
  ,@($bad)
}

function Measure-SingleGateway([string]$label, [int]$logStart, [bool]$strictLog) {
  $bad = @()
  $bad += Measure-Liveness90 $label
  $lines = Get-LogLines
  $new = if ($lines.Count -gt $logStart) { $lines[$logStart..($lines.Count - 1)] } else { @() }
  $first = -1
  for ($i = 0; $i -lt $new.Count; $i++) { if ($new[$i] -match 'Starting Clover Gateway') { $first = $i; break } }
  $starts = @($new | Where-Object { $_ -match 'Starting Clover Gateway' }).Count
  $after = if ($first -ge 0) { @($new[$first..($new.Count - 1)] | Where-Object { $_ -match 'Gateway restart requested|Stopping gateway|service-restart requested|planned --replace takeover|Replacing existing gateway' }) } else { @() }
  Write-Host "[$label] post-update gateway starts=$starts; restart/takeover lines after first post-update start=$($after.Count)"
  $after | ForEach-Object { Write-Host "  $_" }
  if ($first -lt 0) { $bad += "no post-update 'Starting Clover Gateway' in gateway.log" }
  if ($strictLog -and $after.Count -gt 0) { $bad += "[old-updater] restart/takeover after the post-update start" }
  if ($strictLog -and $starts -ne 1) { $bad += "[old-updater] expected exactly 1 post-update gateway start, saw $starts" }
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

function Set-MainIsNext([string]$nextBranch) {
  # Second update on fixed code: move local main onto the real fix commit it
  # already reads as (same tree, clean), then let main read as a NEWER commit
  # so the path's exact command performs a real pull again.
  $d = $env:INSTALL_DIR
  git -C $d fetch -q origin "+refs/heads/${nextBranch}:refs/remotes/origin/${nextBranch}"
  if ($LASTEXITCODE -ne 0) { throw "fetch $nextBranch failed" }
  $fixSha = (git -C $d rev-parse "refs/remotes/origin/$env:FIX_BRANCH").Trim()
  $nextSha = (git -C $d rev-parse "refs/remotes/origin/$nextBranch").Trim()
  git -C $d update-ref refs/heads/main $fixSha
  Remove-MainFromHistory $nextSha $env:MAIN_SHA
  git -C $d replace -f $env:MAIN_SHA $nextSha
  $dirty = git -C $d status --porcelain --untracked-files=no
  Write-Host "local main at $(git -C $d rev-parse --short HEAD); main $env:MAIN_SHA now reads as $nextBranch $nextSha; tracked changes: $(@($dirty).Count)"
}

function Start-ConfigTrace([string]$label) {
  # Evidence for config migrations: every write to config.yaml (size/mtime
  # change) with the schema version, whether display.cleanup_progress is
  # still set, and which Clover processes were alive at that moment.
  $out = "$env:RUNNER_TEMP\config-trace.log"
  "=== $label" | Add-Content $out
  Start-Job -ArgumentList "$env:CLOVER_HOME\config.yaml", $out -ScriptBlock {
    param($cfg, $out)
    $last = ""
    while ($true) {
      $state = "missing"
      try {
        $item = Get-Item $cfg -ErrorAction Stop
        $t = Get-Content $cfg -Raw -ErrorAction Stop
        $ver = ([regex]::Match($t, '(?m)^_config_version:\s*(\d+)')).Groups[1].Value
        $cp = [regex]::IsMatch($t, '(?m)^  cleanup_progress:')
        $state = "size=$($item.Length) ver=$ver cleanup_key=$cp"
      } catch { }
      if ($state -ne $last) {
        $procs = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match 'clover' -and $_.Name -match 'python|clover' } | ForEach-Object {
          $c = ($_.CommandLine -replace '^.*?(clover_cli[.]\w+|clover\.exe"?|update_restart_watcher\S*)', '$1') -replace '\s+', ' '
          "$($_.ProcessId)<$($_.ParentProcessId) $($c.Substring(0, [Math]::Min(110, $c.Length)))" }) -join "`n      "
        "$(Get-Date -Format HH:mm:ss.fff) $state :: $procs" | Add-Content $out
        $last = $state
      }
      Start-Sleep -Milliseconds 300
    }
  }
}

function Stop-ConfigTrace($job) {
  if ($job) { Stop-Job $job -ErrorAction SilentlyContinue; Remove-Job $job -Force -ErrorAction SilentlyContinue }
  Write-Host "--- config.yaml trace ---"
  Get-Content "$env:RUNNER_TEMP\config-trace.log" -ErrorAction SilentlyContinue | Select-Object -Last 120 | Out-Host
}

function Invoke-PathUpdate([string]$label, [string[]]$cliArgs) {
  # The trace keeps running through the hand-off child, the watcher and the
  # first post-update gateway start; the assert step stops and prints it.
  $global:ConfigTraceJob = Start-ConfigTrace $label
  return (Invoke-PathUpdateInner $label $cliArgs)
}

function Invoke-PathUpdateInner([string]$label, [string[]]$cliArgs) {
  # Runs the user's exact update for $env:UPDATE_PATH (or the given CLI args)
  # and returns the update's exit code; output lands in update-output.txt.
  Remove-Item "$env:RUNNER_TEMP\update-output.txt" -ErrorAction SilentlyContinue
  if ($cliArgs -or $env:UPDATE_PATH -eq 'cli') {
    if (-not $cliArgs) { $cliArgs = @('update', '--yes') }
    Write-Host "[$label] running: clover $($cliArgs -join ' ')"
    & "$env:CLOVER_BIN" @cliArgs 2>&1 | Tee-Object -FilePath "$env:RUNNER_TEMP\update-output.txt" | Out-Host
    return $LASTEXITCODE
  }
  Write-Host "[$label] sending /update to the gateway over IRC"
  New-Item -ItemType File -Force -Path "$env:RUNNER_TEMP\irc.trigger" | Out-Null
  $deadline = (Get-Date).AddSeconds(120)
  while ((Get-Date) -lt $deadline -and (Get-UpdateProcesses).Count -eq 0) { Start-Sleep -Seconds 1 }
  Write-Host "[$label] updater processes: $((Get-UpdateProcesses | ForEach-Object { "$($_.ProcessId)<-$($_.ParentProcessId)" }) -join ', ')"
  Wait-UpdateChainDone 1800
  # The restarted gateway reports the result back on the chat.
  $deadline = (Get-Date).AddSeconds(300)
  while ((Get-Date) -lt $deadline -and -not (Test-Path "$env:CLOVER_HOME\.update_exit_code") -and (Test-Path "$env:CLOVER_HOME\.update_pending.json")) { Start-Sleep -Seconds 3 }
  $code = if (Test-Path "$env:CLOVER_HOME\.update_exit_code") { (Get-Content "$env:CLOVER_HOME\.update_exit_code" -Raw).Trim() } else { $null }
  if ($null -eq $code) {
    $side = try { Get-Content "$env:CLOVER_HOME\.clover-last-update" -Raw | ConvertFrom-Json } catch { $null }
    $code = if ($side -and $side.outcome -eq 'success') { 0 } else { "unknown (outcome=$($side.outcome))" }
  }
  # The gateway rotates the chat run's transcript into logs\update-output.last.txt
  # once it has sent the result (D8); take whichever is there.
  $transcript = @("$env:CLOVER_HOME\.update_output.txt", "$env:CLOVER_HOME\logs\update-output.last.txt") | Where-Object { Test-Path $_ } | Select-Object -First 1
  if ($transcript) { Copy-Item $transcript "$env:RUNNER_TEMP\update-output.txt" -Force; Write-Host "[$label] transcript: $transcript" }
  Write-Host "--- [$label] updater output (.update_output.txt, tail) ---"
  Get-Content "$env:RUNNER_TEMP\update-output.txt" -ErrorAction SilentlyContinue | Select-Object -Last 80 | Out-Host
  Write-Host "--- [$label] IRC transcript (bot replies, tail) ---"
  Get-Content "$env:RUNNER_TEMP\irc.log" | Select-String -Pattern '<< PRIVMSG ant|-> :ant' | Select-Object -Last 25 | ForEach-Object { $_.Line } | Out-Host
  return $code
}

function Assert-UpdatePass([string]$label, [int]$logStart, $exitCode, [string]$landedPath, [bool]$oldUpdater = $false) {
  # $oldUpdater: the update was executed by the from_ref's OWN (unfixed)
  # updater code. Criteria only that code decides are reported, not failed;
  # everything the fix can influence is still required. Updates run by the
  # fixed code ($oldUpdater = $false) are held to every criterion.
  $bad = @()
  $bad += Measure-SingleGateway $label $logStart $true
  Stop-ConfigTrace $global:ConfigTraceJob
  $out = (Get-Content "$env:RUNNER_TEMP\update-output.txt" -Raw -ErrorAction SilentlyContinue)
  $bad += Measure-UpdateOutcome $label $exitCode "$out" $landedPath
  if ($oldUpdater) {
    $old = @($bad | Where-Object { $_ -like '*`[old-updater`]*' })
    $old | ForEach-Object { Write-Host "OLD-UPDATER FINDING [$label]: $_" }
    $bad = @($bad | Where-Object { $_ -notlike '*`[old-updater`]*' })
  }
  git -C "$env:INSTALL_DIR" log --oneline -1 | Out-Host
  if ($bad.Count) {
    Write-Host "FAIL [$label]:"; $bad | Select-Object -First 30 | ForEach-Object { Write-Host "  $_" }
    Write-Host "--- gateway.log tail ---"; Get-LogLines | Select-Object -Last 150 | Out-Host
    throw "[$label] failed $($bad.Count) criteria"
  }
  Write-Host "PASS [$label]: exit 0, one gateway 90s, port bound, running, 1 start, no restart after it, no failed backup, outcome ok, telegram cleanup_progress True"
}

function Measure-UpdateOutcome([string]$label, $exitCode, [string]$outputText, [string]$landedPath) {
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
    if ($failedBackup.Count) { $bad += "[old-updater] receipt: pre-update backup step failed ($($failedBackup[0].detail))" }
    if ($receipt.outcome -eq 'running') { $bad += "[old-updater] receipt never finalized (outcome=running)" }
    $failedSteps = @($receipt.steps | Where-Object { -not $_.ok } | ForEach-Object { $_.name })
    if ($failedSteps.Count) { Write-Host "[$label] receipt failed steps: $($failedSteps -join ', ')" }
  } else { Write-Host "[$label] no update receipt" }
  if ($outputText -match '(?i)backup step failed|backup failed') { $bad += "[old-updater] update output mentions a failed backup" }
  $sidecar = $null
  try { $sidecar = Get-Content "$env:CLOVER_HOME\.clover-last-update" -Raw | ConvertFrom-Json } catch { }
  Write-Host "[$label] .clover-last-update: $($sidecar | ConvertTo-Json -Compress)"
  if ($sidecar -and $sidecar.outcome -eq 'failed') { $bad += "[old-updater] .clover-last-update outcome=failed ($($sidecar.detail))" }
  if (-not $sidecar) { $bad += "[old-updater] no .clover-last-update written" }
  $cp = Test-CleanupProgress
  Write-Host "[$label] telegram cleanup_progress resolves to: $cp"
  if ("$cp" -ne "True") {
    $bad += "telegram cleanup_progress resolves to '$cp', not True"
    # Why: registry, raw display value types, and a dry run of the v41 rewrite.
    $py = "$env:INSTALL_DIR\venv\Scripts\python.exe"
    $diag = @'
import copy, json
from clover_cli import config as c, config_migrations as m
raw = c.read_raw_config()
d = raw.get("display") or {}
print("registry:", [v for v, _ in m.MIGRATIONS][-4:])
print("version:", c.check_config_version(), "raw _config_version:", repr(raw.get("_config_version")))
print("display keys:", {k: (repr(v), type(v).__name__) for k, v in d.items() if not isinstance(v, dict)})
print("display.platforms:", repr(d.get("platforms")))
fn = getattr(m, "_v41_rewrite_config", None)
print("v41 dry run:", fn(copy.deepcopy(raw)) if fn else "NO _v41_rewrite_config")
'@
    Push-Location $env:INSTALL_DIR
    try { & $py -c $diag 2>&1 | ForEach-Object { Write-Host "  [cleanup-diag] $_" } } finally { Pop-Location }
  }
  if (-not $landedPath) { $landedPath = "clover_cli\config_migrations.py" }
  $landed = Select-String -Path "$env:INSTALL_DIR\clover_cli\update_cmd.py" -Pattern '_hand_off_windows_gateway_resume' -Quiet
  if (-not $landed -or -not (Test-Path "$env:INSTALL_DIR\$landedPath")) { $bad += "target code did not land ($landedPath; main moved during the run?)" }
  ,@($bad)
}

function Write-EmilioReceipt {
  # D1/D2: the receipt Emilio's install carried for weeks (E4 in his
  # report): a refused Telegram /update that never pulled, fleet [], and a
  # plan naming an old code_sha. On unfixed code it armed the fleet-restart
  # catch-up on every later no-op update, which killed the gateway.
  $old = $env:FROM_REF
  $gw = @(Get-GatewayRoots | ForEach-Object ProcessId) | Select-Object -First 1
  $sha = [ordered]@{ sha = $old; short_sha = $old.Substring(0, 8); version = '1.0.0'; source = 'git' }
  $r = [ordered]@{
    outcome = 'refused'; exit_code = 2; stop_reason = 'sys.exit(2)'
    pre_update = $sha; post_update = $sha; gateway_restart = @{}; fleet = @()
    steps = @([ordered]@{ name = 'pre_update_backup'; ok = $false; detail = 'disabled or failed'; at = (Get-Date).ToUniversalTime().ToString('o') })
    plan = [ordered]@{
      runtimes = @([ordered]@{ kind = 'gateway'; profile = 'default'; pid = [int]$gw; supervisor = 'manual'; code_sha = $old; code_version = '1.0.0'; restart_via = 'manual'; detail = @{} })
      install_method = 'git'
    }
  }
  $dir = "$env:CLOVER_HOME\logs\update_receipts"
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $r | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 "$dir\latest.json"
  Write-Host "seeded Emilio-shaped latest.json (refused, fleet [], code_sha $($old.Substring(0,8)), gateway pid $gw)"
}

function Assert-NoPullKeepsOneGateway([string]$label, $exitCode) {
  $bad = @()
  if ("$exitCode" -ne "0") { $bad += "update exit=$exitCode" }
  $bad += Measure-Liveness90 $label
  $out = (Get-Content "$env:RUNNER_TEMP\update-output.txt" -Raw -ErrorAction SilentlyContinue)
  if ("$out" -match 'Pending fleet restart|did not restart running gateways') { $bad += "the fleet-restart catch-up ran on a no-pull update" }
  if ($bad.Count) {
    Write-Host "FAIL [$label]:"; $bad | Select-Object -First 30 | ForEach-Object { Write-Host "  $_" }
    Write-Host "--- update output ---"; "$out" | Out-Host
    Write-Host "--- gateway.log tail ---"; Get-LogLines | Select-Object -Last 120 | Out-Host
    throw "[$label] failed $($bad.Count) criteria"
  }
  Write-Host "PASS [$label]: exit 0, no catch-up, exactly one gateway for 90s, port bound, running"
}

function Start-LongTurn {
  # D5: a slow agent turn through the api_server that is still running when
  # the update pauses the gateway. The fake model streams for 75 s.
  $llmLog = "$env:RUNNER_TEMP\llm.log"
  if (-not (Test-Path $llmLog)) {
    $py = (Get-Command python).Source  # system Python, never the Clover venv
    Start-Process -FilePath $py -ArgumentList @("$env:E2E\fake_llm.py", $llmLog, $env:LLM_PORT, "75") -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline -and -not ((Test-Path $llmLog) -and (Select-String -Path $llmLog -Pattern 'listening' -Quiet))) { Start-Sleep -Seconds 1 }
    if (-not (Test-Path $llmLog)) { throw "fake model server did not start" }
  }
  foreach ($kv in @(@('model.provider', 'custom'), @('model.base_url', "http://127.0.0.1:$env:LLM_PORT/v1"), @('model.default', 'fake-slow'), @('model.api_key', 'fake-e2e-key'))) {
    & "$env:CLOVER_BIN" config set $kv[0] $kv[1] | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "clover config set $($kv[0]) failed" }
  }
  $body = @{ model = 'clover-c1'; stream = $false; messages = @(@{ role = 'user'; content = 'LONG-TURN: answer when you are done thinking.' }) } | ConvertTo-Json -Depth 5
  $job = Start-Job -ArgumentList $env:API_PORT, $body -ScriptBlock {
    param($port, $body)
    try {
      $r = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/v1/chat/completions" -Headers @{ Authorization = 'Bearer win-update-paths-key-0123456789' } -ContentType 'application/json' -Body $body -TimeoutSec 900
      "STATUS=ok CONTENT=$($r.choices[0].message.content)"
    } catch { "STATUS=error $($_.Exception.Message) $($_.ErrorDetails.Message)" }
  }
  $deadline = (Get-Date).AddSeconds(120)
  while ((Get-Date) -lt $deadline -and -not (Select-String -Path $llmLog -Pattern 'long turn started' -Quiet)) { Start-Sleep -Seconds 1 }
  if (-not (Select-String -Path $llmLog -Pattern 'long turn started' -Quiet)) {
    Receive-Job $job -ErrorAction SilentlyContinue | Out-Host
    Get-Content $llmLog | Out-Host; Get-LogLines | Select-Object -Last 60 | Out-Host
    throw "the long turn never reached the model"
  }
  Write-Host "long turn in flight at $(Get-Date -Format HH:mm:ss)"
  $job
}

function Assert-LongTurnSurvived($job, [int]$logStart, [string]$label) {
  $bad = @()
  $null = Wait-Job $job -Timeout 900
  $result = "$(Receive-Job $job -ErrorAction SilentlyContinue)"
  Write-Host "[$label] long turn result: $result"
  Write-Host "--- model log ---"; Get-Content "$env:RUNNER_TEMP\llm.log" | Select-Object -Last 20 | Out-Host
  if ($result -notmatch 'STATUS=ok' -or $result -notmatch 'LONG-TURN-DONE') { $bad += "the long turn did not complete: $result" }
  if (Select-String -Path "$env:RUNNER_TEMP\llm.log" -Pattern 'CUT OFF' -Quiet) { $bad += "the model stream was cut off mid-turn" }
  $lines = Get-LogLines
  $new = if ($lines.Count -gt $logStart) { $lines[$logStart..($lines.Count - 1)] } else { @() }
  $cut = @($new | Where-Object { $_ -match 'interrupting remaining work|Restart after-turn wait timed out' })
  $cut | ForEach-Object { $bad += "gateway.log: $_" }
  $waited = "$(Get-Content "$env:RUNNER_TEMP\update-output.txt" -Raw -ErrorAction SilentlyContinue)" -match 'in-flight turn'
  Write-Host "[$label] updater said it waited for the in-flight turn: $waited"
  if (-not $waited) { $bad += "the updater never said it was waiting for the in-flight turn" }
  if ($bad.Count) {
    Write-Host "FAIL [$label long turn]:"; $bad | ForEach-Object { Write-Host "  $_" }
    Write-Host "--- gateway.log tail ---"; $new | Select-Object -Last 120 | Out-Host
    throw "[$label] the active turn was amputated by the update"
  }
  Write-Host "PASS [$label long turn]: the turn running at update time completed; nothing interrupted; the updater waited for it"
}
