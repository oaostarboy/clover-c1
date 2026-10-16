"""Exercise native proof argv through the real canonical subprocess runner."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).with_name('native_tests.py')


class NativeRunnerContract(unittest.TestCase):
    def test_sqlite_suite_produces_nonempty_passing_junit_via_real_runner(self):
        spec = importlib.util.spec_from_file_location('native_tests_runner_contract', SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='native proof ') as d:
            # Spaces and shell punctuation must remain one literal argv value.
            xml = Path(d) / 'native result ; not-a-command.xml'
            file, expression = module.SUITES['sqlite']
            command = module.suite_command(ROOT, file, expression, xml)
            env = {**os.environ, 'CLOVER_PYTHON': sys.executable,
                   'CLOVER_TEST_FILE_RETRIES': '0'}
            proc = subprocess.run(command, cwd=ROOT, env=env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
            self.assertEqual(proc.returncode, 0, f'{command!r}\n{proc.stdout}')
            self.assertGreater(module.validate_junit(xml), 0)


    def test_report_only_keeps_bounded_hang_diagnostics_without_retry_or_timeout_change(self):
        spec = importlib.util.spec_from_file_location('native_tests_report_diagnostics', SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.object(module, 'canonical_shell', return_value='bash'):
            command = module.suite_command(Path('/inert-proof-source'),
                'tests/gateway/test_workstream_acceptance.py', '', Path('/inert-evidence/report.xml'))
        self.assertEqual(command[-3:], ['-vv', '-o', 'faulthandler_timeout=30'])
        self.assertEqual(command[command.index('--file-retries') + 1], '0')
        self.assertNotIn('--timeout', command)


    def test_windows_ucrt64_discovery_uses_the_same_git_installation_root(self):
        """Portable path-walking contract; actual Windows execution is separate."""
        spec = importlib.util.spec_from_file_location('native_tests_ucrt64_shell', SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='native UCRT64 discovery ') as d:
            root = Path(d) / 'Program Files/Git'
            git = root / 'ucrt64/bin/git.exe'
            git.parent.mkdir(parents=True)
            git.write_text('inert Git discovery fixture', encoding='utf-8')
            bash = root / 'bin/bash.exe'
            bash.parent.mkdir(parents=True)
            bash.write_text('inert shell path fixture; never executed', encoding='utf-8')
            with mock.patch.object(module, 'os', types.SimpleNamespace(name='nt')):
                setattr(module, 'shutil', types.SimpleNamespace(which=lambda name: str(git) if name == 'git' else None))
                self.assertEqual(module.canonical_shell(), str(bash))
                bash.unlink()
                with self.assertRaises(FileNotFoundError):
                    module.canonical_shell()


    @unittest.skipIf(os.name == 'nt', 'Portable Bash argv fixture uses a POSIX symlink, not native Windows')
    def test_windows_uses_git_bash_not_path_wsl_and_preserves_real_runner_argv(self):
        spec = importlib.util.spec_from_file_location('native_tests_windows_shell', SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='native Git Bash ; ') as d:
            root = Path(d)
            git = root / 'Program Files/Git/cmd/git.exe'
            git.parent.mkdir(parents=True)
            git.write_text('fixture Git discovery only', encoding='utf-8')
            bash = root / 'Program Files/Git/bin/bash.exe'
            bash.parent.mkdir(parents=True)
            bash.symlink_to('/bin/bash')
            xml = root / 'literal result ; no-shell.xml'
            with mock.patch.object(module, 'os', types.SimpleNamespace(name='nt')):
                setattr(module, 'shutil', types.SimpleNamespace(which=lambda name: str(git) if name == 'git' else None))
                command = module.suite_command(ROOT, *module.SUITES['sqlite'], xml)
            self.assertEqual(command[0], str(bash), 'bare bash selects Windows WSL, not the known Git Bash')
            env = {**os.environ, 'CLOVER_PYTHON': sys.executable, 'CLOVER_TEST_FILE_RETRIES': '0'}
            proc = subprocess.run(command, cwd=ROOT, env=env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertGreater(module.validate_junit(xml), 0)
            bash.unlink()
            with mock.patch.object(module, 'os', types.SimpleNamespace(name='nt')):
                with self.assertRaises(FileNotFoundError):
                    module.suite_command(ROOT, *module.SUITES['sqlite'], xml)
                setattr(module, 'shutil', types.SimpleNamespace(which=lambda name: None))
                with self.assertRaises(FileNotFoundError):
                    module.suite_command(ROOT, *module.SUITES['sqlite'], xml)


if __name__ == '__main__':
    unittest.main()
