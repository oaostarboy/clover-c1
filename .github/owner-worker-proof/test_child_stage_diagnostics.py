"""Portable execution of the actual child body/catch; NOT native account proof.

Only Windows identity/folder lookups and the external token observer are doubled.
All stage transitions, predicates, receipt I/O, catch and exit are source-derived.
Written for serialized parent execution; no native entry guard is bypassed.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
PWSH = os.environ.get('TOKEN_TEST_PWSH') or shutil.which('pwsh')
PHASES = ('credential_isolation identity token acl_denial profile_userprofile '
          'profile_home profile_known_userprofile profile_appdata profile_localappdata '
          'home_absence home_create home_write home_read startup_location startup_create '
          'startup_write startup_read startup_remove access_receipt').split()


@unittest.skipUnless(PWSH, 'PowerShell required for portable fixture execution')
class ChildStageDiagnostics(unittest.TestCase):
    def run_child_body(self, phase=None, predicate=False, diagnostic_failure=False,
                       fallback_failure=False, real_write_failure=False, boundary_failure=None,
                       startup_case=None):
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            assert PWSH is not None
            root = Path(directory)
            evidence = root / 'evidence'
            evidence.mkdir()
            if diagnostic_failure:
                (evidence / 'child-stage.json').mkdir()
            if fallback_failure:
                (evidence / 'child-failure.txt').mkdir()
            body = (HERE / 'account_child.ps1').read_text()
            body = body[body.index('$exitCode = 1'):]
            # Test-only seams for platform APIs; the actual hosted entry is never run.
            body = body.replace('[Security.Principal.WindowsIdentity]::GetCurrent()',
                                "([pscustomobject]@{User=[pscustomobject]@{Value='owned-sid'}})")
            body = body.replace('([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)', '$false')
            if startup_case:
                body = body.replace("[Environment]::GetFolderPath('Startup', [Environment+SpecialFolderOption]::DoNotVerify)",
                                    '(Get-PortableStartup -DoNotVerify)')
                body = body.replace("[Environment]::GetFolderPath('Startup')", '(Get-PortableStartup)')
            folders = {'UserProfile': '$c.profile', 'ApplicationData': '$env:APPDATA',
                       'LocalApplicationData': '$env:LOCALAPPDATA', 'Startup': "Join-Path $env:APPDATA 'Startup'"}
            for name, value in folders.items():
                body = body.replace(f"[Environment]::GetFolderPath('{name}')", f'({value})')
            if not startup_case:
                body = body.replace("[Environment]::GetFolderPath('Startup', [Environment+SpecialFolderOption]::DoNotVerify)",
                                    "(Join-Path $env:APPDATA 'Startup')")
            if startup_case == 'appdata-mismatch':
                marker = "Set-AccountChildStage -Phase 'startup_location' -State 'entered'"
                body = body.replace(marker, marker + "\n    $env:APPDATA=Join-Path $c.profile 'foreign-appdata'")
            if phase:
                marker = f"Set-AccountChildStage -Phase '{phase}' -State 'entered'"
                self.assertEqual(body.count(marker), 1)
                failure = ('Assert-AccountChildPredicate $false' if predicate else
                           "throw [IO.IOException]::new('sentinel-secret path credential')")
                body = body.replace(marker, marker + '\n    ' + failure)
            if boundary_failure:
                mutations = {
                    'credential_isolation': "$env:OPENAI_API_KEY='sentinel-secret'",
                    'identity': "$c.expected_sid='foreign-sid'",
                    'token': 'function Observe-PortableToken { $global:LASTEXITCODE=1 }',
                    'acl_denial': "$c.protected=Join-Path $c.evidence 'readable'; [IO.File]::WriteAllText($c.protected,'readable')",
                    'profile_userprofile': "$env:USERPROFILE='foreign'",
                    'profile_home': "$env:HOME='foreign'",
                    'profile_known_userprofile': "$c.profile='foreign'",
                    'profile_appdata': "$null=$null",
                    'profile_localappdata': "$null=$null",
                    'home_absence': '$null=New-Item -ItemType Directory -Path $env:CLOVER_HOME',
                    'home_create': '$null=New-Item -ItemType Directory -Path $env:CLOVER_HOME',
                    'home_write': '$owned=$env:CLOVER_HOME',
                    'home_read': "[IO.File]::WriteAllText($owned,'wrong')",
                    'startup_location': '$null=$null',
                    # -Force can silently return $null for file collisions; use an
                    # invalid filesystem path so the real directory creation fails.
                    'startup_create': "$startup=$c.evidence+[IO.Path]::DirectorySeparatorChar+'invalid'+[char]0+'directory'",
                    'startup_write': '$null=New-Item -ItemType Directory -Path $readiness',
                    'startup_read': "[IO.File]::WriteAllText($readiness,'wrong')",
                    'startup_remove': 'Remove-Item -LiteralPath $readiness',
                    'access_receipt': "$null=New-Item -ItemType Directory -Path (Join-Path $c.evidence 'access.json')",
                }
                marker = f"Set-AccountChildStage -Phase '{boundary_failure}' -State 'entered'"
                body = body.replace(marker, marker + '\n    ' + mutations[boundary_failure])
                # Return wrong OS-known folder values at the native lookup seam,
                # rather than replacing the production comparison predicate.
                expression = {'profile_known_userprofile': '($c.profile)',
                              'profile_appdata': '($env:APPDATA)',
                              'profile_localappdata': '($env:LOCALAPPDATA)',
                              'startup_location': "(Join-Path $env:APPDATA 'Startup')"}.get(boundary_failure)
                if expression:
                    body = body.replace(expression, "('foreign-location')")
            if real_write_failure:
                body = body.replace("Set-AccountChildStage -Phase 'home_write' -State 'entered'",
                                    "Set-AccountChildStage -Phase 'home_write' -State 'entered'\n    $owned = $env:CLOVER_HOME")
            prelude = r"""
