"""Lane-local Git/ownership contracts, not macOS or updater certification."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.request

import psutil

SCRIPT = Path(__file__).with_name('proof.py')
SPEC = importlib.util.spec_from_file_location('posix_native_proof', SCRIPT)
assert SPEC and SPEC.loader
proof = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proof)


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True,
                                   stderr=subprocess.PIPE).strip()


class ShallowOrigin(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-posix-git-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        git(self.source, 'init', '-q')
        git(self.source, 'config', 'user.name', 'native fixture')
        git(self.source, 'config', 'user.email', 'fixture@example.invalid')
        (self.source / 'product').write_text('old\n')
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-qm', 'old')
        git(self.source, 'tag', 'v1.5.5')
        (self.source / 'product').write_text('candidate\n')
        git(self.source, 'commit', '-qam', 'candidate')
        self.candidate = git(self.source, 'rev-parse', 'HEAD')
        self.candidate_tree = git(self.source, 'rev-parse', 'HEAD^{tree}')
        git(self.source, 'branch', 'install')
        marker = self.source / proof.MARKER
        marker.parent.mkdir(parents=True)
        marker.write_text('next marker\n')
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-qm', 'next marker')
        self.target = git(self.source, 'rev-parse', 'HEAD')
        self.target_tree = git(self.source, 'rev-parse', 'HEAD^{tree}')
        git(self.source, 'branch', 'ci/native-next/fixture')
        self.repo = self.root / 'installed'
        subprocess.run(['git', 'clone', '-q', '--depth', '1', '--branch', 'install',
                        self.source.as_uri(), str(self.repo)], check=True)
        # Exactly the native POSIX preparation: fetch next into a shallow install.
        git(self.repo, 'fetch', '--no-tags', str(self.source), self.target)
        self.mirror = self.root / 'origin.git'

    def test_shallow_install_can_serve_exact_marker_child_without_source_repair(self):
        self.assertEqual(git(self.repo, 'rev-parse', '--is-shallow-repository'), 'true')
        before = git(self.repo, 'rev-parse', 'HEAD')
        before_tree = git(self.repo, 'rev-parse', 'HEAD^{tree}')
        target_object = git(self.repo, 'cat-file', 'commit', self.target)
        proof.fixture_origin(self.repo, self.mirror, self.target)
        self.assertEqual(git(self.mirror, 'rev-parse', 'refs/heads/main'), self.target)
        self.assertEqual(git(self.mirror, 'rev-parse', 'main^{tree}'), self.target_tree)
        self.assertEqual(git(self.mirror, 'cat-file', 'commit', self.target), target_object)
        self.assertEqual([line for line in target_object.splitlines() if line.startswith('parent ')],
                         ['parent ' + self.candidate])
        self.assertEqual(git(self.mirror, 'diff', '--name-only', self.candidate, self.target), proof.MARKER)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), before)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD^{tree}'), before_tree)
        self.assertEqual(git(self.repo, 'diff', '--name-only', 'HEAD'), '')
        for repo in [self.repo, self.mirror]:
            self.assertEqual(git(repo, 'replace', '-l'), '')
            self.assertFalse((repo / ('' if repo == self.mirror else '.git') / 'info/grafts').exists())
        # Real fetch + ff-only merge exercise the Git seam used by the updater.
        git(self.repo, 'fetch', 'origin', 'main')
        git(self.repo, 'merge', '--ff-only', 'origin/main')
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.target)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD^{tree}'), self.target_tree)


    def test_missing_target_fails_before_repointing_origin(self):
        origin = git(self.repo, 'remote', 'get-url', 'origin')
        with self.assertRaises(subprocess.CalledProcessError):
            proof.fixture_origin(self.repo, self.mirror, '0' * 40)
        self.assertEqual(git(self.repo, 'remote', 'get-url', 'origin'), origin)
        self.assertFalse(self.mirror.exists())

    def test_missing_tree_fails_before_repointing_origin(self):
        git(self.source, 'remote', 'add', 'origin', self.source.as_uri())
        tree_object = self.source / '.git/objects' / self.target_tree[:2] / self.target_tree[2:]
        tree_object.unlink()
        with self.assertRaises(subprocess.CalledProcessError):
            proof.fixture_origin(self.source, self.mirror, self.target)
        self.assertEqual(git(self.source, 'remote', 'get-url', 'origin'), self.source.as_uri())

    def test_missing_blob_fails_before_repointing_origin(self):
        git(self.source, 'remote', 'add', 'origin', self.source.as_uri())
        blob = git(self.source, 'rev-parse', self.target + ':' + proof.MARKER)
        (self.source / '.git/objects' / blob[:2] / blob[2:]).unlink()
        with self.assertRaises(subprocess.CalledProcessError):
            proof.fixture_origin(self.source, self.mirror, self.target)
        self.assertEqual(git(self.source, 'remote', 'get-url', 'origin'), self.source.as_uri())

    def test_existing_fixture_is_not_adopted(self):
        self.mirror.mkdir()
        origin = git(self.repo, 'remote', 'get-url', 'origin')
        with self.assertRaisesRegex(ValueError, 'must be new'):
            proof.fixture_origin(self.repo, self.mirror, self.target)
        self.assertEqual(git(self.repo, 'remote', 'get-url', 'origin'), origin)

    def test_moved_next_ref_is_rejected_by_unchanged_plan(self):
        # Resolve a real exact marker first, then move its branch: no re-pinning.
        git(self.source, 'remote', 'add', 'origin', self.source.as_uri())
        accepted = proof.plan(self.source, 'v1.5.5', self.candidate,
                              self.candidate_tree, 'ci/native-next/fixture')
        self.assertEqual(accepted['next_sha'], self.target)
        (self.source / 'product').write_text('unexpected product change\n')
        git(self.source, 'commit', '-qam', 'wrong next')
        git(self.source, 'branch', '-f', 'ci/native-next/fixture')
        with self.assertRaisesRegex(ValueError, 'exactly one parent'):
            proof.plan(self.source, 'v1.5.5', self.candidate,
                       self.candidate_tree, 'ci/native-next/fixture')
        # The literal target still selects its exact real object, not moved HEAD.
        proof.fixture_origin(self.repo, self.mirror, self.target)
        self.assertEqual(git(self.mirror, 'rev-parse', 'main'), self.target)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.candidate)


CHILD = '''\
import json
import os
from pathlib import Path
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')
    def log_message(self, *args):
        pass

if sys.argv[-1] == 'idle':
    Path(os.environ['CLOVER_HOME'], 'ready.json').write_text('{"port": 0}')
    time.sleep(60)
else:
    server = HTTPServer(('127.0.0.1', 0), Health)
    Path(os.environ['CLOVER_HOME'], 'ready.json').write_text(json.dumps({'port':server.server_port}))
    server.serve_forever()
'''


class GatewayOwnership(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-posix-process-')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'installed'
        self.repo.mkdir()
        self.home = Path(self.temp.name) / 'home'
        self.home.mkdir()
        self.script = self.repo / 'disposable.py'
        self.script.write_text(CHILD)
        self.sha = 'b' * 40

    def launch(self, *, mode='listen', home=None, gateway=True):
        home = home or self.home
        ready = home / 'ready.json'
        ready.unlink(missing_ok=True)
        argv = [sys.executable, str(self.script)]
        if gateway:
            argv += ['gateway', 'run']
        argv += [mode]
        proc = subprocess.Popen(argv, env={**os.environ, 'CLOVER_HOME': str(home)},
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        def stop():
            if proc.poll() is None:
                proc.terminate()
            proc.communicate(timeout=5)
        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while not ready.exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                self.fail('disposable process did not become ready')
            time.sleep(0.01)
        return proc, json.loads(ready.read_text())['port']

    def state_for(self, proc):
        stat = Path('/proc') / str(proc.pid) / 'stat'
        start = (stat.read_text().rsplit(')', 1)[1].split()[19] if stat.exists()
                 else str(psutil.Process(proc.pid).create_time() * 100))
        state = dict(pid=proc.pid, code_sha=self.sha, start_time=start,
                     gateway_state='running', restart_requested=False)
        (self.home / 'gateway_state.json').write_text(json.dumps(state))
        return state

    def test_darwin_selection_observes_real_owned_socket_without_global_access(self):
        # Actual sockets/processes on this host, NOT native macOS certification.
        proc, port = self.launch()
        state = self.state_for(proc)
        with mock.patch.object(sys, 'platform', 'darwin'), mock.patch.object(
                psutil, 'net_connections', side_effect=psutil.AccessDenied(pid=1)) as global_scan:
            self.assertEqual(proof.observe_gateway(self.repo, self.home, self.sha, port), state)
        global_scan.assert_not_called()


    def test_actual_host_owned_listener(self):
        proc, port = self.launch()
        state = self.state_for(proc)
        self.assertEqual(proof.observe_gateway(self.repo, self.home, self.sha, port), state)

    def test_foreign_health_listener_is_not_ownership(self):
        owned, _ = self.launch(mode='idle')
        foreign_home = Path(self.temp.name) / 'foreign'
        foreign_home.mkdir()
        _, port = self.launch(home=foreign_home, gateway=False)
        self.state_for(owned)
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3) as response:
            self.assertEqual(json.load(response), {'status': 'ok'})
        with mock.patch.object(sys, 'platform', 'darwin'), mock.patch.object(
                urllib.request, 'urlopen') as health, self.assertRaises(ValueError):
            proof.observe_gateway(self.repo, self.home, self.sha, port)
        health.assert_not_called()

    def test_duplicate_gateway_fails(self):
        owned, port = self.launch()
        self.launch(mode='idle')
        self.state_for(owned)
        with mock.patch.object(sys, 'platform', 'darwin'), self.assertRaises(ValueError):
            proof.observe_gateway(self.repo, self.home, self.sha, port)

    def test_owned_socket_access_denied_fails_before_health(self):
        owned, port = self.launch()
        self.state_for(owned)
        with mock.patch.object(sys, 'platform', 'darwin'), mock.patch.object(
                psutil.Process, 'net_connections', side_effect=psutil.AccessDenied(owned.pid)), \
                mock.patch.object(urllib.request, 'urlopen') as health, \
                self.assertRaises(psutil.AccessDenied):
            proof.observe_gateway(self.repo, self.home, self.sha, port)
        health.assert_not_called()

    def test_empty_owned_socket_observation_fails(self):
        owned, port = self.launch()
        self.state_for(owned)
        with mock.patch.object(sys, 'platform', 'darwin'), mock.patch.object(
                psutil.Process, 'net_connections', return_value=[]), self.assertRaises(ValueError):
            proof.observe_gateway(self.repo, self.home, self.sha, port)

    def test_unobservable_possible_duplicate_fails_closed(self):
        owned, port = self.launch()
        duplicate, _ = self.launch(mode='idle')
        self.state_for(owned)
        original = psutil.Process.environ
        def environment(proc):
            if proc.pid == duplicate.pid:
                raise psutil.AccessDenied(proc.pid)
            return original(proc)
        with mock.patch.object(sys, 'platform', 'darwin'), mock.patch.object(
                psutil.Process, 'environ', environment), self.assertRaises(psutil.AccessDenied):
            proof.observe_gateway(self.repo, self.home, self.sha, port)

    def test_wrong_process_start_identity_fails(self):
        owned, port = self.launch()
        state = self.state_for(owned)
        state['start_time'] = '1'
        (self.home / 'gateway_state.json').write_text(json.dumps(state))
        with mock.patch.object(sys, 'platform', 'darwin'), self.assertRaisesRegex(ValueError, 'start'):
            proof.observe_gateway(self.repo, self.home, self.sha, port)

    def test_state_pid_without_owned_gateway_root_fails(self):
        foreign_home = Path(self.temp.name) / 'foreign'
        foreign_home.mkdir()
        foreign, port = self.launch(home=foreign_home)
        self.state_for(foreign)
        with mock.patch.object(sys, 'platform', 'darwin'), self.assertRaises(ValueError):
            proof.observe_gateway(self.repo, self.home, self.sha, port)


if __name__ == '__main__':
    unittest.main()
