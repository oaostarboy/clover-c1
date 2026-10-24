param([Parameter(Mandatory=$true)][string]$Python,
      [Parameter(Mandatory=$true)][string]$Source,
      [Parameter(Mandatory=$true)][string]$Evidence,
      [Parameter(Mandatory=$true)][string]$ExpectedSid,
      [switch]$ProbeOnly)
# A bounded NATIVE SCHEMA PROBE, never an automatic-update PASS.
# Run only after the owner provisions a genuine new account and logs it on.
# This does not create/elevate/log on an account or change audit policy.
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne 'Win32NT' -or $env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted') {
    throw 'Disposable hosted Windows required'
}
if (-not $ProbeOnly) { throw 'Native schema adapter not admitted. Only -ProbeOnly is supported; no Task acceptance.' }
$probe = Join-Path $Evidence 'task-origin-probe'
if (Test-Path -LiteralPath $probe) { throw 'Refuse old probe evidence' }
New-Item -ItemType Directory -Path $probe | Out-Null
$collector = $null
$task = $null
$phase = 'account-prerequisite'
function Save-ProbeJson($Name, $Value) {
    $Value | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $probe $Name) -Encoding utf8
}
function Read-ScheduleIdentity {
    $service = Get-CimInstance Win32_Service -Filter "Name='Schedule'"
    if ($service.State -ne 'Running') { throw 'Schedule not running' }
    $process = Get-Process -Id $service.ProcessId
    @{ pid=[int]$service.ProcessId; creation_filetime=$process.StartTime.ToUniversalTime().ToFileTimeUtc(); status='running' }
}
function Read-InstalledTask {
    $xml = $folder.GetTask($taskName).Xml
    $doc = [xml]$xml
    $ns = [Xml.XmlNamespaceManager]::new($doc.NameTable)
    $ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    $principals = @($doc.SelectNodes('/t:Task/t:Principals/t:Principal', $ns))
    $actions = @($doc.SelectNodes('/t:Task/t:Actions/t:Exec', $ns))
    if ($principals.Count -ne 1 -or $actions.Count -ne 1) { throw 'Ambiguous public Task definition' }
    $p = $principals[0]
    $principalName = $p.SelectSingleNode('t:UserId', $ns).InnerText
    if ($principalName -match '^S-1-') { $sid = $principalName }
    else { $sid = ([Security.Principal.NTAccount]::new($principalName)).Translate([Security.Principal.SecurityIdentifier]).Value }
    if ($sid -ne $ExpectedSid -or $p.SelectSingleNode('t:LogonType', $ns).InnerText -ne 'InteractiveToken' -or $p.SelectSingleNode('t:RunLevel', $ns).InnerText -ne 'LeastPrivilege') {
        throw 'Public XML principal differs from the actual interactive account'
    }
    $a = $actions[0]
    if ($a.SelectSingleNode('t:Command', $ns).InnerText -ne 'wscript.exe' -or $a.SelectSingleNode('t:Arguments', $ns).InnerText -cne ('//B //Nologo "' + $launcher + '"')) {
        throw 'Not the installed public VBS Task action'
    }
    $head = (& git -C $Source rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Installed source HEAD unavailable' }
    $tree = (& git -C $Source rev-parse 'HEAD^{tree}').Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Installed source tree unavailable' }
    $dirty = @(& git -C $Source diff --name-only HEAD)
    if ($LASTEXITCODE -ne 0 -or $dirty.Count) { throw 'Installed source changed' }
    if ($head -ne $installed.sha -or $tree -ne $installed.tree) { throw 'Installed source differs from installer receipt' }
    # Pure renderer observation from the exact installed module. It never calls
    # install(), _write_task_script(), or any private lifecycle helper.
    $renderCheck = @'
import hashlib, json, os, sys
from pathlib import Path
from clover_cli import gateway_windows as g
from clover_cli.gateway import PROJECT_ROOT, _profile_arg, get_python_path
from clover_cli.config import get_clover_home
source = Path(sys.argv[1]).resolve(strict=True)
assert Path(g.__file__).resolve() == source / 'clover_cli/gateway_windows.py'
assert Path(sys.prefix).resolve() == source / 'venv'
home = str(get_clover_home())
launcher = Path(home) / 'gateway-service' / (g.get_task_name() + '.vbs')
expected = g._build_gateway_vbs_script(g._preserve_clover_home_path(get_python_path()),
    g._stable_gateway_working_dir(PROJECT_ROOT), home, _profile_arg(home)).encode('utf-8')
assert launcher.read_bytes() == expected, 'launcher differs from installed pure renderer'
print(json.dumps(dict(launcher_sha256=hashlib.sha256(expected).hexdigest(),
    expected_xml=g._build_scheduled_task_xml(g.get_task_name(), launcher, g._resolve_task_user()),
    expected_xml_is_pure_render_not_emitted_bytes=True)))
'@
    $rendered = & $Python -c $renderCheck $Source | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Launcher does not match installed code/home renderer' }
    $xmlBytes = [Text.Encoding]::UTF8.GetBytes($xml)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $xmlHash = [BitConverter]::ToString($hasher.ComputeHash($xmlBytes)).Replace('-', '').ToLowerInvariant() }
    finally { $hasher.Dispose() }
    [ordered]@{ xml=$xml; xml_sha256=$xmlHash; expected_rendered_xml=$rendered.expected_xml; launcher=$launcher;
       launcher_sha256=(Get-FileHash -LiteralPath $launcher -Algorithm SHA256).Hash;
       source_sha=$head; source_tree=$tree; home=$env:CLOVER_HOME; principal_sid=$sid;
       action_image=$actionImage; action_argv=@('wscript.exe', '//B', '//Nologo', $launcher) }
}
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    $admin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    $interactive = @($identity.Groups | ForEach-Object { $_.Value }) -contains 'S-1-5-4'
    $session = (Get-Process -Id $PID).SessionId
    Save-ProbeJson 'account.json' @{ sid=$identity.User.Value; expected_sid=$ExpectedSid; admin=$admin; interactive_group=$interactive; session_id=$session }
    if ($identity.User.Value -ne $ExpectedSid -or -not $interactive -or $session -eq 0) {
        throw 'Genuine new-account interactive logon required; service/batch/runas environment is not public InteractiveToken proof'
    }
    # The supported CLI declines into Startup on a limited token. Do not switch
    # that account to admin or fake an elevated handoff to obtain a Task.
    if (-not $admin) { throw 'Public limited-token Task registration requires real user UAC; no automatic elevation or Startup-as-Task fallback is allowed' }
    $phase = 'installed-source-prerequisite'
    if ((Resolve-Path -LiteralPath $Python).Path -ne (Join-Path $Source 'venv\Scripts\python.exe')) { throw 'Expected exact installed console Python' }
    $installed = Get-Content -Raw -LiteralPath (Join-Path $Evidence 'released-installer-source.json') | ConvertFrom-Json
    $env:CLOVER_HOME = Split-Path -Parent $Source
    $env:VIRTUAL_ENV = Join-Path $Source 'venv'
    Set-Location -LiteralPath $Source
    $taskName = (& $Python -c 'from clover_cli.gateway_windows import get_task_name; print(get_task_name())').Trim()
    if ($LASTEXITCODE -ne 0 -or $taskName -notmatch '^Clover_Gateway(?:_[A-Za-z0-9_-]+)?$') { throw 'Unsupported public Task name' }
    $launcher = Join-Path $env:CLOVER_HOME ('gateway-service\' + $taskName + '.vbs')
    $actionImage = Join-Path $env:SystemRoot 'System32\wscript.exe'
    $scheduler = New-Object -ComObject 'Schedule.Service'
    $scheduler.Connect()
    $folder = $scheduler.GetFolder('\')
    if (@($folder.GetTasks(1) | Where-Object { $_.Name -eq $taskName }).Count) { throw 'Refuse pre-existing public Task' }
    if (@(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'clover_cli.main.*gateway.*run' }).Count) { throw 'Refuse pre-existing gateway' }
    $beforeSchedule = Read-ScheduleIdentity
    Save-ProbeJson 'schedule-before.json' $beforeSchedule
    $phase = 'sdk-schema-prerequisite'
    $schemas = @()
    foreach ($provider in @('Microsoft-Windows-TaskScheduler', 'Microsoft-Windows-Security-Auditing', 'Microsoft-Windows-Kernel-Process')) {
        $metadata = Get-WinEvent -ListProvider $provider
        $schemas += @{ name=$metadata.Name; id=$metadata.Id; events=@($metadata.Events | Where-Object { $provider -ne 'Microsoft-Windows-Security-Auditing' -or $_.Id -eq 4688 } | ForEach-Object { @{ id=$_.Id; version=$_.Version; template=$_.Template } }) }
    }
    Save-ProbeJson 'sdk-provider-templates.json' $schemas
    foreach ($channel in @('Security', 'Microsoft-Windows-TaskScheduler/Operational')) {
        if (-not (Get-WinEvent -ListLog $channel).IsEnabled) { throw ('Required existing event channel disabled: ' + $channel + '; no fixture policy change') }
    }
    if ($PSEdition -eq 'Core') {
        $references = @((Get-ChildItem "$PSHOME/ref/*.dll").FullName) + @("$PSHOME/System.Diagnostics.EventLog.dll")
        Add-Type -Path (Join-Path $PSScriptRoot 'task_origin_callbacks.cs') -ReferencedAssemblies $references
    } else {
        Add-Type -Path (Join-Path $PSScriptRoot 'task_origin_callbacks.cs')
    }
    $base = (& $Python -c 'import sys; print(sys._base_executable)').Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Installed base interpreter unavailable' }
    $safeCommands = @('"' + $actionImage + '" //B //Nologo "' + $launcher + '"',
                      'wscript.exe //B //Nologo "' + $launcher + '"',
                      '"' + $Python + '" -m clover_cli.main gateway run')
    $collector = [TaskOriginCallbacks]::new(('\' + $taskName), @($actionImage, $Python, $base), $safeCommands)
    $collector.Subscribe()
    Save-ProbeJson 'armed.json' @{ utc_ticks=[DateTime]::UtcNow.Ticks; collector='EventLogWatcher/future-events'; native_adapter='NONE'; task_preexisted=$false; gateway_preexisted=$false }
    $phase = 'public-registration'
    $argv = @('-m', 'clover_cli.main', 'gateway', 'install', '--no-start-now', '--start-on-login')
    & $Python @argv *> (Join-Path $probe 'public-registration-cli.log')
    if ($LASTEXITCODE -ne 0) { throw 'Actual public CLI registration failed; no synthetic fallback' }
    $task = $folder.GetTask($taskName)
    $initial = Read-InstalledTask
    Save-ProbeJson 'installed-before.json' $initial
    $phase = 'public-task-start'
    $requested = [DateTime]::UtcNow.Ticks
    # Actual registered public Task, NOT gateway start or install --start-now.
    $instance = $task.Run($null)
    $instanceId = $instance.InstanceGuid
    Save-ProbeJson 'task-instance.json' @{ instance_guid=$instanceId; request_utc_ticks=$requested; returned_utc_ticks=[DateTime]::UtcNow.Ticks }
    # An exited public launcher is normal. EnginePID is diagnostic only.
    try { Save-ProbeJson 'task-engine-diagnostic.json' @{ engine_pid=$instance.EnginePID; engine_pid_is_NOT_action_identity=$true } }
    catch { Save-ProbeJson 'task-engine-diagnostic.json' @{ unavailable=$_.Exception.GetType().Name; engine_pid_is_NOT_action_identity=$true } }
    # One bounded collection, not retries or a raised update deadline.
    Start-Sleep -Seconds 15
    $phase = 'pre-update-readback'
    $reread = Read-InstalledTask
    Save-ProbeJson 'installed-reread.json' $reread
    if (($initial | ConvertTo-Json -Depth 10 -Compress) -cne ($reread | ConvertTo-Json -Depth 10 -Compress)) { throw 'Task/source/launcher replaced since registration' }
    $afterSchedule = Read-ScheduleIdentity
    Save-ProbeJson 'schedule-reread.json' $afterSchedule
    if ($beforeSchedule.pid -ne $afterSchedule.pid -or $beforeSchedule.creation_filetime -ne $afterSchedule.creation_filetime) { throw 'Schedule creation identity changed' }
    $phase = 'native-schema-admission'
    throw 'Native callbacks captured only. SDK action/process-creation schema remains unvalidated; no origin acceptance, stop authority, or update was attempted'
} catch {
    Save-ProbeJson 'blocker.json' @{ status='NEEDS_NATIVE_SCHEMA_PROBE'; phase=$phase; error=$_.Exception.Message; native_acceptance=$false; update_attempted=$false; account_elevated_by_fixture=$false }
    throw
} finally {
    if ($null -ne $collector) {
        $collector.Dispose()
        Save-ProbeJson 'actual-sdk-callbacks.json' @{ records=@($collector.Records); dropped=$collector.Dropped; errors=@($collector.Errors); native_adapter='NONE'; acceptance=$false }
    }
    # Do not stop a gateway, delete a Task, kill a process, or touch Schedule.
    # The surrounding account-lifetime owner must perform its identity-bound
    # cleanup. This probe grants no lifecycle authority and cannot finish updates.
}
