# Full driver receipt admission is separate from the unchanged account prerequisite.
Set-StrictMode -Version Latest
function Assert-AccountUpdateResult {
    param($Result, [string]$ExpectedSid, [string]$Profile, $Request)
    if ($Result.passed -isnot [bool] -or $Result.passed -ne $true -or
        $Result.sid -cne $ExpectedSid -or $Result.profile -cne $Profile) {
        throw 'Full Startup result is not bound to the created account'
    }
    foreach ($key in @('source','helpers','uv','setup','old','candidate','target','tree','entry','turn_mode')) {
        if ($Result.request.$key -cne $Request.$key) { throw 'Full Startup request identity changed' }
    }
    $expected = @{ status='PASS'; old=$Request.old; target=$Request.target; entry=$Request.entry;
        turn_mode=$Request.turn_mode; actual_windows_startup=$true; actual_scheduled_task=$false;
        driver_restart=$false; updater_owned_successor=$true; stability_seconds=90;
        real_terminal_tool=$true; inflight_witness=$true; latest_write_state=$true }
    foreach ($entry in $expected.GetEnumerator()) {
        $value = $Result.driver_result.($entry.Key)
        if ($entry.Key -eq 'stability_seconds') {
            # ConvertFrom-Json reads integer 90 as Int64, not the literal's Int32.
            if (($value -isnot [long] -and $value -isnot [int]) -or $value -ne 90) {
                throw 'Full Startup stability window did not complete'
            }
        } elseif ($value.GetType() -ne $entry.Value.GetType() -or $value -cne $entry.Value) {
            throw 'Full Startup acceptance receipt failed'
        }
    }
}
