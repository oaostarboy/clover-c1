"""Portable PowerShell observation tests with scheduler stubs, NOT native proof."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
PWSH = os.environ.get('TOKEN_TEST_PWSH') or shutil.which('pwsh')


@unittest.skipUnless(PWSH, 'PowerShell required for portable observation execution')
class ObservationContracts(unittest.TestCase):
    def observe(self, *, identity_error=False, token=False, state='Running', result=1):
        assert PWSH is not None
        text = (HERE / 'token_prerequisite.ps1').read_text(encoding='utf-8-sig')
        # Execute the actual observation block only, not platform setup or ACLs.
        block = text[text.index('    $until ='):text.index('} finally {')]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if identity_error:
                (root / 'token-error-identity.json').write_text(json.dumps(
                    dict(errno=24, error='GetTokenInformation(size,class=20)')), encoding='utf-8')
            if token:
                (root / 'scheduled-token.json').write_text('{}', encoding='utf-8')
            script = root / 'observe.ps1'
            script.write_text("""param([string]$Evidence)
$ErrorActionPreference = 'Stop'
$taskName = 'Clover_OwnedFixture_Only'
function Get-ScheduledTask {
    param([string]$TaskName)
    if ($TaskName -ne 'Clover_OwnedFixture_Only') { throw 'Wrong task' }
    [pscustomobject]@{ State='STATE' }
}
function Get-ScheduledTaskInfo {
    param([string]$TaskName)
    if ($TaskName -ne 'Clover_OwnedFixture_Only') { throw 'Wrong task' }
    [pscustomobject]@{ LastTaskResult=RESULT }
}
function Start-Sleep { throw 'Unexpected wait before reporting prerequisite failure' }
try {
BLOCK
    'OBSERVED'
} catch {
    Write-Output $_.Exception.Message
    exit 1
}
""".replace('STATE', state).replace('RESULT', str(result)).replace('BLOCK', block), encoding='utf-8')
            completed = subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-File', str(script),
                                        '-Evidence', str(root)], capture_output=True, text=True,
                                       timeout=10, check=False)
            target = root / 'scheduled-task-result.json'
            diagnostic = json.loads(target.read_text(encoding='utf-8-sig')) if target.exists() else None
            return completed, diagnostic

    def test_existing_identity_error_fails_before_sleep_and_records_own_task_result(self):
        for state in ('Running', 'Ready'):
            for token in (False, True):
                with self.subTest(state=state, token=token):
                    completed, diagnostic = self.observe(identity_error=True, state=state, token=token)
                    self.assertEqual(completed.returncode, 1)
                    self.assertIn('GetTokenInformation(size,class=20)', completed.stdout)
                    self.assertIn('24', completed.stdout)
                    self.assertNotIn('Unexpected wait', completed.stdout)
                    assert diagnostic is not None, 'Missing own-task diagnostic'
                    self.assertEqual(diagnostic['task_name'], 'Clover_OwnedFixture_Only')
                    self.assertEqual(diagnostic['last_task_result'], 1)

    def test_success_requires_token_and_zero_task_result(self):
        completed, diagnostic = self.observe(token=True, state='Ready', result=0)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn('OBSERVED', completed.stdout)
        assert diagnostic is not None, 'Missing own-task diagnostic'
        self.assertEqual(diagnostic['last_task_result'], 0)
        completed, _ = self.observe(token=True, state='Ready', result=5)
        self.assertEqual(completed.returncode, 1)
        self.assertIn('Baseline task exited 5', completed.stdout)
        completed, _ = self.observe(token=False, state='Ready', result=0)
        self.assertEqual(completed.returncode, 1)
        self.assertIn('Unexpected wait', completed.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
