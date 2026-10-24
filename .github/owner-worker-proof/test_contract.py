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
        pins['scope'] = 'full'
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
        pins.update(mode='candidate', scope='full', target='a' * 40, target_tree='b' * 40)
        self.assertEqual(p.plan(pins)['mode'], 'candidate')
        pins['target'] = p.CALLERS['1.5.9']
        with self.assertRaises(AssertionError):
            p.plan(pins)

    def test_native_safety_only_can_never_claim_full_update_acceptance(self):
        p = load('plan')
        pins = json.loads((HERE / 'pins.json').read_text())
        pins.update(mode='native-safety', scope='full')
        self.assertFalse(p.plan(pins)['full_matrix'])
        pins['mode'] = 'candidate'
        self.assertTrue(p.plan(pins)['full_matrix'])

    def test_task_correction_is_explicit_partial_and_cannot_clear_future_source(self):
        p = load('plan')
        pins = json.loads((HERE / 'pins.json').read_text())
        pins.update(mode='candidate', scope='troy-verifier-correction',
                    target='bdad7a7b6b6698f347e06fd305a2bc6c3dc9055e',
                    target_tree='04630f4788743b5b556e5e4379d998c42394efa3')
        result = p.plan(pins)
        self.assertFalse(result['full_matrix'])
        self.assertEqual(len(result['matrix']['include']), 8)
        self.assertEqual({r['setup'] for r in result['matrix']['include']}, {'troy'})
        pins['target'] = 'a' * 40
        with self.assertRaises(AssertionError):
            p.plan(pins)

    def test_native_topology_accepts_measured_base_argv_not_only_venv_prefix(self):
        t = load('topology')
        installed = r'C:\fixture\venv\Scripts\python.exe'
        base = r'C:\fixture\python\python.exe'
        rows = [dict(pid=10, ppid=1, exe=installed, argv=[installed]),
                dict(pid=11, ppid=10, exe=base, argv=[base])]
        self.assertEqual([r['pid'] for r in t.pair(rows, installed, base)], [10, 11])
        rows[1]['exe'] = r'C:\foreign\python.exe'
        with self.assertRaises(AssertionError):
            t.pair(rows, installed, base)
        rows[1]['exe'] = base
        rows.append(dict(pid=12, ppid=10, exe=base, argv=[base]))
        with self.assertRaises(AssertionError):
            t.pair(rows, installed, base)

    def test_native_topology_does_not_admit_a_second_installed_launcher(self):
        t = load('topology')
        installed = r'C:\fixture\venv\Scripts\python.exe'
        base = r'C:\fixture\python\python.exe'
        with self.assertRaises(AssertionError):
            t.pair([dict(pid=10, ppid=1, exe=installed),
                    dict(pid=12, ppid=2, exe=installed)], installed, base)

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
            result = load('target').prepare(repo, base, tree, 'candidate', evidence, required_ancestor=base)
            with self.assertRaises(subprocess.CalledProcessError):
                load('target').prepare(repo, base, tree, '1.5.9', evidence, required_ancestor=result['target'])
            self.assertEqual(git('rev-parse', result['target'] + '^'), base)
            self.assertEqual(git('diff', '--name-only', base, result['target']), '.native-owner-worker-next')
            self.assertEqual(git('status', '--porcelain'), '')
            self.assertEqual(git('replace', '-l'), '')
            self.assertEqual((repo / 'retained').read_text(), 'untouched\n')


if __name__ == '__main__':
    unittest.main(verbosity=2)
