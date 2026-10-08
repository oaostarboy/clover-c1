# Helpers for the "update pre-check" Windows e2e (dot-sourced after
# supervisor-proof.ps1). The pre-check runs inside the RUNNING gateway when
# chat /update arrives, and again inside `clover update`.

function Send-ChatText([string]$text) {
  # Atomic: fake_ircd reads the trigger's first line the moment it exists, so
  # write a temp file and rename it into place.
  $tmp = "$env:RUNNER_TEMP\irc.trigger.tmp"
  Set-Content -Path $tmp -Value $text -NoNewline -Encoding utf8
  Move-Item -Force $tmp "$env:RUNNER_TEMP\irc.trigger"
}

function Get-BotLines([int]$start) {
  # Lines the gateway sent over IRC since line $start of irc.log.
  $p = "$env:RUNNER_TEMP\irc.log"
  if (-not (Test-Path $p)) { return @() }
  ,@(Get-Content $p | Select-Object -Skip $start | Where-Object { $_ -match ' << PRIVMSG \S+ :' } | ForEach-Object { ($_ -split ' << PRIVMSG \S+ :', 2)[1] })
}

function Wait-BotText([int]$start, [string]$pattern, [int]$timeoutS = 180) {
  $deadline = (Get-Date).AddSeconds($timeoutS)
  while ((Get-Date) -lt $deadline) {
    $lines = Get-BotLines $start
    if (@($lines | Where-Object { $_ -match $pattern }).Count -gt 0) {
      Start-Sleep -Seconds 4  # let the rest of a multi-line reply arrive
      return ,@(Get-BotLines $start)
    }
    Start-Sleep -Seconds 2
  }
  Write-Host "--- bot lines since $start ---"; Get-BotLines $start | Out-Host
  throw "no bot reply matching '$pattern' within ${timeoutS}s"
}

function Get-InstallFingerprint {
  # HEAD, branch, status and a hash of every tracked+untracked file's status
  # line: "unchanged" means byte-for-byte the same checkout state.
  $d = $env:INSTALL_DIR
  [pscustomobject]@{
    Head   = (git -C $d rev-parse HEAD).Trim()
    Branch = (git -C $d rev-parse --abbrev-ref HEAD).Trim()
    Status = ((git -C $d status --porcelain --untracked-files=all) -join "`n")
    Readme = (Get-FileHash "$d\README.md" -Algorithm SHA256).Hash
  }
}

function Make-DirtyWrongBranch {
  # The case that refused Clover's own update on 2026-10-07: checkout parked on
  # a feature branch with unsaved edits. Same commit, so the running code is
  # unchanged; only the checkout state differs.
  $d = $env:INSTALL_DIR
  git -C $d checkout -q -b local-work
  if ($LASTEXITCODE -ne 0) { throw "branch create failed" }
  Add-Content -Path "$d\README.md" -Value "`nlocal edit made by the pre-check proof"
  Set-Content -Path "$d\my-notes.txt" -Value "untracked user file"
  Write-Host "made the install dirty on branch local-work:"
  git -C $d status --short | Out-Host
}

function Assert-GatewayUnchanged([string]$label, [int]$gatewayPid, [int]$seconds = 60) {
  # The same gateway process stays up, alone, port bound, state running, for
  # $seconds, and no updater/restart-watcher process was ever started.
  $bad = @()
  $end = (Get-Date).AddSeconds($seconds); $n = 0
  while ((Get-Date) -lt $end) {
    $roots = Get-GatewayRoots; $port = Test-ApiPort; $st = Get-GatewayState
    $upd = Get-UpdateProcesses
    $n++
    $line = "[$label] t=$n roots=$($roots.Count) pids=$(($roots | ForEach-Object ProcessId) -join ',') port=$port state=$($st.gateway_state) restart_requested=$($st.restart_requested) updaters=$($upd.Count)"
    Write-Host $line
    if ($roots.Count -ne 1 -or [int]($roots | Select-Object -First 1).ProcessId -ne $gatewayPid -or -not $port -or $st.gateway_state -ne 'running' -or $st.restart_requested -eq $true -or $upd.Count -ne 0) { $bad += $line }
    Start-Sleep -Seconds 3
  }
  if (Test-Path "$env:CLOVER_HOME\.update_pending.json") { $bad += "[$label] .update_pending.json was written" }
  ,@($bad)
}
