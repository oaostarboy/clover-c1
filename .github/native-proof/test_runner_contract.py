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
