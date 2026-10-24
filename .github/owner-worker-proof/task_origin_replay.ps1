# Portable SDK-callback replay and PowerShell parser; NOT native certification.
param([Parameter(Mandatory=$true)][string]$Fixture)
$ErrorActionPreference = 'Stop'
foreach ($name in @('start_troy.ps1', 'start_troy_live_ancestor_diagnostic.ps1')) {
    $tokens = $null; $parseErrors = $null
    [void][Management.Automation.Language.Parser]::ParseFile((Join-Path $Fixture $name), [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
}
# Load the real SDK reference assembly supplied with PowerShell, not a stub.
if ($PSEdition -eq 'Core') {
        $references = @((Get-ChildItem "$PSHOME/ref/*.dll").FullName) + @("$PSHOME/System.Diagnostics.EventLog.dll")
        Add-Type -Path (Join-Path $Fixture 'task_origin_callbacks.cs') -ReferencedAssemblies $references
    } else {
        Add-Type -Path (Join-Path $Fixture 'task_origin_callbacks.cs')
    }
$collector = [TaskOriginCallbacks]::new('\Clover_Gateway', @('C:\fixture\python.exe'), @('"C:\fixture\python.exe" -m clover_cli.main gateway run'))
function Event-Xml($Channel, $Data) {
    '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System><Provider Name="MODELED-REPLAY-NOT-NATIVE" Guid="{00000000-0000-0000-0000-000000000001}"/><EventID>129</EventID><Version>0</Version><Channel>' + $Channel + '</Channel></System><EventData>' + $Data + '</EventData></Event>'
}
$task = Event-Xml 'Microsoft-Windows-TaskScheduler/Operational' '<Data Name="TaskName">\Clover_Gateway</Data><Data Name="EnginePID">300</Data>'
$collector.OnXml($task)
if ($collector.Records.Count -ne 1 -or $collector.Records[0].Xml -cne $task -or -not $collector.Records[0].OriginalXml) { throw 'Exact callback XML not retained' }
if ($collector.Records[0].LivePid -ne 0) { throw 'EnginePID was incorrectly mapped to an action PID' }
$collector.OnXml((Event-Xml 'Microsoft-Windows-TaskScheduler/Operational' '<Data Name="TaskName">\foreign</Data>'))
if ($collector.Records.Count -ne 1) { throw 'Foreign Task payload retained' }
$secret = 'MUST-NOT-BE-IN-CAPTURE'
$secretTask = [TaskOriginCallbacks]::new('\Clover_Gateway', @('C:\fixture\python.exe'), @())
$secretTask.OnXml((Event-Xml 'Microsoft-Windows-TaskScheduler/Operational' ('<Data Name="TaskName">\Clover_Gateway</Data><Data Name="ActionName">' + $secret + '</Data>')))
if ($secretTask.Records.Count -ne 1 -or $secretTask.Records[0].OriginalXml -or $secretTask.Records[0].Xml.Contains($secret)) { throw 'Unexpected Task action exposed' }
$secretTask.Dispose()
$collector.OnXml((Event-Xml 'Security' ('<Data Name="NewProcessName">C:\fixture\python.exe</Data><Data Name="CommandLine">' + $secret + '</Data>')))
if ($collector.Records.Count -ne 2 -or $collector.Records[1].OriginalXml -or $collector.Records[1].Xml.Contains($secret)) { throw 'Unexpected command line exposed' }
$collector.OnXml((Event-Xml 'Security' '<Data Name="NewProcessName">C:\foreign\python.exe</Data><Data Name="CommandLine">private</Data>'))
if ($collector.Records.Count -ne 2) { throw 'Foreign process payload retained' }
$collector.OnXml((Event-Xml 'Microsoft-Windows-TaskScheduler/Operational' '<Data Name="TaskName">\Clover_Gateway</Data><Data Name="TaskName">\Clover_Gateway</Data>'))
if ($collector.Errors.Count -ne 1) { throw 'Ambiguous SDK schema not refused' }
1..255 | ForEach-Object { $collector.OnXml($task) }
if ($collector.Records.Count -ne 256 -or $collector.Dropped -ne 1) { throw 'Bounded callback loss not recorded' }
$collector.Dispose()
$collector.OnXml($task)
if ($collector.Errors.Count -ne 2) { throw 'Late callback not refused' }
@{ status='PORTABLE_SDK_CALLBACK_REPLAY_ONLY'; powershell_parse=$true; callbacks_retained=256; dropped_control=1; native_acceptance=$false; native_subscription_exercised=$false } | ConvertTo-Json -Compress
