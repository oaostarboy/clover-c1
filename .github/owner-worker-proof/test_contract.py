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
    def test_old_environment_is_created_by_released_installer_not_direct_uv(self):
        import ast
        text = (HERE / 'windows_update.py').read_text(encoding='utf-8')
        tree = ast.parse(text)
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
        calls = [n for n in ast.walk(main) if isinstance(n, ast.Call)]
        installs = [n for n in calls if isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == 'released_installer' and n.func.attr == 'install']
        self.assertEqual(len(installs), 1, 'F1: exact released install.ps1 must create the old install')
        identity_line = next(n.lineno for n in ast.walk(main)
                             if isinstance(n, ast.Constant) and n.value == 'old-source-identity')
        self.assertLess(installs[0].lineno, identity_line)
        self.assertNotIn("'old-install'", text)
        self.assertNotIn("'external-venv'", text)
        self.assertNotIn('g._write_task_script()', text)
        self.assertNotIn('g._install_startup_entry(', text)

    def test_released_installer_bytes_and_real_powershell_file_command(self):
        helper = load('released_installer')
        repo = HERE.parents[1]
        candidate = json.loads((HERE / 'pins.json').read_text(encoding='utf-8'))['target']
        for caller in load('plan').CALLERS.values():
            with self.subTest(caller=caller), tempfile.TemporaryDirectory(dir=HERE) as tmp:
                path = Path(tmp) / 'released-install.ps1'
                proof = helper.materialize(repo, caller, candidate, path)
                expected = subprocess.check_output(['git', '-C', str(repo), 'show', caller + ':scripts/install.ps1'])
                self.assertEqual(path.read_bytes(), expected)
                self.assertEqual(proof['installer_ref'], caller)
                self.assertEqual(proof['caller_sha'], caller)
                self.assertEqual(proof['installer_sha256'], helper.INSTALLER_SHA256)
                argv = helper.command(path, proof)
                self.assertEqual(argv[0], 'powershell.exe')
                self.assertEqual(argv[argv.index('-File') + 1], str(path.resolve()))
                self.assertEqual(argv[argv.index('-Commit') + 1], caller)
                self.assertIn('-ForceCommit', argv)
                self.assertIn('-NonInteractive', argv)
                self.assertIn('-SkipSetup', argv)
                self.assertNotIn('-Stage', argv)  # full install, not a selected subset
                self.assertNotIn('-NoVenv', argv)
                self.assertNotIn('-InstallDir', argv)  # prove the actual default path
                self.assertNotIn('-CloverHome', argv)
                self.assertEqual(helper.command(path, proof, manifest=True), argv + ['-Manifest'])

    def test_installer_refuses_missing_changed_bytes_and_unapproved_hash(self):
        helper = load('released_installer')
        caller = load('plan').CALLERS['1.5.8']
        with tempfile.TemporaryDirectory(dir=HERE) as tmp:
            path = Path(tmp) / 'released-install.ps1'
            proof = helper.materialize(HERE.parents[1], caller, caller, path)
            original = path.read_bytes()
            path.write_bytes(original + b'\r\n# even a comment is forbidden\r\n')
            with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
                helper.command(path, proof)
            path.write_bytes(original)
            proof['installer_sha256'] = '0' * 64
            with self.assertRaises(ValueError):
                helper.command(path, proof)
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                helper.command(path, proof)
            setattr(helper, 'INSTALLER_SHA256', '0' * 64)
            with self.assertRaisesRegex(ValueError, 'released installer SHA256 mismatch'):
                helper.materialize(HERE.parents[1], caller, caller, path)
            self.assertFalse(path.exists())

    def test_installer_rejects_wrong_or_normalized_frozen_caller(self):
        helper = load('released_installer')
        candidate = json.loads((HERE / 'pins.json').read_text(encoding='utf-8'))['target']
        wrong = ['v1.5.6', '32d3e6728c71d1f7c8684b7b41ac1785cb8117a9',
                 load('plan').CALLERS['1.5.8'].upper(), 'a' * 40]
        with tempfile.TemporaryDirectory(dir=HERE) as tmp:
            path = Path(tmp) / 'released-install.ps1'
            for ref in wrong:
                with self.subTest(ref=ref), self.assertRaises(ValueError):
                    helper.materialize(HERE.parents[1], ref, candidate, path)
                self.assertFalse(path.exists())
            # Candidate-to-next uses released installer bytes with the candidate
            # as -Commit, never a modified installer from an unreviewed tree.
            proof = helper.materialize(HERE.parents[1], candidate, candidate, path)
            self.assertEqual(proof['installer_ref'], helper.CANDIDATE_INSTALLER_REF)
            self.assertEqual(helper.command(path, proof)[helper.command(path, proof).index('-Commit') + 1], candidate)

    def test_full_manifest_and_completion_cannot_omit_or_skip_required_stages(self):
        helper = load('released_installer')
        definitions = [dict(name=name) for name in helper.STAGES]
        manifest = dict(protocol_version=1, stages=definitions)
        self.assertEqual(helper.validate_manifest([manifest]), manifest)
        stages = [dict(stage=name, ok=True, skipped=False, reason=None) for name in helper.STAGES]
        completion = dict(ok=True, protocol_version=1)
        self.assertEqual(helper.validate_completion(stages + [completion]), stages)
        for values in [[], [manifest, manifest], [dict(manifest, stages=definitions[:-1])]]:
            with self.assertRaises(ValueError):
                helper.validate_manifest(values)
        for values in [[], stages, stages[:-1] + [completion],
                       [dict(stages[0], skipped=True)] + stages[1:] + [completion],
                       [dict(stages[0], ok=False)] + stages[1:] + [completion],
                       list(reversed(stages)) + [completion]]:
            with self.assertRaises(ValueError):
                helper.validate_completion(values)
        with tempfile.TemporaryDirectory(dir=HERE) as tmp:
            log = Path(tmp) / 'raw.log'
            log.write_text('human output\n' + json.dumps(stages[0]) + '\n', encoding='utf-8')
            self.assertEqual(helper.frames(log), stages[:1])

    def test_private_git_rewrites_really_clone_exact_objects_without_network(self):
        import os
        helper = load('released_installer')
        self.assertTrue(hasattr(helper, 'origin_rewrites'), 'private installer Git rewrites need an exercised contract')
        with tempfile.TemporaryDirectory(dir=HERE) as tmp:
            root = Path(tmp).resolve()
            source, mirror = root / 'source', root / 'origin.git'
            subprocess.run(['git', 'init', '-q', '-b', 'main', source], check=True)
            helper.git(source, 'config', 'user.name', 'Fixture')
            helper.git(source, 'config', 'user.email', 'fixture@example.invalid')
            (source / 'fixture-only').write_bytes(b'synthetic portable Git control, not an install\n')
            helper.git(source, 'add', '.')
            helper.git(source, 'commit', '-qm', 'portable origin control')
            expected = helper.git(source, 'rev-parse', 'HEAD')
            tree = helper.git(source, 'rev-parse', 'HEAD^{tree}')
            subprocess.run(['git', 'clone', '-q', '--mirror', source, mirror], check=True)
            # Refuse every network transport even if a rewrite were broken.
            env = dict(os.environ, GIT_ALLOW_PROTOCOL='file', **helper.origin_rewrites(mirror))
            for i, url in enumerate(('git@github.com:oaostarboy/clover-c1.git',
                                     'https://github.com/oaostarboy/clover-c1.git')):
                installed = root / ('installed-' + str(i))
                subprocess.run(['git', 'clone', '-q', '--depth', '1', '--branch', 'main', url, installed],
                               env=env, check=True)
                self.assertEqual(helper.git(installed, 'rev-parse', 'HEAD'), expected)
                self.assertEqual(helper.git(installed, 'rev-parse', 'HEAD^{tree}'), tree)

    def test_installer_side_effect_entry_is_native_hosted_only(self):
        import sys
        if sys.platform == 'win32':
            self.skipTest('portable non-Windows refusal control')
        with self.assertRaisesRegex(RuntimeError, 'disposable hosted Windows'):
            load('released_installer').install(*([None] * 10))

    def test_startup_registration_uses_supported_cli_and_real_limited_token(self):
        text = (HERE / 'register_startup.ps1').read_text(encoding='utf-8')
        self.assertIn("'clover_cli.main', 'gateway', 'install'", text)
        self.assertIn("'--no-start-now', '--start-on-login'", text)
        self.assertIn('IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)', text)
        self.assertIn('if ($admin) { throw', text)
        self.assertIn('-LogonType S4U -RunLevel Limited', text)
        self.assertIn("'n' | & $Python @argv", text)
        self.assertNotIn('_write_task_script', text)
        self.assertNotIn('_install_startup_entry', text)
        self.assertIn('Unregister-ScheduledTask -TaskName $taskName', text)

    def test_reviewed_native_controls_and_update_acceptance_are_not_weakened(self):
        repo = HERE.parents[1]
        reviewed = '8883828f78049272e7c98b590e142a8ec56d8359'
        # The bootstrap path resolution is deliberately corrected and covered
        # by test_bootstrap_resolution.py; all native safety controls remain
        # byte-identical to the independently reviewed fixture.
        for name in ('frozen_postpull.py', 'plan.py', 'start_troy.ps1',
                     'topology.py', 'test_owner_worker_native.py', 'test_status_lock_windows_native.py'):
            with self.subTest(file=name):
                # Compare the exact reviewed checkout representation, including
                # the repository's existing .ps1 CRLF checkout attribute.
                original = subprocess.check_output(['git', '-C', str(repo), 'cat-file', '--filters',
                    reviewed + ':.github/owner-worker-proof/' + name])
                self.assertEqual((HERE / name).read_bytes(), original)
        original = subprocess.check_output(['git', '-C', str(repo), 'show',
            reviewed + ':.github/owner-worker-proof/windows_update.py']).decode('utf-8')
        current = (HERE / 'windows_update.py').read_text(encoding='utf-8')
        start, end = '        before = wait_for(initial, 180)', "\n\nif __name__ == '__main__':"
        self.assertEqual(current.split(start, 1)[1].split(end, 1)[0],
                         original.split(start, 1)[1].split(end, 1)[0])

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

    def test_bootstrap_tool_requires_explicit_absolute_existing_file(self):
        helper = load('bootstrap')
        for value in [None, '', 'uv']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                helper.resolve(value)
        with tempfile.TemporaryDirectory() as directory:
            tool = Path(directory) / 'uv.exe'
            tool.write_bytes(b'path-only fixture; not executed')
            self.assertEqual(helper.resolve(str(tool)), str(tool.resolve()))
            with self.assertRaises(ValueError):
                helper.resolve(directory)
            with self.assertRaises(FileNotFoundError):
                helper.resolve(str(Path(directory) / 'missing-uv.exe'))

    def test_exact_frozen_postpull_excerpt_preserves_systemexit_escape(self):
        import builtins
        from types import SimpleNamespace
        p = load('plan')
        helper = load('frozen_postpull')
        repo = HERE.parents[1]
        refs = [*p.CALLERS.values(), '27f507acb4f12c30d237471095f137007952c312']
        for ref in refs:
            with self.subTest(caller=ref):
                code, receipt = helper.load(repo, ref)
                self.assertEqual(receipt['caller_sha'], ref)
                calls = []
                def stop():
                    calls.append(ref)
                    raise SystemExit(1)
                def forbidden(*args, **kwargs):
                    raise AssertionError('portable control must not execute dependency work')
                api = SimpleNamespace(ensure_uv=forbidden, update_managed_uv=stop)
                def controlled_import(name, *args, **kwargs):
                    self.assertEqual(name, 'clover_cli.managed_uv')
                    return api
                namespace = dict(__builtins__=dict(vars(builtins), __import__=controlled_import),
                    _shim_quarantine_error_type=lambda: RuntimeError, subprocess=subprocess)
                with self.assertRaises(SystemExit) as escaped:
                    exec(code, namespace)
                self.assertEqual(escaped.exception.code, 1)
                self.assertEqual(calls, [ref])

    def test_frozen_excerpt_ref_is_not_repaired_or_normalized(self):
        with self.assertRaises(AssertionError):
            load('frozen_postpull').load(HERE.parents[1], 'v1.5.6')

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
