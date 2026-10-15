"""Exercise native proof argv through the real canonical subprocess runner."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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


if __name__ == '__main__':
    unittest.main()
