"""Offline contracts for the exact-target native verifier (no product guards bypassed)."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).with_name('proof.py')


class ExactTarget(unittest.TestCase):
    def test_no_alias_or_replace_and_exact_marker_child(self):
        self.assertTrue(SCRIPT.is_file(), 'exact-target proof support is missing')
        spec = importlib.util.spec_from_file_location('native_proof', SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / 'source'
            repo.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
            git('init', '-q')
            git('remote', 'add', 'origin', 'https://github.com/oaostarboy/clover-c1.git')
            git('config', 'user.name', 'native verifier fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / 'file').write_text('old')
            git('add', '.')
            git('commit', '-qm', 'old')
            old = git('rev-parse', 'HEAD')
            git('tag', 'v1.5.5')
            (repo / 'file').write_text('candidate')
            git('commit', '-qam', 'candidate')
            target = git('rev-parse', 'HEAD')
            tree = git('rev-parse', 'HEAD^{tree}')
            marker = repo / '.github/win-e2e/NEXT_MARKER.txt'
            marker.parent.mkdir(parents=True)
            marker.write_text('fixture next')
            git('add', '.')
            git('commit', '-qm', 'next marker')
            next_sha = git('rev-parse', 'HEAD')
            git('branch', 'ci/native-next/fixture')
            plan = module.plan(repo, 'v1.5.5', target, tree, 'ci/native-next/fixture')
            self.assertEqual(plan['old_sha'], old)
            self.assertEqual(plan['next_sha'], next_sha)
            # Both supported release-tag shapes resolve through the same exact pinned ancestry.
            git('tag', 'v1.5.9.1', old)
            for old_ref in ['v1.5.5', 'v1.5.9.1']:
                with self.subTest(old_ref=old_ref):
                    self.assertEqual(module.plan(repo, old_ref, target, tree,
                                                 'ci/native-next/fixture')['old_sha'], old)
            for old_ref in ['xv1.5.9.1', 'v1.5.9.1-suffix', 'v1.5.9.1/extra',
                            'v1.5.9.1' + chr(10), 'v1.5.9.1.0']:
                with self.subTest(old_ref=old_ref), self.assertRaises(ValueError):
                    module.plan(repo, old_ref, target, tree, 'ci/native-next/fixture')
            with self.assertRaises(ValueError):
                module.plan(repo, 'v1.5.5', target, '0' * 40, 'ci/native-next/fixture')
            with self.assertRaises(ValueError):
                module.plan(repo, 'v1.5.5', target[:7], tree, 'ci/native-next/fixture')
            mirror = Path(d) / 'origin.git'
            module.fixture_origin(repo, mirror, target)
            self.assertEqual(git('ls-remote', 'origin', 'refs/heads/main').split()[0], target)
            self.assertEqual(git('rev-parse', 'HEAD'), next_sha, 'origin setup must not alias installed HEAD')
            self.assertEqual(git('replace', '-l'), '')
            self.assertEqual(subprocess.check_output(['git', '--git-dir', str(mirror), 'rev-parse', 'main'], text=True).strip(), target)
            # A ref that moves or changes product content is not a next marker.
            (repo / 'file').write_text('wrong')
            git('commit', '-qam', 'unexpected product change')
            git('branch', '-f', 'ci/native-next/fixture')
            with self.assertRaises(ValueError):
                module.plan(repo, 'v1.5.5', target, tree, 'ci/native-next/fixture')


    def test_receipt_rejects_stale_wrong_source_and_provisional_success(self):
        import json
        from datetime import datetime, timedelta, timezone
        spec = importlib.util.spec_from_file_location('native_proof', SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(hasattr(module, 'check_receipt'), 'current-run receipt validation is missing')
        now = datetime.now(timezone.utc)
        start = (now - timedelta(seconds=2)).isoformat()
        end = (now - timedelta(seconds=1)).isoformat()
        receipt = dict(started_at=start, finished_at=end, outcome='success',
                       pre_update={'sha': 'a' * 40}, post_update={'sha': 'b' * 40})
        module.check_receipt(receipt, now.timestamp()-3, 'a'*40, 'b'*40)
        for change in [dict(outcome='running'), dict(finished_at=None),
                       dict(started_at=(now-timedelta(days=1)).isoformat()),
                       dict(post_update={'sha': 'a'*40}),
                       dict(pre_update={'sha': 'c'*40})]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.check_receipt({**receipt, **change}, now.timestamp()-3, 'a'*40, 'b'*40)


    def test_gateway_identity_and_manual_workflow_contract(self):
        spec = importlib.util.spec_from_file_location('native_proof', SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(hasattr(module, 'check_gateway'), 'live gateway identity checks are missing')
        state = dict(pid=42, code_sha='b'*40, start_time='ticks', gateway_state='running', restart_requested=False)
        module.check_gateway(state, 'b'*40, [42], [42])
        for change, roots, listeners in [({'code_sha':'a'*40},[42],[42]),
                ({'restart_requested':True},[42],[42]), ({'gateway_state':'stopped'},[42],[42]),
                ({},[42,43],[42]), ({},[42],[43]), ({'start_time':None},[42],[42])]:
            with self.subTest(change=change, roots=roots, listeners=listeners), self.assertRaises(ValueError):
                module.check_gateway({**state, **change}, 'b'*40, roots, listeners)
        workflow = ROOT / '.github/workflows/native-exact-proof.yml'
        self.assertTrue(workflow.exists(), 'manual exact-target workflow is missing')
        import yaml
        data = yaml.safe_load(workflow.read_text())
        triggers = data.get('on', data.get(True))
        self.assertEqual(set(triggers), {'workflow_dispatch', 'workflow_call'})
        for mode in triggers.values():
            self.assertEqual(mode['inputs']['old_ref']['default'], 'v1.5.5')
            for name in ['target_ref','target_tree','next_ref']:
                self.assertTrue(mode['inputs'][name]['required'])
        self.assertEqual(data['permissions'], {'contents':'read'})
        self.assertEqual(data['jobs']['windows']['strategy']['matrix'],
                         {'setup':['troy','zyra'], 'path':['cli','chat'], 'phase':['old2new','new2next']})
        self.assertNotIn('secrets', data)


    def test_native_suite_rejects_empty_skipped_and_failed_junit(self):
        script = SCRIPT.with_name('native_tests.py')
        self.assertTrue(script.exists(), 'native SQLite/report-only/Stop test routing is missing')
        spec = importlib.util.spec_from_file_location('native_tests', script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'result.xml'
            path.write_text('<testsuite><testcase name="real"/></testsuite>')
            self.assertEqual(module.validate_junit(path), 1)
            for xml in ['<testsuite/>', '<testsuite><testcase><skipped/></testcase></testsuite>',
                        '<testsuite><testcase><failure/></testcase></testsuite>',
                        '<testsuite><testcase><error/></testcase></testsuite>']:
                path.write_text(xml)
                with self.subTest(xml=xml), self.assertRaises(ValueError):
                    module.validate_junit(path)


    def test_evidence_preserves_failed_outcome_without_copying_credentials(self):
        import json
        script = SCRIPT.with_name('row.py')
        self.assertTrue(script.exists(), 'row evidence staging is missing')
        spec = importlib.util.spec_from_file_location('native_row', script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / 'home'; home.mkdir()
            (home / '.env').write_text('SYNTHETIC_SECRET=must-not-copy')
            (home / 'gateway_state.json').write_text('{"gateway_state":"stopped"}')
            (home / 'logs').mkdir()
            (home / 'logs/gateway.log').write_text('actual failed fixture log')
            dst = Path(d) / 'evidence'
            module.stage(home, dst, {'status':'failure', 'run_id':'fixture'})
            self.assertEqual(json.loads((dst/'row.json').read_text())['status'], 'failure')
            self.assertTrue((dst/'logs/gateway.log').exists())
            self.assertFalse((dst/'.env').exists())


    def test_native_dependencies_use_managed_uv_not_unseeded_pip(self):
        spec = importlib.util.spec_from_file_location('native_tests', SCRIPT.with_name('native_tests.py'))
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(hasattr(module, 'dependency_command'), 'installer venv has no pip; use its managed uv')
        for name, binary in [('posix', 'uv'), ('nt', 'uv.exe')]:
            with tempfile.TemporaryDirectory() as d:
                repo = Path(d) / 'clover-c1'; repo.mkdir()
                uv = repo.parent / 'bin' / binary; uv.parent.mkdir(); uv.touch()
                command = module.dependency_command(repo, '/literal/venv/python', name)
                self.assertEqual(command, [str(uv), 'pip', 'install', '--python', '/literal/venv/python', '-e', str(repo)+'[dev]'])


if __name__ == '__main__':
    unittest.main()
