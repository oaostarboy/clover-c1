"""Portable real-Git diagnostic contracts, NOT native Windows certification."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('proof.py').resolve()
PREFIX = 'INSTALLED_SOURCE_REJECTION '


class InstalledSourceDiagnostics(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'checkout'
        self.repo.mkdir()
        self.neutral = self.root / 'neutral'
        self.neutral.mkdir()
        # No user Git config, hooks, providers, or home files participate.
        self.env = {key: value for key, value in os.environ.items()
                    if key in {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP'}}
        self.env.update(HOME=str(self.root), USERPROFILE=str(self.root),
                        GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_TERMINAL_PROMPT='0', PYTHONUTF8='1')
        self.git('init', '-q')
        self.git('config', 'user.name', 'identity diagnostic fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'core.autocrlf', 'false')
        (self.repo / '.gitattributes').write_bytes(b'*.py text eol=lf\n*.ps1 text eol=crlf\n')
        self.original = b'print("approved")\n'
        (self.repo / 'sample.py').write_bytes(self.original)
        self.git('add', '.')
        self.git('commit', '-qm', 'approved source')
        self.sha = self.git('rev-parse', 'HEAD')
        self.tree = self.git('rev-parse', 'HEAD^{tree}')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args],
                                       env=self.env, text=True).strip()

    def reject(self):
        output = self.neutral / 'imports.json'
        result = subprocess.run([sys.executable, str(SCRIPT), 'identity',
                                 '--repo', str(self.repo), '--sha', self.sha,
                                 '--tree', self.tree, '--output', str(output)],
                                cwd=self.neutral, env=self.env, capture_output=True,
                                text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists(), 'rejection must never produce a success receipt')
        self.assertIn('ValueError: replacement refs or modified tracked source', result.stderr)
        receipts = [line[len(PREFIX):] for line in result.stderr.splitlines()
                    if line.startswith(PREFIX)]
        self.assertEqual(len(receipts), 1, 'rejection must preserve one structured diagnostic before raising')
        return json.loads(receipts[0])

    def test_real_content_mutation_preserves_read_only_path_blob_and_attribute_evidence(self):
        changed = b'print("unauthorized")\r\n'
        (self.repo / 'sample.py').write_bytes(changed)
        (self.repo / '.env').write_text('PROVIDER_SECRET=must-not-leak')
        index = self.repo / '.git/index'
        before_index = index.read_bytes()
        receipt = self.reject()
        self.assertFalse(receipt['accepted'])
        self.assertEqual(receipt['schema'], 'native-installed-source-rejection/v1')
        self.assertEqual(receipt['declared'], {'sha': self.sha, 'tree': self.tree})
        self.assertEqual(receipt['observed'], {'sha': self.sha, 'tree': self.tree})
        self.assertEqual(receipt['replacement_refs'], '')
        self.assertEqual(receipt['changed_paths'], ['sample.py'])
        self.assertEqual(receipt['git']['replace_refs']['stdout'], '')
        self.assertIn('sample.py', receipt['git']['status']['stdout'])
        self.assertIn('sample.py', receipt['git']['diff_raw']['stdout'])
        path = receipt['paths'][0]
        self.assertEqual(path['path'], 'sample.py')
        self.assertEqual(path['checkout']['sha256'], hashlib.sha256(changed).hexdigest())
        self.assertEqual(path['declared_blob']['sha256'], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(path['declared_oid'], self.git('rev-parse', self.sha + ':sample.py'))
        self.assertFalse(path['raw_bytes_equal_declared'])
        self.assertFalse(path['crlf_to_lf_equal_declared'])
        self.assertIn('sample.py\x00eol\x00lf\x00', path['attributes']['stdout'])
        self.assertEqual((self.repo / 'sample.py').read_bytes(), changed)
        self.assertEqual(index.read_bytes(), before_index, 'diagnosis must not refresh/write the index')
        self.assertNotIn('must-not-leak', json.dumps(receipt))
        self.assertNotIn('PROVIDER_SECRET', json.dumps(receipt))


    def test_failure_receipt_survives_in_hosted_evidence_directory(self):
        self.env['RUNNER_TEMP'] = str(self.root / 'runner-temp')
        (self.repo / 'sample.py').write_bytes(b'changed content\n')
        receipt = self.reject()
        artifact = Path(self.env['RUNNER_TEMP']) / 'evidence/installed-source-rejection.json'
        self.assertTrue(artifact.is_file(), 'native artifact upload must retain diagnostic JSON')
        self.assertEqual(json.loads(artifact.read_text(encoding='utf-8')), receipt)

    def test_replacement_ref_is_distinct_from_clean_tracked_source(self):
        old = self.sha
        self.git('commit', '--allow-empty', '-qm', 'replacement target fixture')
        self.sha = self.git('rev-parse', 'HEAD')
        self.git('replace', old, self.sha)
        receipt = self.reject()
        self.assertEqual(receipt['replacement_refs'], old)
        self.assertEqual(receipt['changed_paths'], [])
        self.assertEqual(receipt['git']['replace_refs']['stdout'],
                         f'refs/replace/{old} {self.sha}\n')
        self.assertEqual(self.git('replace', '-l'), old, 'diagnosis must never delete replacement refs')
        self.assertEqual(receipt['observed'], {'sha': self.sha, 'tree': self.tree})

    def test_staged_mutation_and_deleted_tracked_path_remain_rejections(self):
        (self.repo / 'sample.py').write_bytes(b'staged mutation\n')
        self.git('add', 'sample.py')
        staged = self.reject()
        self.assertEqual(staged['changed_paths'], ['sample.py'])
        self.assertIn('M  sample.py\x00', staged['git']['status']['stdout'])
        (self.repo / 'sample.py').unlink()
        deleted = self.reject()
        self.assertEqual(deleted['paths'][0]['checkout']['error'], 'FileNotFoundError')
        self.assertEqual(deleted['paths'][0]['declared_blob']['sha256'],
                         hashlib.sha256(self.original).hexdigest())

    def test_spaces_and_unicode_paths_are_not_git_quote_strings(self):
        name = 'space caf\u00e9.py'
        (self.repo / name).write_bytes(self.original)
        self.git('add', '.')
        self.git('commit', '-qm', 'path fixture')
        self.sha = self.git('rev-parse', 'HEAD')
        self.tree = self.git('rev-parse', 'HEAD^{tree}')
        (self.repo / name).write_bytes(b'real changed bytes\n')
        receipt = self.reject()
        self.assertEqual(receipt['changed_paths'], [name])
        self.assertEqual(receipt['paths'][0]['path'], name)
        self.assertEqual(receipt['paths'][0]['declared_blob']['sha256'],
                         hashlib.sha256(self.original).hexdigest())

    def clean_identity(self, *, foreign=False, direct_url=None):
        # Real fixture imports; only metadata location is injected. This is
        # not an installed Clover/Windows result and never changes a venv.
        (self.repo / 'clover_cli').mkdir()
        (self.repo / 'clover_cli/__init__.py').write_bytes(b'')
        for name in ['agent', 'gateway', 'cron']:
            (self.repo / name).mkdir()
            (self.repo / name / '__init__.py').write_bytes(b'')
        (self.repo / 'cron/notepad.py').write_bytes(b'')
        (self.repo / 'sample.ps1').write_bytes(b'Write-Output "approved"\r\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'real import fixture with declared CRLF checkout')
        self.sha = self.git('rev-parse', 'HEAD')
        self.tree = self.git('rev-parse', 'HEAD^{tree}')
        self.git('remote', 'add', 'origin', 'https://example.invalid/fixture.git')
        site = self.root / 'site-packages'
        metadata = site / 'clover_c1-0.0.dist-info'
        metadata.mkdir(parents=True)
        (metadata / 'direct_url.json').write_text(json.dumps({
            'url': direct_url or self.repo.as_uri(), 'dir_info': {'editable': True}}))
        foreign_root = self.root / 'foreign'
        foreign_root.mkdir()
        (foreign_root / 'clover_cli.py').write_bytes(b'')
        code = '''import importlib.util, json, pathlib, sys, sysconfig
script, repo, sha, tree, site, foreign = sys.argv[1:]
sys.path.insert(0, repo)
if foreign:
    sys.path.insert(0, foreign)
spec = importlib.util.spec_from_file_location('identity_fixture', script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sysconfig.get_path = lambda key: site
print(json.dumps(module.installed(repo, sha, tree)))
'''
        result = subprocess.run([sys.executable, '-c', code, str(SCRIPT), str(self.repo),
                                 self.sha, self.tree, str(site), str(foreign_root) if foreign else ''],
                                env=self.env, cwd=self.neutral, text=True,
                                capture_output=True, timeout=30)
        return result

    def test_declared_crlf_checkout_matches_git_policy_but_real_mutation_still_fails(self):
        self.git('config', 'core.autocrlf', 'true')
        result = self.clean_identity()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['head'], self.sha)
        path = self.repo / 'sample.ps1'
        self.assertIn(b'\r\n', path.read_bytes())
        blob = subprocess.check_output(['git', '-C', str(self.repo), 'show',
                                        self.sha + ':sample.ps1'], env=self.env)
        self.assertNotIn(b'\r\n', blob)
        self.assertEqual(self.git('diff', '--name-only', 'HEAD'), '')
        path.write_bytes(b'Write-Output "unapproved"\r\n')
        receipt = self.reject()
        self.assertEqual(receipt['changed_paths'], ['sample.ps1'])
        self.assertFalse(receipt['paths'][0]['crlf_to_lf_equal_declared'])
        self.assertIn('sample.ps1\x00eol\x00crlf\x00', receipt['paths'][0]['attributes']['stdout'])

    def test_clean_source_does_not_bypass_foreign_import_rejection(self):
        result = self.clean_identity(foreign=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('foreign import: clover_cli', result.stderr)
        self.assertNotIn(PREFIX, result.stderr)

    def test_clean_source_does_not_bypass_foreign_direct_url_rejection(self):
        result = self.clean_identity(direct_url='https://example.invalid/foreign')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('editable installation points outside installed checkout', result.stderr)
        self.assertNotIn(PREFIX, result.stderr)

    def test_normalized_byte_similarity_is_diagnostic_only_not_acceptance(self):
        (self.repo / '.gitattributes').write_bytes(b'*.py -text\n')
        self.git('add', '.gitattributes')
        self.git('commit', '-qm', 'literal-byte fixture policy')
        self.sha = self.git('rev-parse', 'HEAD')
        self.tree = self.git('rev-parse', 'HEAD^{tree}')
        (self.repo / 'sample.py').write_bytes(self.original.replace(b'\n', b'\r\n'))
        receipt = self.reject()
        self.assertTrue(receipt['paths'][0]['crlf_to_lf_equal_declared'])
        self.assertFalse(receipt['paths'][0]['raw_bytes_equal_declared'])
        self.assertFalse(receipt['accepted'])

    def test_unwritable_or_inside_source_artifact_path_never_reopens_rejection(self):
        (self.repo / 'sample.py').write_bytes(b'unapproved source\n')
        blocker = self.root / 'not-a-directory'
        blocker.write_bytes(b'keep this file')
        for destination in [blocker, self.repo]:
            with self.subTest(destination=destination):
                self.env['RUNNER_TEMP'] = str(destination)
                receipt = self.reject()
                self.assertIn('artifact_error', receipt)
                self.assertFalse((self.repo / 'evidence').exists())
        self.assertEqual(blocker.read_bytes(), b'keep this file')
        self.assertEqual((self.repo / 'sample.py').read_bytes(), b'unapproved source\n')

    def test_head_tree_and_outside_source_preconditions_remain_strict(self):
        for sha, tree, cwd, pythonpath, expected in [
                ('0' * 40, self.tree, self.neutral, '', 'installed HEAD/tree mismatch'),
                (self.sha, '0' * 40, self.neutral, '', 'installed HEAD/tree mismatch'),
                (self.sha, self.tree, self.repo, '', 'installed probe must run outside source'),
                (self.sha, self.tree, self.neutral, 'foreign', 'installed probe must run outside source')]:
            env = dict(self.env)
            if pythonpath:
                env['PYTHONPATH'] = pythonpath
            result = subprocess.run([sys.executable, str(SCRIPT), 'identity', '--repo', str(self.repo),
                                     '--sha', sha, '--tree', tree, '--output', str(self.neutral / 'result.json')],
                                    env=env, cwd=cwd, text=True, capture_output=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected, result.stderr)
            self.assertNotIn(PREFIX, result.stderr)
            self.assertFalse((self.neutral / 'result.json').exists())


if __name__ == '__main__':
    unittest.main()
