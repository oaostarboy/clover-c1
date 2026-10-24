"""Portable proof-fixture contracts, not native platform certification."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Contracts(unittest.TestCase):
    def test_exact_released_callers_and_no_duplicate_rows(self):
        p = load('plan')
        pins = json.loads((HERE / 'pins.json').read_text())
        result = p.plan(pins)
        rows = result['matrix']['include']
        self.assertEqual(len(rows), 16)
        self.assertEqual({r['old'] for r in rows if r['caller'] != 'candidate'}, set(p.CALLERS.values()))
        self.assertEqual({r['setup'] for r in rows}, {'troy', 'zyra'})
        self.assertEqual({r['entry'] for r in rows}, {'terminal', 'gateway'})
        self.assertTrue(all(r['turn_mode'] == ('inflight' if r['caller'] == 'candidate' else 'completed') for r in rows))

    def test_changed_frozen_caller_is_not_silently_normalized(self):
        p = load('plan')
        pins = json.loads((HERE / 'pins.json').read_text())
        pins['old_callers']['1.5.6-suni'] = '32d3e6728c71d1f7c8684b7b41ac1785cb8117a9'
        with self.assertRaises(AssertionError):
            p.plan(pins)

    def test_candidate_needs_immutable_tree_and_nonrelease_identity(self):
        p = load('plan')
        pins = json.loads((HERE / 'pins.json').read_text())
        pins.update(mode='candidate', target='a' * 40, target_tree='b' * 40)
        self.assertEqual(p.plan(pins)['mode'], 'candidate')
        pins['target'] = p.CALLERS['1.5.9']
        with self.assertRaises(AssertionError):
            p.plan(pins)

    def test_candidate_next_is_exact_parent_plus_marker_only(self):
        with tempfile.TemporaryDirectory(dir=HERE) as tmp:
            root = Path(tmp)
            repo = root / 'source'
            evidence = root / 'evidence'
            evidence.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / 'retained').write_text('untouched\n')
            git('add', 'retained')
            git('commit', '-qm', 'fixture base')
            base, tree = git('rev-parse', 'HEAD'), git('rev-parse', 'HEAD^{tree}')
            result = load('target').prepare(repo, base, tree, 'candidate', evidence)
            self.assertEqual(git('rev-parse', result['target'] + '^'), base)
            self.assertEqual(git('diff', '--name-only', base, result['target']), '.native-owner-worker-next')
            self.assertEqual(git('status', '--porcelain'), '')
            self.assertEqual(git('replace', '-l'), '')
            self.assertEqual((repo / 'retained').read_text(), 'untouched\n')


if __name__ == '__main__':
    unittest.main(verbosity=2)
