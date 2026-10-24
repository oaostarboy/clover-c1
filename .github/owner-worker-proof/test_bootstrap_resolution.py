"""Bounded setup-uv path contract; never search PATH or PATHEXT."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('proof_bootstrap_resolution', HERE / 'bootstrap.py')
assert spec is not None and spec.loader is not None
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def resolve(self, value, windows=True):
        with patch.object(bootstrap, 'WINDOWS', windows, create=True):
            return bootstrap.resolve(str(value))

    def test_windows_extensionless_setup_output_resolves_exact_adjacent_exe(self):
        exe = self.root / 'uv.exe'
        exe.write_bytes(b'fixture executable identity')
        self.assertEqual(self.resolve(self.root / 'uv'), str(exe))

    def test_explicit_exe_remains_exact(self):
        exe = self.root / 'uv.exe'
        exe.write_bytes(b'fixture')
        self.assertEqual(self.resolve(exe), str(exe))

    def test_existing_extensionless_file_keeps_precedence(self):
        tool = self.root / 'uv'
        tool.write_bytes(b'original')
        (self.root / 'uv.exe').write_bytes(b'other')
        self.assertEqual(self.resolve(tool), str(tool))

    def test_posix_does_not_append_exe(self):
        (self.root / 'uv.exe').write_bytes(b'fixture')
        with self.assertRaises(FileNotFoundError):
            self.resolve(self.root / 'uv', windows=False)

    def test_other_names_do_not_gain_exe_fallback(self):
        (self.root / 'other.exe').write_bytes(b'fixture')
        with self.assertRaises(FileNotFoundError):
            self.resolve(self.root / 'other')

    def test_cmd_is_not_a_fallback(self):
        (self.root / 'uv.cmd').write_bytes(b'fixture')
        with self.assertRaises(FileNotFoundError):
            self.resolve(self.root / 'uv')

    def test_bat_is_not_a_fallback(self):
        (self.root / 'uv.bat').write_bytes(b'fixture')
        with self.assertRaises(FileNotFoundError):
            self.resolve(self.root / 'uv')

    def test_directory_is_not_executable(self):
        (self.root / 'uv').mkdir()
        with self.assertRaises(ValueError):
            self.resolve(self.root / 'uv')

    def test_exe_directory_is_not_executable(self):
        (self.root / 'uv.exe').mkdir()
        with self.assertRaises(ValueError):
            self.resolve(self.root / 'uv')

    def test_empty_and_relative_paths_rejected(self):
        for value in ('', 'uv', 'uv.exe', 'relative/uv'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.resolve(value)

    def test_no_neighbor_directory_search(self):
        neighbor = self.root / 'neighbor'
        neighbor.mkdir()
        (neighbor / 'uv.exe').write_bytes(b'fixture')
        with self.assertRaises(FileNotFoundError):
            self.resolve(self.root / 'uv')


if __name__ == '__main__':
    unittest.main(verbosity=2)