$ErrorActionPreference='Stop'
. 'COMMON'
Set-StrictMode -Version Latest
$c=[pscustomobject]@{expected_sid='owned-sid';runner_sid='runner-sid';profile=(Join-Path 'ROOT' 'profile');evidence=(Join-Path 'ROOT' 'evidence');protected=(Join-Path 'ROOT' 'protected');python='Observe-PortableToken'}
$env:USERPROFILE=$c.profile; $env:HOME=$c.profile
$env:APPDATA=Join-Path $c.profile 'AppData/Roaming'; $env:LOCALAPPDATA=Join-Path $c.profile 'AppData/Local'
$env:CLOVER_HOME=Join-Path $env:LOCALAPPDATA 'clover'
$null=New-Item -ItemType Directory -Path $env:LOCALAPPDATA,$env:APPDATA,$c.protected -Force
foreach ($name in @('GITHUB_TOKEN','ACTIONS_RUNTIME_TOKEN','ACTIONS_ID_TOKEN_REQUEST_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY')) { [Environment]::SetEnvironmentVariable($name,$null) }
function Observe-PortableToken {
    @{is_user_an_admin=$false} | ConvertTo-Json | Set-Content (Join-Path $c.evidence 'token.json')
    $global:LASTEXITCODE=0
}
""".replace('COMMON', str(HERE / 'account_common.ps1').replace("'", "''")).replace('ROOT', str(root).replace("'", "''"))
            startup_prelude = r"""
