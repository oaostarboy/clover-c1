param([Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $IsWindows -or $env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted') {
    throw 'Disposable hosted Windows runner required'
}
$c = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
. (Join-Path $PSScriptRoot 'account_common.ps1')
$exitCode = 1
# Finite product-free counterfactual, never an acceptance path. A successful
# shell-managed Create followed by normal lookup would justify testing that
# provisioning change on a NEW account, not accepting this already failed one.
function Invoke-AccountStartupCounterfactual {
    param([string]$ValidatedStartup, [string]$AppData, [string]$Profile, [string]$ExpectedSid)
    $result = [ordered]@{
        phase='post_failure_counterfactual'; state='not_attempted'
        identity_matches=$null; environment_matches=$null; directory_exists=$null
        mappings=@{}; before_normal=$null; before_do_not_verify=$null
        create=@{attempted=$false; path_empty=$null; equals_validated_os_path=$null; exception_category='none'}
        after_normal=$null; managed_after_empty=$null; managed_after_agrees=$null
    }
    try {
        $result.identity_matches = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -eq $ExpectedSid
        $result.environment_matches = $env:USERPROFILE -eq $Profile -and $env:HOME -eq $Profile -and $env:APPDATA -eq $AppData
        $result.directory_exists = [IO.Directory]::Exists($ValidatedStartup)
        if (-not ($result.identity_matches -and $result.environment_matches -and $result.directory_exists -and
            (Test-AccountOwnedStartupLocation -Startup $ValidatedStartup -AppData $AppData -Profile $Profile))) {
            $result.state = 'precondition_rejected'; return $result
        }
        # Read-only HKCU vs exact-SID hive comparison. Equality of values is NOT
        # proof that HKCU is mapped to that hive. Do not serialize paths or values.
        $subkey = 'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders'
        $currentKey = $null; $sidKey = $null
        try {
            $currentKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($subkey)
            $sidKey = [Microsoft.Win32.Registry]::Users.OpenSubKey("$ExpectedSid\$subkey")
            foreach ($name in @('Startup','Start Menu','Programs','AppData')) {
                $mapping = @{present=$null; kind='unknown'; matches_sid_hive=$null; expanded_owned=$null; expanded_exists=$null; equals_validated_os_path=$null}
                $result.mappings[$name] = $mapping
                if ($null -eq $currentKey -or $null -eq $sidKey) { continue }
                $raw = $currentKey.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                $sidRaw = $sidKey.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                $mapping.present = $null -ne $raw
                if ($null -eq $raw) { continue }
                $kind = $currentKey.GetValueKind($name).ToString()
                $mapping.kind = if ($kind -in @('String','ExpandString')) { $kind } else { 'other' }
                if ($kind -notin @('String','ExpandString') -or $raw -isnot [string]) { continue }
                $mapping.matches_sid_hive = $sidRaw -is [string] -and $kind -eq $sidKey.GetValueKind($name).ToString() -and $raw -ceq $sidRaw
                $expanded = if ($kind -eq 'ExpandString') { [Environment]::ExpandEnvironmentVariables($raw) } else { $raw }
                $mapping.expanded_owned = Test-AccountOwnedStartupLocation -Startup $expanded -AppData $AppData -Profile $Profile
                $mapping.expanded_exists = [IO.Directory]::Exists($expanded)
                $mapping.equals_validated_os_path = $expanded -eq $ValidatedStartup
            }
        } catch { # Missing/unavailable registry observations remain unknown.
        } finally {
            if ($null -ne $currentKey) { $currentKey.Dispose() }
            if ($null -ne $sidKey) { $sidKey.Dispose() }
        }
        $query = @{ValidatedStartup=$ValidatedStartup; RecordedDefaultNormal=$null; AppData=$AppData; Profile=$Profile}
        $result.before_normal = Get-AccountStartupNativeObservation -Flags 0 @query
        $result.before_do_not_verify = Get-AccountStartupNativeObservation -Flags 0x4000 @query
        # One variable changes: replace raw mkdir with the OS Create operation.
        # Require a fresh, exact owned registration and the observed Win32 2
        # symptom immediately before that operation. No repair on ambiguity.
        if ($result.before_normal.reason -ne 'returned' -or $result.before_normal.native_code -ne 2 -or
            $result.before_do_not_verify.reason -ne 'returned' -or $result.before_do_not_verify.hresult_signed -ne 0 -or
            $result.before_do_not_verify.owned_path -ne $true -or $result.before_do_not_verify.equals_validated_os_path -ne $true) {
            $result.state = 'precondition_rejected'; return $result
        }
        $result.create.attempted = $true
        try {
            # SpecialFolderOption.Create is KF_FLAG_CREATE (0x8000); unlike
            # DoNotVerify this asks the shell to provision the known folder.
            $created = [Environment]::GetFolderPath('Startup', [Environment+SpecialFolderOption]::Create)
            $result.create.path_empty = [string]::IsNullOrEmpty($created)
            $result.create.equals_validated_os_path = $created -eq $ValidatedStartup
        } catch {
            $result.create.exception_category = (Get-AccountExceptionObservation $_.Exception).exception_category
        }
        $result.after_normal = Get-AccountStartupNativeObservation -Flags 0 @query
        $managed = [Environment]::GetFolderPath('Startup')
        $result.managed_after_empty = [string]::IsNullOrEmpty($managed)
        $result.managed_after_agrees = $managed -eq $ValidatedStartup
        $result.state = 'observed'
    } catch { $result.state = 'observer_unavailable' }
    return $result
}
$script:childStage = [ordered]@{
    phase='credential_isolation'; state='unknown'; predicate_id='credential_isolation'; predicate_passed=$null
    reason='none'; exception_category='none'; hresult_signed=$null; native_code=$null
    startup=@{default_empty=$null; resolved_owned=$null; exists=$null; readback_agrees=$null; readback_empty=$null}
    runtime=@{ps_version='unknown'; framework_description='unknown'; process_architecture='unknown'; os_architecture='unknown'}
    post_failure_observation=@{phase='not_observed'; normal=$null; do_not_verify=$null}
    states=[ordered]@{
        credential_isolation='unknown'; identity='unknown'; token='unknown'; acl_denial='unknown'
        profile_userprofile='unknown'; profile_home='unknown'; profile_known_userprofile='unknown'
        profile_appdata='unknown'; profile_localappdata='unknown'; home_absence='unknown'
        home_create='unknown'; home_write='unknown'; home_read='unknown'; startup_location='unknown'
        startup_create='unknown'; startup_write='unknown'; startup_read='unknown'; startup_remove='unknown'
        access_receipt='unknown'
    }
}
$startup = $null; $startupDefault = $null
try { $script:childStage.runtime = Get-AccountChildRuntimeObservation } catch { }
try {
    Set-AccountChildStage -Phase 'credential_isolation' -State 'entered'
    foreach ($name in @('GITHUB_TOKEN','ACTIONS_RUNTIME_TOKEN','ACTIONS_ID_TOKEN_REQUEST_TOKEN',
                         'OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY')) {
        Assert-AccountChildPredicate ($null -eq [Environment]::GetEnvironmentVariable($name))
    }
    Set-AccountChildStage -Phase 'credential_isolation' -State 'passed'
    Set-AccountChildStage -Phase 'identity' -State 'entered'
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $admin = ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    Assert-AccountChildPredicate ($identity.User.Value -eq $c.expected_sid -and $identity.User.Value -ne $c.runner_sid -and -not $admin)
    Set-AccountChildStage -Phase 'identity' -State 'passed'
    Set-AccountChildStage -Phase 'token' -State 'entered'
    & $c.python -I (Join-Path $PSScriptRoot 'restricted_token.py') --evidence (Join-Path $c.evidence 'token.json') --expected-sid $c.expected_sid
    Assert-AccountChildPredicate ($LASTEXITCODE -eq 0)
    $token = Get-Content (Join-Path $c.evidence 'token.json') -Raw | ConvertFrom-Json
    Set-AccountChildStage -Phase 'token' -State 'passed'
    Set-AccountChildStage -Phase 'acl_denial' -State 'entered'
    $denied = $false
    try { [IO.File]::ReadAllText($c.protected) | Out-Null }
    catch [UnauthorizedAccessException] { $denied = $true }
    Assert-AccountChildPredicate $denied
    Set-AccountChildStage -Phase 'acl_denial' -State 'passed'
    Set-AccountChildStage -Phase 'profile_userprofile' -State 'entered'
    Assert-AccountChildPredicate ($env:USERPROFILE -eq $c.profile)
    Set-AccountChildStage -Phase 'profile_userprofile' -State 'passed'
    Set-AccountChildStage -Phase 'profile_home' -State 'entered'
    Assert-AccountChildPredicate ($env:HOME -eq $c.profile)
    Set-AccountChildStage -Phase 'profile_home' -State 'passed'
    Set-AccountChildStage -Phase 'profile_known_userprofile' -State 'entered'
    Assert-AccountChildPredicate ([Environment]::GetFolderPath('UserProfile') -eq $c.profile)
    Set-AccountChildStage -Phase 'profile_known_userprofile' -State 'passed'
    Set-AccountChildStage -Phase 'profile_appdata' -State 'entered'
    Assert-AccountChildPredicate ([Environment]::GetFolderPath('ApplicationData') -eq $env:APPDATA)
    Set-AccountChildStage -Phase 'profile_appdata' -State 'passed'
    Set-AccountChildStage -Phase 'profile_localappdata' -State 'entered'
    Assert-AccountChildPredicate ([Environment]::GetFolderPath('LocalApplicationData') -eq $env:LOCALAPPDATA)
    Set-AccountChildStage -Phase 'profile_localappdata' -State 'passed'
    Set-AccountChildStage -Phase 'home_absence' -State 'entered'
    Assert-AccountChildPredicate (-not (Test-Path -LiteralPath $env:CLOVER_HOME))
    Set-AccountChildStage -Phase 'home_absence' -State 'passed'
    Set-AccountChildStage -Phase 'home_create' -State 'entered'
    $null = New-Item -ItemType Directory -Path $env:CLOVER_HOME
    $owned = Join-Path $env:CLOVER_HOME 'owned-write.txt'
    Set-AccountChildStage -Phase 'home_create' -State 'passed'
    Set-AccountChildStage -Phase 'home_write' -State 'entered'
    [IO.File]::WriteAllText($owned, 'Private non-admin home write control')
    Set-AccountChildStage -Phase 'home_write' -State 'passed'
    Set-AccountChildStage -Phase 'home_read' -State 'entered'
    Assert-AccountChildPredicate ([IO.File]::ReadAllText($owned) -eq 'Private non-admin home write control')
    Set-AccountChildStage -Phase 'home_read' -State 'passed'
    # Registration readiness only: no gateway, installer, update or task is run.
    Set-AccountChildStage -Phase 'startup_location' -State 'entered'
    $startupDefault = [Environment]::GetFolderPath('Startup')
    $script:childStage.startup.default_empty = [string]::IsNullOrEmpty($startupDefault)
    $startup = [Environment]::GetFolderPath('Startup', [Environment+SpecialFolderOption]::DoNotVerify)
    # Earlier stages bind this identity/profile to the exact created SID. Verify
    # the OS location under that profile's APPDATA before any Startup creation.
    $script:childStage.startup.resolved_owned = Test-AccountOwnedStartupLocation -Startup $startup -AppData $env:APPDATA -Profile $c.profile
    Assert-AccountChildPredicate ($script:childStage.startup.resolved_owned -and
        ($script:childStage.startup.default_empty -or $startupDefault -eq $startup))
    Set-AccountChildStage -Phase 'startup_location' -State 'passed'
    Set-AccountChildStage -Phase 'startup_create' -State 'entered'
    # A new profile needs shell-managed known-folder provisioning, not raw mkdir.
    # Normal lookup and real write/read/remove below remain mandatory.
    $startupCreated = [Environment]::GetFolderPath('Startup', [Environment+SpecialFolderOption]::Create)
    Assert-AccountChildPredicate ($startupCreated -eq $startup)
    $script:childStage.startup.exists = Test-Path -LiteralPath $startup -PathType Container
    $startupReadback = [Environment]::GetFolderPath('Startup')
    $script:childStage.startup.readback_agrees = $startupReadback -eq $startup
    $script:childStage.startup.readback_empty = [string]::IsNullOrEmpty($startupReadback)
    Assert-AccountChildPredicate ($script:childStage.startup.exists -and $script:childStage.startup.readback_agrees -and
        (Test-AccountOwnedStartupLocation -Startup $startup -AppData $env:APPDATA -Profile $c.profile))
    $readiness = Join-Path $startup 'clover-fixture-readiness.txt'
    Set-AccountChildStage -Phase 'startup_create' -State 'passed'
    Set-AccountChildStage -Phase 'startup_write' -State 'entered'
    [IO.File]::WriteAllText($readiness, 'Disposable registration directory readiness, not a gateway')
    Set-AccountChildStage -Phase 'startup_write' -State 'passed'
    Set-AccountChildStage -Phase 'startup_read' -State 'entered'
    Assert-AccountChildPredicate ([IO.File]::ReadAllText($readiness) -eq 'Disposable registration directory readiness, not a gateway')
    Set-AccountChildStage -Phase 'startup_read' -State 'passed'
    Set-AccountChildStage -Phase 'startup_remove' -State 'entered'
    Remove-Item -LiteralPath $readiness
    Set-AccountChildStage -Phase 'startup_remove' -State 'passed'
    Set-AccountChildStage -Phase 'access_receipt' -State 'entered'
    @{ exit=0; sid=$identity.User.Value; admin=$admin; is_user_an_admin=$token.is_user_an_admin;
       admin_only_read_denied=$denied; private_home_write=$true; registration_directory_ready=$true;
       home=$env:HOME; userprofile=$env:USERPROFILE; appdata=$env:APPDATA; localappdata=$env:LOCALAPPDATA;
       clover_home=$env:CLOVER_HOME; startup=$startup; pid=$PID } |
        ConvertTo-Json | Set-Content (Join-Path $c.evidence 'access.json') -Encoding utf8
    Set-AccountChildStage -Phase 'access_receipt' -State 'passed'

    if ($c.PSObject.Properties.Name -contains 'update') {
        # Synchronous lifetime: installer, gateway, public update and stability
        # checks finish under this account before outer SID-bound disposal.
        & $c.python -I (Join-Path $PSScriptRoot 'account_update.py') 'run' --config $Config
        if ($LASTEXITCODE -ne 0) { throw 'Full Startup update driver failed' }
    }
    $exitCode = 0
} catch {
    $originalFailure = $_.Exception
    $exitCode = 1
    if ($script:childStage.phase -eq 'access_receipt' -and $script:childStage.state -eq 'passed') {
        # Never relabel successful access as a failed access stage.
        try { [IO.File]::WriteAllText((Join-Path $c.evidence 'update-failure.txt'), 'Full Startup update driver failed after verified access') } catch { }
    } else {
    try { Set-AccountChildStage -Phase $script:childStage.phase -State 'failed' -Exception $originalFailure } catch { }
    # Only after the ORIGINAL Startup predicate failed and exit=1 was fixed.
    # Later native returns are observations, never replacement acceptance inputs.
    try {
        if ($script:childStage.phase -in @('startup_location','startup_create') -and
            $script:childStage.reason -eq 'predicate_mismatch') {
            $validatedStartup = if ($script:childStage.startup.resolved_owned -eq $true) { $startup } else { $null }
            $script:childStage.post_failure_observation.phase = 'post_failure_observation'
            $script:childStage.post_failure_observation.normal = Get-AccountStartupNativeObservation -Flags 0 -ValidatedStartup $validatedStartup -RecordedDefaultNormal $startupDefault -AppData $env:APPDATA -Profile $c.profile
            $script:childStage.post_failure_observation.do_not_verify = Get-AccountStartupNativeObservation -Flags 0x4000 -ValidatedStartup $validatedStartup -RecordedDefaultNormal $startupDefault -AppData $env:APPDATA -Profile $c.profile
            $script:childStage | ConvertTo-Json -Depth 4 |
                Set-Content -LiteralPath (Join-Path $c.evidence 'child-stage.json') -Encoding utf8 -ErrorAction Stop
        }
    } catch { }
    # Isolated receipt: never overwrite the original stage or reset its exit.
    # One counterfactual only, on this precise symptom after real raw creation.
    try {
        if ($script:childStage.phase -eq 'startup_create' -and $script:childStage.reason -eq 'predicate_mismatch' -and
            $script:childStage.startup.resolved_owned -eq $true -and $script:childStage.startup.exists -eq $true -and
            $script:childStage.startup.readback_empty -eq $true) {
            Invoke-AccountStartupCounterfactual -ValidatedStartup $startup -AppData $env:APPDATA -Profile $c.profile -ExpectedSid $c.expected_sid |
                ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $c.evidence 'startup-counterfactual.json') -Encoding utf8 -ErrorAction Stop
        }
    } catch { }
    # Fixed generic fallback survives even diagnostic persistence/classifier failure.
    try {
        [IO.File]::WriteAllText((Join-Path $c.evidence 'child-failure.txt'), 'Actual account child failed; inspect phase-specific nonsecret receipts')
    } catch { }
    }
}
exit $exitCode
