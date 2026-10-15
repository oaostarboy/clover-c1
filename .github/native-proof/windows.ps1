# Adapt the proven Troy/Zyra and local-IRC helpers, without replacement refs.
param([ValidateSet('fresh','old2new','new2next')][string]$Mode)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted') { throw 'Disposable hosted runner required' }
function Get-NativeProofPythonIdentity([string]$executable) {
  $raw = & $executable -I -c 'import json,sys; print(json.dumps(dict(executable=sys.executable,base_executable=sys._base_executable,prefix=sys.prefix,base_prefix=sys.base_prefix)))'
  if ($LASTEXITCODE -ne 0) { throw 'Cannot establish fixture Python identity' }
  $identity = $raw | ConvertFrom-Json
  $identity | Add-Member -NotePropertyName command_source -NotePropertyValue $executable
  return $identity
}
function Assert-NativeProofBasePython($identity) {
  $venv = "$env:INSTALL_DIR\venv"
  $paths = @($identity.command_source, $identity.executable, $identity.base_executable)
  if (-not $identity.executable -or -not $identity.base_executable -or -not $identity.prefix -or
      $identity.prefix -ne $identity.base_prefix -or @($paths | Where-Object {
        $_ -and ($_.Equals($venv, [StringComparison]::OrdinalIgnoreCase) -or
        $_.StartsWith("$venv\", [StringComparison]::OrdinalIgnoreCase))
      }).Count) { throw 'IRC fixture must use system Python, never a venv holder' }
}
# Own this preinstall identity independently of generic variables in sourced helpers.
Set-Variable -Name NativeProofBasePython -Scope Script -Option ReadOnly -Value (Get-NativeProofPythonIdentity ((Get-Command python -CommandType Application | Select-Object -First 1).Source))
$helpers = "$env:GITHUB_WORKSPACE\helpers"
$source = "$env:GITHUB_WORKSPACE\source"
$proof = "$helpers\.github\native-proof\proof.py"
$evidence = "$env:RUNNER_TEMP\evidence"
New-Item -ItemType Directory -Force $evidence | Out-Null
& python $proof preflight --repo $source --output "$evidence\plan.json"
if ($LASTEXITCODE -ne 0) { throw 'Source pins moved since plan job' }
$env:CLOVER_HOME = "$env:LOCALAPPDATA\clover"
$env:INSTALL_DIR = "$env:CLOVER_HOME\clover-c1"
$script:NativeProofBasePython | ConvertTo-Json -Depth 6 | Set-Content "$evidence\base-python-before-install.json" -Encoding utf8
Assert-NativeProofBasePython $script:NativeProofBasePython
if (Test-Path $env:CLOVER_HOME) { throw 'Runner already has Clover data' }
if (Get-Command clover -ErrorAction SilentlyContinue) { throw 'Runner already has Clover on PATH' }
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
$env:API_PORT = '8791'; $env:IRC_PORT = '6667'
$env:E2E = "$helpers\.github\win-e2e"
$from = if ($Mode -eq 'old2new') { $env:OLD_SHA } else { $env:TARGET_REF }
$to = if ($Mode -eq 'new2next') { $env:NEXT_SHA } else { $env:TARGET_REF }
$toTree = if ($Mode -eq 'new2next') { $env:NEXT_TREE } else { $env:TARGET_TREE }
New-Item -ItemType Directory -Force (Split-Path $env:INSTALL_DIR) | Out-Null
git clone --branch $env:NEXT_REF https://github.com/oaostarboy/clover-c1.git $env:INSTALL_DIR
if ($LASTEXITCODE -ne 0) { throw 'pre-clone failed' }
git -C $env:INSTALL_DIR checkout --detach $from
if ($LASTEXITCODE -ne 0) { throw 'starting commit missing' }
git -C $source show "$($env:TARGET_REF):scripts/install.ps1" | Set-Content "$env:RUNNER_TEMP\install.ps1" -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw 'pinned installer missing' }
function Save-NpmEvidence([string]$destination) {
  # The installer logs are outside the checkout. Preserve them before any
  # identity refusal, including incomplete debug logs from a killed npm tree.
  $npmEvidence = Join-Path $destination 'npm'
  New-Item -ItemType Directory -Force $npmEvidence | Out-Null
  Get-ChildItem -LiteralPath $env:TEMP -Filter 'clover-npm-*' -ErrorAction SilentlyContinue |
    Copy-Item -Destination $npmEvidence -Recurse -Force -ErrorAction Continue
}
try {
  & "$env:RUNNER_TEMP\install.ps1" -Branch $env:NEXT_REF -Commit $from -NonInteractive -SkipComputerUse -CloverHome $env:CLOVER_HOME -InstallDir $env:INSTALL_DIR 2>&1 | Tee-Object "$evidence\install.log" | Out-Host
  $installerExit = $LASTEXITCODE
} finally {
  Save-NpmEvidence $evidence
}
if ($installerExit -ne 0) { throw 'installer failed' }
$script:py = "$env:INSTALL_DIR\venv\Scripts\python.exe"
$env:CLOVER_BIN = "$env:INSTALL_DIR\venv\Scripts\clover.exe"
function Invoke-Proof {
  & $script:py $proof @args
  if ($LASTEXITCODE -ne 0) { throw "Proof command failed: $args" }
}
New-Item -ItemType Directory -Force "$env:RUNNER_TEMP\neutral" | Out-Null
Set-Location "$env:RUNNER_TEMP\neutral"
$fromTree = (git -C $source rev-parse "$from^{tree}").Trim()
if ($LASTEXITCODE -ne 0) { throw 'starting tree missing' }
Invoke-Proof identity --repo $env:INSTALL_DIR --sha $from --tree $fromTree --output "$evidence\before-imports.json"
if ($Mode -ne 'fresh') {
  git -C $env:INSTALL_DIR fetch --no-tags $source $to
  if ($LASTEXITCODE -ne 0) { throw 'target object fetch failed' }
  Invoke-Proof origin --repo $env:INSTALL_DIR --mirror "$env:RUNNER_TEMP\origin.git" --target $to
}
. "$env:E2E\supervisor-proof.ps1"
# The inherited Set-MainIsTarget/expected-failure helpers are NEVER invoked.
# Ordinary git objects in a new bare origin drive the user's default command.
function Read-ExactGateway {
  $observationErrors = @(); $st = $null; $roots = @()
  try { $st = Get-GatewayState } catch { $observationErrors += 'state_query_failed' }
  # The shared helper emits a single array object (including for zero roots).
  # Unroll it here; wrapping it directly in @() hides duplicate gateways.
  try { $roots = @(Get-GatewayRoots | ForEach-Object { $_ }) } catch { $observationErrors += 'root_query_failed' }
  $processes = @(); $listeners = @(); $schedule = $null; $task = $null
  try { $processes = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -match 'clover_cli[.]main' -and
    $_.CommandLine -match 'gateway\s+run' -and $_.CommandLine -notmatch '\s-c\s'
  }) } catch { $observationErrors += 'gateway_process_query_failed' }
  $ids = @($processes | Where-Object { $_.CommandLine.Contains($env:INSTALL_DIR) } | ForEach-Object { [int]$_.ProcessId })
  try { $listeners = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$env:API_PORT) -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique) }
  catch { $observationErrors += 'listener_query_failed' }
  try { $schedule = Get-ScheduleServiceInfo } catch { $observationErrors += 'schedule_query_failed' }
  $chains = @($roots | ForEach-Object {
    $root = $_
    $chain = @()
    try { $chain = @(Get-AncestorChain ([int]$root.ProcessId) | ForEach-Object { $_ }) }
    catch { $observationErrors += 'ancestor_query_failed' }
    # Keep only PIDs/names, never ancestor command lines (which may contain secrets).
    [pscustomobject]@{ root_pid = [int]$root.ProcessId; ancestors = @($chain | ForEach-Object {
      $parts = $_ -split ':', 3
      [pscustomobject]@{ pid = [int]$parts[0]; name = $parts[1] }
    }) }
  })
  $viaSchedule = $schedule -and [int]$schedule.ProcessId -gt 0 -and
    @($chains | ForEach-Object { $_.ancestors } | Where-Object { $_.pid -eq [int]$schedule.ProcessId }).Count -gt 0
  if ($env:SETUP -eq 'troy') {
    try { $task = Get-ScheduledTask -TaskName 'Clover_Troy_Gateway' -ErrorAction Stop }
    catch { $observationErrors += 'troy_task_query_failed' }
  }
  $predicates = [ordered]@{
    one_gateway_root = ($roots.Count -eq 1)
    api_port_reachable = [bool](Test-ApiPort)
    state_running = ($st.gateway_state -eq 'running')
    restart_clear = ($st.restart_requested -eq $false)
    start_identity_present = [bool]$st.start_time
    state_pid_matches_install_command = ($ids -contains [int]$st.pid)
    one_listener_owner = ($listeners.Count -eq 1)
    listener_matches_state_pid = ($listeners.Count -eq 1 -and $listeners[0] -eq [int]$st.pid)
    expected_source_sha = ($st.code_sha -eq $script:expectedSha)
    # Schedule remaining Running is an existing no-collateral-damage assertion
    # for BOTH setups. It does not imply that Zyra is Schedule-hosted.
    schedule_still_running = ($schedule.State -eq 'Running')
    expected_host_mode = ($roots.Count -eq 1 -and ($viaSchedule -eq ($env:SETUP -eq 'troy')))
    troy_task_running = ($env:SETUP -ne 'troy' -or ($task -and $task.State -eq 'Running'))
    observations_complete = ($observationErrors.Count -eq 0)
  }
  $failed = @($predicates.Keys | Where-Object { -not $predicates[$_] })
  if ($failed.Count) {
    $observation = [ordered]@{
      failed_predicates = $failed; predicates = $predicates; observation_errors = $observationErrors
      expected_sha = $script:expectedSha; setup = $env:SETUP
      state = $(if ($st) { $st | Select-Object pid,start_time,code_sha,gateway_state,restart_requested,clover_home } else { $null })
      state_script = $(if ($st.argv) { [string]$st.argv[0] } else { $null })
      roots = @($roots | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath)
      processes = @($processes | ForEach-Object { [pscustomobject]@{
        pid = [int]$_.ProcessId; parent_pid = [int]$_.ParentProcessId; executable = $_.ExecutablePath
        command_has_install_dir = $_.CommandLine.Contains($env:INSTALL_DIR)
        command_is_module_gateway_run = ($_.CommandLine -match 'clover_cli[.]main' -and $_.CommandLine -match 'gateway\s+run')
      } })
      install_command_pids = $ids; listeners = $listeners; ancestors = $chains
      schedule = $schedule; via_schedule = [bool]$viaSchedule
      task = $(if ($task) { $task | Select-Object TaskName,State } else { $null })
      base_python = $script:NativeProofBasePython
    }
    # Preserve the latest complete refusal before raising, even during bounded startup.
    $observation | ConvertTo-Json -Depth 12 | Set-Content "$evidence\gateway-refusal.json" -Encoding utf8
    Write-Host ('GATEWAY_REFUSAL ' + ($observation | ConvertTo-Json -Depth 12 -Compress))
    throw "Gateway refused predicates: $($failed -join ', ')"
  }
  return $st
}
function Wait-ExactGateway {
  # Bounded startup/source convergence only. Once the 90s window starts,
  # Read-ExactGateway failures are fatal; no recovery or driver restart.
  $deadline = (Get-Date).AddSeconds(180)
  do {
    try { return Read-ExactGateway } catch {
      if ((Get-Date) -ge $deadline) { throw }
      Write-Host "Waiting for exact gateway: $_"
      Start-Sleep -Seconds 2
    }
  } while ($true)
}
function Measure-Liveness90([string]$label) {
  Wait-UpdateChainDone 1800
  Wait-GatewayUp 150
  $samples = @(); $first = Wait-ExactGateway
  $end = (Get-Date).AddSeconds(90)
  do {
    $st = Read-ExactGateway
    if ($st.pid -ne $first.pid -or $st.start_time -ne $first.start_time) { throw 'Gateway identity drift in 90 second window' }
    $samples += $st
    Start-Sleep -Seconds 3
  } while ((Get-Date) -lt $end)
  $samples | ConvertTo-Json -Depth 10 | Set-Content "$evidence\gateway-window.json" -Encoding utf8
  ,@()
}
$lines = @('API_SERVER_ENABLED=true', 'API_SERVER_KEY=native-proof-dummy-key-123456', "API_SERVER_PORT=$env:API_PORT", 'API_SERVER_HOST=127.0.0.1')
if ($env:UPDATE_PATH -eq 'chat') {
  $lines += @('IRC_SERVER=127.0.0.1', "IRC_PORT=$env:IRC_PORT", 'IRC_USE_TLS=false', 'IRC_CHANNEL=#clover', 'IRC_NICKNAME=cloverbot', 'IRC_ALLOWED_USERS=ant', 'GATEWAY_ALLOW_ALL_USERS=true')
  $fixturePython = Get-NativeProofPythonIdentity $script:NativeProofBasePython.executable
  [ordered]@{ before = $script:NativeProofBasePython; before_fixture = $fixturePython;
    current_python_command = (Get-Command python -CommandType Application).Source } |
    ConvertTo-Json -Depth 6 | Set-Content "$evidence\base-python-before-fixture.json" -Encoding utf8
  Assert-NativeProofBasePython $script:NativeProofBasePython
  Assert-NativeProofBasePython $fixturePython
  if ($fixturePython.executable -ne $script:NativeProofBasePython.executable -or
      $fixturePython.base_executable -ne $script:NativeProofBasePython.base_executable) { throw 'Fixture base interpreter identity changed' }
  Start-Process -FilePath $script:NativeProofBasePython.executable -ArgumentList @("$env:E2E\fake_ircd.py", "$env:RUNNER_TEMP\irc.log", "$env:RUNNER_TEMP\irc.trigger", $env:IRC_PORT) -WindowStyle Hidden
}
Add-Content "$env:CLOVER_HOME\.env" -Value ($lines -join "`n")
if ($env:SETUP -eq 'troy') { Start-TroyGateway } else { Start-ZyraGateway }
Wait-GatewayUp 150
$script:expectedSha = $from
$before = Wait-ExactGateway
$before | ConvertTo-Json -Depth 10 | Set-Content "$evidence\before-state.json" -Encoding utf8
$own = Show-GatewayOwnership 'before update'
if ($env:SETUP -eq 'troy' -and -not $own.ViaSchedule) { throw 'Troy is not Schedule-hosted' }
if ($env:SETUP -ne 'troy' -and $own.ViaSchedule) { throw 'Zyra unexpectedly Schedule-hosted' }
if ($env:UPDATE_PATH -eq 'chat') {
  $deadline = (Get-Date).AddSeconds(90)
  while ((Get-Date) -lt $deadline -and -not (Select-String "$env:RUNNER_TEMP\irc.log" -Pattern '<< JOIN' -Quiet -ErrorAction SilentlyContinue)) { Start-Sleep 2 }
  if (-not (Select-String "$env:RUNNER_TEMP\irc.log" -Pattern '<< JOIN' -Quiet)) { throw 'Gateway never joined local IRC fixture' }
}
if ($Mode -ne 'fresh') {
  Invoke-Proof begin --output "$evidence\begin.json"
  $logStart = (Get-LogLines).Count
  # Reassert the exact running base immediately before the update trigger.
  $null = Read-ExactGateway
  $exitCode = Invoke-PathUpdate "$env:SETUP $env:UPDATE_PATH $Mode"
  $script:expectedSha = $to
  Assert-UpdateSucceeded "$env:SETUP $env:UPDATE_PATH $Mode" $logStart $exitCode ([int]$own.Pid)
  $after = Read-ExactGateway
  if ($after.pid -eq $before.pid -or $after.start_time -eq $before.start_time) { throw 'Gateway did not restart' }
  $after | ConvertTo-Json -Depth 10 | Set-Content "$evidence\after-state.json" -Encoding utf8
  Invoke-Proof receipt --home $env:CLOVER_HOME --begin "$evidence\begin.json" --old $from --target $to --output "$evidence\update-receipt.json"
  if ($env:UPDATE_PATH -eq 'chat') {
    # Receipt/runtime success is not the final user-visible handoff.
    $deadline = (Get-Date).AddSeconds(300)
    while ((Get-Date) -lt $deadline -and -not (Select-String "$env:RUNNER_TEMP\irc.log" -Pattern 'PRIVMSG.*Clover update finished' -Quiet)) { Start-Sleep 3 }
    if (-not (Select-String "$env:RUNNER_TEMP\irc.log" -Pattern 'PRIVMSG.*Clover update finished' -Quiet)) { throw 'No current-run final chat update reply' }
  }
} else { $null = Measure-Liveness90 'fresh' }
Invoke-Proof identity --repo $env:INSTALL_DIR --sha $to --tree $toTree --output "$evidence\after-imports.json"
if ($Mode -eq 'fresh') {
  & $script:py "$helpers\.github\native-proof\native_tests.py" --repo $env:INSTALL_DIR --evidence $evidence
  if ($LASTEXITCODE -ne 0) { throw 'Native feature contracts failed' }
  Invoke-Proof identity --repo $env:INSTALL_DIR --sha $to --tree $toTree --output "$evidence\after-tests-imports.json"
}