$script:startupCase='CASE'
$script:registeredStartup=Join-Path $env:APPDATA 'OS-registered-startup'
switch ($script:startupCase) {
    'existing' { $null=New-Item -ItemType Directory -Path $script:registeredStartup }
    'empty' { $script:registeredStartup='' }
    'foreign' { $script:registeredStartup=Join-Path (Split-Path $c.profile) 'foreign-startup' }
    'relative' { $script:registeredStartup='relative-startup' }
    'traversal' { $script:registeredStartup=Join-Path $env:APPDATA '../escape-startup' }
    'prefix-collision' { $script:registeredStartup=Join-Path ($env:APPDATA+'-foreign') 'new' }
    'reparse' {
        $target=Join-Path (Split-Path $c.profile) 'foreign-target';$null=New-Item -ItemType Directory -Path $target
        $link=Join-Path $env:APPDATA 'redirect';$null=New-Item -ItemType SymbolicLink -Path $link -Target $target
        $script:registeredStartup=Join-Path $link 'new'
    }
    'file-collision' { [IO.File]::WriteAllText($script:registeredStartup,'not a directory') }
}
function Get-PortableStartup {
    param([switch]$DoNotVerify)
    if ($DoNotVerify) { return $script:registeredStartup }
    if ($script:startupCase -eq 'default-disagrees') { return (Join-Path $env:APPDATA 'other-registration') }
    if ($script:startupCase -eq 'readback-empty') { return '' }
    if ($script:startupCase -eq 'readback-disagrees' -and (Test-Path -LiteralPath $script:registeredStartup)) { return (Join-Path $env:APPDATA 'other-registration') }
    if ($script:startupCase -eq 'empty') { return '' }
    if (Test-Path -LiteralPath $script:registeredStartup -PathType Container) { return $script:registeredStartup }
    return ''
}
""".replace('CASE', startup_case or '')
            script = root / 'body.ps1'
            script.write_text(prelude + startup_prelude + body)
            result = subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(script)],
                                    capture_output=True, text=True, timeout=25)
            receipts = {p.name: p.read_text(encoding='utf-8-sig') for p in evidence.iterdir() if p.is_file()}
            if startup_case:
                receipts['portable-startup-state.json'] = json.dumps({
                    'registered_created': (root / 'profile/AppData/Roaming/OS-registered-startup').is_dir(),
                    'foreign_created': any((root / p).exists() for p in (
                        'foreign-startup', 'foreign-target/new', 'profile/AppData/escape-startup',
                        'profile/AppData/Roaming-foreign/new', 'relative-startup')),
                    'readiness_count': len(list(root.rglob('clover-fixture-readiness.txt'))),
                })
            return result, receipts

    def test_missing_startup_uses_os_resolution_then_requires_normal_readback(self):
        result, receipts = self.run_child_body(startup_case='missing')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr + str(receipts))
        access = json.loads(receipts['access.json'])
        self.assertTrue(access['startup'].endswith('OS-registered-startup'))
        self.assertTrue(access['registration_directory_ready'])
        d = json.loads(receipts['child-stage.json'])
        self.assertEqual(d['states'], dict.fromkeys(PHASES, 'passed'))
        self.assertEqual(d['startup'], dict(default_empty=True, resolved_owned=True,
                                          exists=True, readback_agrees=True))

    def test_startup_rejects_unowned_or_unresolved_paths_before_creation(self):
        for case in ('empty', 'foreign', 'relative', 'traversal', 'prefix-collision',
                     'appdata-mismatch', 'reparse', 'file-collision', 'default-disagrees'):
            with self.subTest(startup_case=case):
                result, receipts = self.run_child_body(startup_case=case)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertNotIn('access.json', receipts)
                d = json.loads(receipts['child-stage.json'])
                self.assertEqual((d['phase'], d['state']), ('startup_location', 'failed'))
                self.assertEqual(d['states']['startup_create'], 'unknown')
                state = json.loads(receipts['portable-startup-state.json'])
                self.assertEqual(state, dict(registered_created=False, foreign_created=False, readiness_count=0))

    def test_startup_requires_postcreate_default_readback_and_keeps_existing_folder_control(self):
        for case in ('readback-empty', 'readback-disagrees', 'existing'):
            with self.subTest(startup_case=case):
                result, receipts = self.run_child_body(startup_case=case)
                d = json.loads(receipts['child-stage.json'])
                if case == 'existing':
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('access.json', receipts)
                    self.assertIs(d['startup']['default_empty'], False)
                else:
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertNotIn('access.json', receipts)
                    self.assertEqual(d['phase'], 'startup_create')
                    self.assertIs(d['startup']['exists'], True)
                    self.assertIs(d['startup']['readback_agrees'], False)
                self.assertEqual(json.loads(receipts['portable-startup-state.json'])['readiness_count'], 0)

    def test_every_actual_phase_failure_keeps_exit_one_and_success_receipt_absent(self):
        for phase in PHASES:
            for predicate in (False, True):
                with self.subTest(phase=phase, predicate=predicate):
                    result, receipts = self.run_child_body(phase, predicate)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertNotIn('access.json', receipts)
                    d = json.loads(receipts['child-stage.json'])
                    self.assertEqual((d['phase'], d['state']), (phase, 'failed'))
                    self.assertEqual(d['states'][phase], 'failed')
                    for prior in PHASES[:PHASES.index(phase)]:
                        self.assertEqual(d['states'][prior], 'passed')
                    for later in PHASES[PHASES.index(phase) + 1:]:
                        self.assertEqual(d['states'][later], 'unknown')
                    self.assertEqual(d['reason'], 'predicate_mismatch' if predicate else 'exception')
                    self.assertIs(d['predicate_passed'], False if predicate else None)
                    self.assertEqual(d['exception_category'], 'predicate_mismatch' if predicate else 'io')
                    self.assertIsNone(d['native_code'])
                    self.assertEqual(d['hresult_signed'], None if predicate else -2146232800)
                    self.assertIn('child-failure.txt', receipts)
                    raw = ''.join(v for k, v in receipts.items() if k != 'token.json')
                    self.assertNotIn('sentinel-secret', raw + result.stdout + result.stderr)
                    self.assertNotIn('credential', json.dumps(d).replace('credential_isolation', ''))
                    self.assertEqual(set(d), {'phase', 'state', 'predicate_id', 'predicate_passed',
                                             'reason', 'exception_category', 'hresult_signed',
                                             'native_code', 'states', 'startup'})
                    self.assertEqual(set(d['startup']), {'default_empty', 'resolved_owned', 'exists', 'readback_agrees'})
                    self.assertTrue(all(v is None or type(v) is bool for v in d['startup'].values()))

    def test_real_predicates_and_io_fail_at_the_named_actual_boundaries(self):
        for phase in PHASES:
            with self.subTest(phase=phase):
                result, receipts = self.run_child_body(boundary_failure=phase)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertNotIn('access.json', receipts)
                d = json.loads(receipts['child-stage.json'])
                self.assertEqual((d['phase'], d['state']), (phase, 'failed'))
                self.assertNotIn('sentinel-secret', receipts['child-stage.json'] + result.stdout + result.stderr)

    def test_diagnostic_and_fallback_write_failures_do_not_replace_original_exit(self):
        for phase in PHASES:
            for fallback in (False, True):
                with self.subTest(phase=phase, fallback=fallback):
                    result, receipts = self.run_child_body(phase, diagnostic_failure=True, fallback_failure=fallback)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertNotIn('access.json', receipts)
                    self.assertNotIn('sentinel-secret', result.stdout + result.stderr)

    def test_success_requires_all_original_boundaries_and_access_persistence(self):
        for diagnostic_failure in (False, True):
            result, receipts = self.run_child_body(diagnostic_failure=diagnostic_failure)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(receipts['access.json'])['exit'], 0)
            if not diagnostic_failure:
                d = json.loads(receipts['child-stage.json'])
                self.assertEqual(d['states'], dict.fromkeys(PHASES, 'passed'))
                self.assertEqual((d['phase'], d['state']), ('access_receipt', 'passed'))
        result, receipts = self.run_child_body(real_write_failure=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn('access.json', receipts)
        self.assertEqual(json.loads(receipts['child-stage.json'])['phase'], 'home_write')

    def test_actual_exception_classifier_redacts_and_only_decodes_verified_native_codes(self):
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            assert PWSH is not None
            script = Path(directory) / 'codes.ps1'
            script.write_text("$ErrorActionPreference='Stop'\n. '" + str(HERE / 'account_common.ps1') + "'\n" + r"""
