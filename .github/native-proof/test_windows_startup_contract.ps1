# Portable PowerShell seam tests. No installer, gateway, task or service starts.
param([Parameter(Mandatory=$true)][string]$Python, [string]$JUnit)
$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$path = Join-Path $PSScriptRoot 'windows.ps1'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$sourceText = Get-Content $path -Raw
function Import-Function([string]$name) {
  $node = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }.GetNewClosure(), $true)
  if ($node) { . ([scriptblock]::Create($node.Extent.Text)); Set-Item "Function:script:$name" (Get-Item "Function:$name").ScriptBlock }
}
foreach ($name in @('Get-NativeProofPythonIdentity','Assert-NativeProofBasePython','Read-ExactGateway')) { Import-Function $name }
. (Join-Path $PSScriptRoot '../win-e2e/supervisor-proof.ps1')
function Assert-True($value, [string]$message) { if (-not $value) { throw $message } }
$results = @()
function Case([string]$name, [scriptblock]$body) {
  try { & $body; $script:results += [pscustomobject]@{ name=$name; error=$null }; Write-Host "PASS $name" }
  catch { $script:results += [pscustomobject]@{ name=$name; error="$($_.Exception.Message)" }; Write-Host "FAIL ${name}: $_" }
}
$temp = Join-Path ([IO.Path]::GetTempPath()) ('native-win-startup-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $temp | Out-Null
$env:INSTALL_DIR = 'C:\proof\install'; $env:API_PORT = '8791'; $env:E2E = 'C:\proof\helpers'
$evidence = $temp
try {
  Case 'preinstall interpreter survives a real dot-source variable collision' {
    # Execute the real preinstall assignment, then the real import seam.
    $init = @($ast.EndBlock.Statements | Where-Object { $_.Extent.Text -match '^\$systemPy\s*=|^Set-Variable -Name NativeProofBasePython ' })
    Assert-True ($init.Count -eq 1) 'Missing unique preinstall identity capture'
    # Get-Command -CommandType Application can emit multiple Python matches on
    # a real Windows PATH. Preserve the first command, not a joined path list.
    function Get-Command { param($Name,$CommandType)
      [pscustomobject]@{ Source=$Python }
      [pscustomobject]@{ Source=(Join-Path $temp 'other-python.exe') }
    }
    . ([scriptblock]::Create($init[0].Extent.Text))
    . (Join-Path $PSScriptRoot '../win-e2e/supervisor-proof.ps1')
    Assert-True ($script:NativeProofBasePython.executable -eq $Python) 'Actual helper changed preinstall identity'
    Assert-True ((Get-Variable NativeProofBasePython -Scope Script).Options -band [System.Management.Automation.ScopedItemOptions]::ReadOnly) 'Identity is not protected from reassignment'
    $collision = Join-Path $temp 'trusted-collision.ps1'
    Set-Content $collision '$systemPy = "$env:INSTALL_DIR\venv\Scripts\python.exe"'
    . $collision
    function Start-Process { param($FilePath,$ArgumentList,$WindowStyle) $script:startedPython=$FilePath }
    $chat = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.IfStatementAst] -and $n.Extent.Text -match '^if \(\$env:UPDATE_PATH -eq ''chat''\)' }, $true) | Select-Object -First 1
    $env:UPDATE_PATH = 'chat'; $lines = @()
    . ([scriptblock]::Create($chat.Extent.Text))
    Assert-True ($script:startedPython -eq $Python) 'IRC did not use the captured base interpreter'
  }
  Case 'venv IRC interpreter still refuses before process start' {
    $identity = [pscustomobject]@{ command_source="$env:INSTALL_DIR\venv\Scripts\python.exe"; executable="$env:INSTALL_DIR\venv\Scripts\python.exe"; base_executable=$Python; prefix="$env:INSTALL_DIR\venv"; base_prefix='C:\Python' }
    $refused = $false
    try { Assert-NativeProofBasePython $identity } catch { $refused = $_ -match 'IRC fixture must use system Python' }
    Assert-True $refused 'Venv guard failed to refuse'
  }
  # Real helper functions consume these trusted CIM/task/port boundary fixtures.
  function Get-CimInstance { param($ClassName,$Filter)
    if ($ClassName -eq 'Win32_Service') { return $script:schedule }
    if ($Filter -match '^ProcessId=(\d+)$') { return $script:processes | Where-Object ProcessId -eq ([int]$Matches[1]) }
    return $script:processes
  }
  function Get-GatewayState { return $script:state }
  function Test-ApiPort { return $script:portUp }
  function Get-NetTCPConnection { param($State,$LocalPort,$ErrorAction) $script:listeners | ForEach-Object { [pscustomobject]@{ OwningProcess=$_ } } }
  function Get-ScheduledTask { param($TaskName,$ErrorAction) return $script:task }
  function Reset-Gateway([string]$setup) {
    $env:SETUP=$setup; $script:expectedSha='expected-sha'; $script:portUp=$true
    $script:state=[pscustomobject]@{ pid=100; gateway_state='running'; restart_requested=$false; start_time=123; code_sha=$script:expectedSha }
    $script:schedule=[pscustomobject]@{ State='Running'; ProcessId=9 }
    $parent=if ($setup -eq 'troy') { 9 } else { 8 }
    $script:processes=@(
      [pscustomobject]@{ ProcessId=100; ParentProcessId=$parent; Name='pythonw.exe'; ExecutablePath="$env:INSTALL_DIR\venv\Scripts\pythonw.exe"; CommandLine="`"$env:INSTALL_DIR\venv\Scripts\pythonw.exe`" -m clover_cli.main gateway run" },
      [pscustomobject]@{ ProcessId=9; ParentProcessId=1; Name='svchost.exe'; ExecutablePath='C:\Windows\System32\svchost.exe'; CommandLine='svchost.exe -s Schedule SECRET-SENTINEL' },
      [pscustomobject]@{ ProcessId=8; ParentProcessId=1; Name='wscript.exe'; ExecutablePath='C:\Windows\System32\wscript.exe'; CommandLine='wscript.exe Clover_Gateway.vbs' })
    $script:listeners=@(100)
    $script:task=[pscustomobject]@{ State='Running'; TaskName='Clover_Troy_Gateway' }
    Remove-Item (Join-Path $evidence 'gateway-refusal.json') -ErrorAction SilentlyContinue
  }
  foreach ($setup in @('troy','zyra')) {
    Case "$setup accepts a matching owned gateway" {
      Reset-Gateway $setup
      $actual=Read-ExactGateway
      Assert-True ($actual.pid -eq 100) 'Wrong returned identity'
    }
    Case "$setup reports base-child exclusion rather than silently trusting health" {
      Reset-Gateway $setup
      $script:processes += [pscustomobject]@{ ProcessId=101; ParentProcessId=100; Name='pythonw.exe'; ExecutablePath='C:\base\pythonw.exe'; CommandLine='"C:\base\pythonw.exe" -m clover_cli.main gateway run' }
      $script:state.pid=101; $script:listeners=@(101)
      $refused=$false
      try { $null=Read-ExactGateway } catch { $refused=$true }
      Assert-True $refused 'Unproved base interpreter ownership was waived'
      $diag=Get-Content (Join-Path $evidence 'gateway-refusal.json') -Raw | ConvertFrom-Json
      Assert-True ($diag.failed_predicates.Count -eq 1 -and $diag.failed_predicates[0] -eq 'state_pid_matches_install_command') 'Base-child counterexample was not identified precisely'
      Assert-True ($diag.roots[0].ProcessId -eq 100 -and $diag.state.pid -eq 101) 'Root/state identities were conflated'
    }
    foreach ($mutation in @('wrong-sha','wrong-state-pid','wrong-listener','duplicate-root','missing-root','missing-host','stopped-task','stopped-schedule')) {
      if ($setup -eq 'zyra' -and $mutation -eq 'stopped-task') { continue }
      Case "$setup refuses $mutation with per-predicate evidence" {
        Reset-Gateway $setup
        $expectedPredicate = @{ 'wrong-sha'='expected_source_sha'; 'wrong-state-pid'='state_pid_matches_install_command'; 'wrong-listener'='listener_matches_state_pid'; 'duplicate-root'='one_gateway_root'; 'missing-root'='one_gateway_root'; 'missing-host'='expected_host_mode'; 'stopped-task'='troy_task_running'; 'stopped-schedule'='schedule_still_running' }[$mutation]
        switch ($mutation) {
          'wrong-sha' { $script:state.code_sha='wrong' }
          'wrong-state-pid' { $script:state.pid=101 }
          'wrong-listener' { $script:listeners=@(101) }
          'duplicate-root' { $script:processes += [pscustomobject]@{ ProcessId=102; ParentProcessId=8; Name='python.exe'; ExecutablePath="$env:INSTALL_DIR\venv\Scripts\python.exe"; CommandLine="`"$env:INSTALL_DIR\venv\Scripts\python.exe`" -m clover_cli.main gateway run" } }
          'missing-root' { $script:processes=@() }
          'missing-host' { $script:processes[0].ParentProcessId=if ($setup -eq 'troy') { 8 } else { 9 } }
          'stopped-task' { $script:task.State='Ready' }
          'stopped-schedule' { $script:schedule.State='Stopped' }
        }
        $refused=$false
        try { $null=Read-ExactGateway } catch { $refused=$true }
        Assert-True $refused 'Invalid gateway was accepted'
        $diagPath=Join-Path $evidence 'gateway-refusal.json'
        Assert-True (Test-Path $diagPath) 'Refusal lost per-predicate diagnostic'
        $diag=Get-Content $diagPath -Raw | ConvertFrom-Json
        Assert-True ($diag.failed_predicates -contains $expectedPredicate) "Wrong predicate for $mutation"
        Assert-True ($null -ne $diag.processes -and $null -ne $diag.listeners -and $null -ne $diag.schedule) 'Diagnostic omitted boundary observations'
        Assert-True ((Get-Content $diagPath -Raw) -notmatch 'SECRET-SENTINEL') 'Diagnostic leaked ancestor command line'
      }
    }
  }
} finally { Remove-Item $temp -Recurse -Force }
if ($JUnit) {
  $xml = New-Object System.Xml.XmlDocument
  $suite = $xml.CreateElement('testsuite'); $suite.SetAttribute('name','windows-startup-contract'); $suite.SetAttribute('tests',"$($results.Count)"); $suite.SetAttribute('failures',"$(@($results | Where-Object error).Count)"); $null=$xml.AppendChild($suite)
  foreach ($result in $results) { $case=$xml.CreateElement('testcase'); $case.SetAttribute('name',$result.name); if ($result.error) { $failure=$xml.CreateElement('failure'); $failure.InnerText=$result.error; $null=$case.AppendChild($failure) }; $null=$suite.AppendChild($case) }
  $xml.Save($JUnit)
}
if (@($results | Where-Object error).Count) { exit 1 }
