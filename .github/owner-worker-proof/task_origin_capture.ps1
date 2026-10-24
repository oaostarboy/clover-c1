param([Parameter(Mandatory=$true)][string]$Evidence,
      [Parameter(Mandatory=$true)][string]$Python)
# One capture-only experiment. No install, update, logon, UAC or stop authority.
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -or $env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or $env:GITHUB_RUN_ATTEMPT -ne '1') {
    throw 'One first-attempt disposable hosted Windows capture required'
}
if (Test-Path -LiteralPath $Evidence) { throw 'Refuse existing evidence' }
New-Item -ItemType Directory -Path $Evidence | Out-Null
function Save($Name,$Value) {
    $Value | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath (Join-Path $Evidence $Name) -Encoding utf8
}
function Schedule-Identity {
    $service=Get-CimInstance Win32_Service -Filter "Name='Schedule'"
    $process=Get-Process -Id $service.ProcessId
    @{ pid=[int]$service.ProcessId; creation_filetime=$process.StartTime.ToUniversalTime().ToFileTimeUtc(); status=$service.State }
}
$collector=$null
$errors=[Collections.Generic.List[string]]::new()
$phase='native-environment'
$before=Schedule-Identity
Save 'schedule-before.json' $before
try {
    Add-Type -Path (Join-Path $PSScriptRoot 'task_origin_session.cs')
    $native=[TaskOriginSession]::Capture()
    Save 'native-environment.json' $native
    $diagnose=@'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from task_origin import session_diagnostic
print(json.dumps(session_diagnostic(json.loads(Path(sys.argv[2]).read_text(encoding='utf-8-sig')))))
'@
    $sessionResult=& $Python -I -c $diagnose $PSScriptRoot (Join-Path $Evidence 'native-environment.json')
    if ($LASTEXITCODE -ne 0) { throw 'Native session classification failed' }
    Save 'session-diagnostic.json' ($sessionResult | ConvertFrom-Json)
    $phase='native-sdk-inventory'
    $schemas=@()
    foreach($provider in @('Microsoft-Windows-TaskScheduler','Microsoft-Windows-Security-Auditing','Microsoft-Windows-Kernel-Process')) {
        try {
            $meta=Get-WinEvent -ListProvider $provider
            $schemas+=@{ name=$meta.Name; guid=$meta.Id.ToString(); events=@($meta.Events | Where-Object {
                $provider -ne 'Microsoft-Windows-Security-Auditing' -or $_.Id -eq 4688
            } | ForEach-Object { @{ id=$_.Id; version=$_.Version; template=$_.Template; opcode=$_.Opcode.Value; task=$_.Task.Value } }) }
        } catch { $errors.Add('provider:'+$provider+':'+$_.Exception.GetType().Name+':'+$_.Exception.HResult) }
    }
    Save 'sdk-provider-templates.json' $schemas
    $channels=@()
    $enabled=[Collections.Generic.List[string]]::new()
    foreach($channel in @('Security','Microsoft-Windows-TaskScheduler/Operational')) {
        try {
            $log=Get-WinEvent -ListLog $channel
            $channels+=@{ name=$channel; enabled=$log.IsEnabled; record_count=$log.RecordCount; log_mode=$log.LogMode.ToString() }
            if($log.IsEnabled) { $enabled.Add($channel) }
        } catch { $errors.Add('channel:'+$channel+':'+$_.Exception.GetType().Name+':'+$_.Exception.HResult) }
    }
    Save 'native-channels.json' $channels
    $references=@((Get-ChildItem "$PSHOME/ref/*.dll").FullName)+@("$PSHOME/System.Diagnostics.EventLog.dll")
    Add-Type -Path (Join-Path $PSScriptRoot 'task_origin_callbacks.cs') -ReferencedAssemblies $references
    $image=Join-Path $PSHOME 'pwsh.exe'
    $commands=@(('"'+$image+'" -NoLogo -NoProfile -Command "Start-Sleep -Milliseconds 750"'),
                 ($image+' -NoLogo -NoProfile -Command "Start-Sleep -Milliseconds 750"'))
    $collector=[TaskOriginCallbacks]::new('\Clover_Gateway', @($image), $commands)
    # Only schema/hashes of foreign Task events; no foreign Task payload values.
    $collector.CaptureForeignTaskSchemas=$true
    $phase='native-sdk-subscription'
    # Subscribe() is the public probe's unchanged two-channel entry point.
    # Here each already-enabled channel is armed independently: disabled channels
    # remain diagnostic gaps, not policy changes or an early no-data refusal.
    foreach($channel in $enabled) {
        try { $collector.SubscribeChannels(@($channel)) }
        catch { $errors.Add('subscribe:'+$channel+':'+$_.Exception.GetType().Name+':'+$_.Exception.HResult) }
    }
    Save 'armed.json' @{ utc_ticks=[DateTime]::UtcNow.Ticks; channels=@($enabled); native_adapter='NONE'; acceptance=$false }
    $phase='diagnostic-child-only'
    # This inert SDK stimulus is NOT a Task action, gateway, root or worker.
    $child=Start-Process -FilePath $image -ArgumentList @('-NoLogo','-NoProfile','-Command','"Start-Sleep -Milliseconds 750"') -PassThru -WindowStyle Hidden
    $childIdentity=@{ pid=$child.Id; creation_filetime=$child.StartTime.ToUniversalTime().ToFileTimeUtc(); image=$image;
        command_line_candidates=$commands; role='INERT_DIAGNOSTIC_CHILD_NOT_TASK'; native_acceptance=$false }
    if(-not $child.WaitForExit(5000)) { throw 'Inert child did not exit; no kill authority granted' }
    $childIdentity.exit_code=$child.ExitCode
    Save 'diagnostic-child.json' $childIdentity
    # Finite eight-second drain; no retries, audit policy changes or update waits.
    $clock=[Diagnostics.Stopwatch]::StartNew()
    while($clock.Elapsed.TotalSeconds -lt 8) { Start-Sleep -Milliseconds 100 }
    $phase='pre-seal-readback'
    $after=Schedule-Identity
    Save 'schedule-reread.json' $after
    if($before.pid -ne $after.pid -or $before.creation_filetime -ne $after.creation_filetime -or $after.status -ne 'Running') {
        throw 'Schedule creation identity changed'
    }
    Save 'native-environment-reread.json' ([TaskOriginSession]::Capture())
    $phase='diagnostic-complete'
} catch {
    $errors.Add($phase+':'+$_.Exception.GetType().Name+':'+$_.Exception.HResult)
} finally {
    if($null -ne $collector) {
        $collector.Dispose()
        Save 'actual-sdk-callbacks.json' @{ records=@($collector.Records); dropped=$collector.Dropped; errors=@($collector.Errors);
            sealed_utc_ticks=[DateTime]::UtcNow.Ticks; native_adapter='NONE'; acceptance=$false }
    }
    Save 'capture.json' @{ status='DIAGNOSTIC_ONLY'; phase=$phase; errors=@($errors); native_acceptance=$false; supported_native_adapters=@();
        public_registration_attempted=$false; task_created=$false; update_attempted=$false; stop_attempted=$false;
        cleanup=@{ task_cleanup_required=$false; account_cleanup_required=$false; profile_cleanup_required=$false;
                   inert_child_exit_observed=($null -ne $childIdentity -and $childIdentity.ContainsKey('exit_code')) };
        next='Review actual WTS/token/logon and SDK evidence. No Task origin or session arrangement is admitted.';
        target_source_sha='f49296084d8e5914998f18726d24905c566f13fb'; target_source_tree='50a2af19f825d782890d9139ad295204ff787cf1';
        target_installed=$false; fixture_sha=$env:GITHUB_SHA; run_id=$env:GITHUB_RUN_ID; run_attempt=$env:GITHUB_RUN_ATTEMPT }
    $hashes=@(Get-ChildItem -LiteralPath $Evidence -File | Sort-Object Name | ForEach-Object {
        @{ name=$_.Name; sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant(); bytes=$_.Length }
    })
    Save 'integrity.json' $hashes
}
# Deliberately nonqualifying even when every diagnostic succeeded.
exit 3