$cases=@(
    [ComponentModel.Win32Exception]::new(5,'sentinel-secret'),
    [UnauthorizedAccessException]::new('sentinel-secret'),
    [IO.IOException]::new('sentinel-secret'),
    [InvalidOperationException]::new('sentinel-secret'),
    [Exception]::new('sentinel-secret'),
    [Reflection.TargetInvocationException]::new([ComponentModel.Win32Exception]::new(87,'sentinel-secret')),
    [Exception]::new('sentinel-secret',[ComponentModel.Win32Exception]::new(123,'sentinel-secret'))
)
@($cases | ForEach-Object { Get-AccountExceptionObservation $_ }) | ConvertTo-Json -Depth 5
""")
            result = subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-File', str(script)],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('sentinel-secret', result.stdout + result.stderr)
            values = json.loads(result.stdout)
            self.assertEqual([v['native_code'] for v in values], [5, 5, None, None, None, 87, None])
            self.assertEqual([v['exception_category'] for v in values],
                             ['win32', 'access_denied', 'io', 'invalid_operation', 'unknown', 'win32', 'unknown'])
            self.assertIsNone(values[-1]['hresult_signed'])  # Do not unwrap arbitrary exceptions.


if __name__ == '__main__':
    unittest.main(verbosity=2)
