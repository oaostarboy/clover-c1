"""Portable verifier checks; these do not constitute native Windows proof."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location('external_' + name, HERE / (name + '.py'))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VerifierTests(unittest.TestCase):
    def test_literal_pins(self):
        probe = load('probe')
        self.assertEqual(probe.sha('a' * 40), 'a' * 40)
        for value in ('a' * 39, 'a' * 41, 'A' * 40, ' a' + 'a' * 39, 'main'):
            with self.assertRaises(ValueError):
                probe.sha(value)

    def test_missing_pending_state_is_red(self):
        probe = load('probe')
        with self.assertRaisesRegex(AssertionError, 'pending'):
            probe.ensure_state({'integrity': [['ok']], 'user_labels': ['new'], 'pending': []}, ['new'])
        with self.assertRaisesRegex(AssertionError, 'integrity'):
            probe.ensure_state({'integrity': [['bad']], 'user_labels': ['new'], 'pending': ['new']}, ['new'])

    def test_child_state_roundtrip_real_store(self):
        with tempfile.TemporaryDirectory(prefix='external-verifier-') as tmp:
            env = dict(os.environ, CLOVER_HOME=tmp, PYTHONPATH=str(REPO))
            env.pop('PYTHONHOME', None)
            for label in ('before-update', 'new-state-before-rollback'):
                result = subprocess.run([sys.executable, str(HERE / 'child.py'), 'seed',
                                         '--output', str(Path(tmp) / 'seed.json'), '--label', label],
                                        cwd=tmp, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads((Path(tmp) / 'seed.json').read_text())
            load('probe').ensure_state(state, ['before-update', 'new-state-before-rollback'])
            # RED control: a latest snapshot that completes the original task
            # must not inherit a stale historical pending status.
            import sqlite3
            conn = sqlite3.connect(Path(tmp) / 'state.db')
            try:
                row_id, raw = conn.execute("SELECT id,content FROM messages WHERE role='tool' ORDER BY id DESC LIMIT 1").fetchone()
                snap = json.loads(raw)
                for item in snap['todos']:
                    if item['id'] == 'before-update':
                        item['status'] = 'completed'
                conn.execute('UPDATE messages SET content=? WHERE id=?', (json.dumps(snap), row_id))
                conn.commit()
            finally:
                conn.close()
            result = subprocess.run([sys.executable, str(HERE / 'child.py'), 'read',
                                     '--output', str(Path(tmp) / 'read.json')],
                                    cwd=tmp, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads((Path(tmp) / 'read.json').read_text())
            self.assertNotIn('before-update', observed['pending'])
            self.assertIn('new-state-before-rollback', observed['pending'])
            with self.assertRaisesRegex(AssertionError, 'pending'):
                load('probe').ensure_state(observed, ['before-update'])

    def test_real_source_hashes_and_actual_host_identity(self):
        probe = load('probe')
        with tempfile.TemporaryDirectory(prefix='external-source-verifier-') as tmp:
            base = Path(tmp)
            env = dict(os.environ, CLOVER_HOME=tmp, PYTHONPATH=str(REPO))
            env.pop('PYTHONHOME', None)
            commands = probe.Commands(base, env, base)
            pin = subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], text=True).strip()
            probe.source_identity(REPO, pin, base, 'portable-source', commands)
            actual = base / 'identity.json'
            result = subprocess.run([sys.executable, str(HERE / 'child.py'), 'identity',
                                     '--output', str(actual)], cwd=tmp, env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            identity = json.loads(actual.read_text())
            self.assertEqual(identity['os_name'], os.name)
            self.assertEqual(Path(identity['executable']), Path(sys.executable))

    @unittest.skipIf(os.name == 'nt', 'This verifier control targets the current non-Windows host')
    def test_non_native_cannot_report_pass(self):
        with tempfile.TemporaryDirectory(prefix='external-nonnative-') as tmp:
            result = subprocess.run([sys.executable, str(HERE / 'probe.py'), '--source', str(REPO),
                                     '--target-sha', 'a' * 40, '--target-tree', 'b' * 40,
                                     '--old-sha', 'c' * 40, '--case', 'candidate-repair',
                                     '--evidence', tmp], capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            receipt = json.loads((Path(tmp) / 'result.json').read_text())
            self.assertEqual(receipt['status'], 'FAIL')
            self.assertIn('ONLY on native Windows', receipt['error'])
            xml = ET.parse(Path(tmp) / 'native-results.xml').getroot()
            self.assertEqual(len(xml.findall('testcase/failure')), 1)
            self.assertEqual(len(xml.findall('testcase')), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
