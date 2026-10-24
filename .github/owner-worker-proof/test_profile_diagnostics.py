"""Portable diagnostics only: never Windows account/logon/profile/ACL proof."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class ProfileDiagnostics(unittest.TestCase):
    def powershell(self):
        executable = shutil.which('pwsh')
        if not executable:
            self.skipTest('Portable diagnostic execution requires PowerShell 7')
        return executable

    def test_actual_profile_boundary_and_cleanup_with_portable_native_double(self):
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            result = subprocess.run(
                [self.powershell(), '-NoLogo', '-NoProfile', '-NonInteractive',
                 '-File', str(HERE / 'test_profile_diagnostics.ps1'),
                 '-EvidenceRoot', directory],
                capture_output=True, text=True, timeout=45,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads((Path(directory) / 'PORTABLE-RESULT.json').read_text())
            self.assertEqual(receipt['scope'], 'portable diagnostics only; NO Windows proof')
            self.assertTrue(receipt['passed'])
            self.assertEqual(receipt['hresult_cases'], 8)
            self.assertEqual(receipt['binding_negative'], 1)
            self.assertEqual(receipt['cleanup_cases'], 10)
            self.assertTrue(receipt['parser_and_native_declaration_compile'])
            for artifact in Path(directory).rglob('*.json'):
                raw = artifact.read_text()
                self.assertNotIn('portable-synthetic-secret-never-record', raw)
                self.assertNotIn('::add-mask::', raw)

    @unittest.skipIf(sys.platform == 'win32', 'Portable non-Windows refusal control')
    def test_full_entry_still_refuses_non_windows_before_account_or_evidence_creation(self):
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            evidence = Path(directory) / 'must-not-exist'
            result = subprocess.run(
                [self.powershell(), '-NoLogo', '-NoProfile', '-NonInteractive',
                 '-File', str(HERE / 'disposable_account.ps1'),
                 '-Python', sys.executable, '-Evidence', str(evidence)],
                capture_output=True, text=True, timeout=15,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Disposable hosted Windows runner required', result.stderr)
            self.assertFalse(evidence.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
