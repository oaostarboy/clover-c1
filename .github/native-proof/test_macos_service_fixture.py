"""Linux-portable fixture contracts; not native product acceptance."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import urllib.request

SPEC = importlib.util.spec_from_file_location('mac_fixture', Path(__file__).with_name('macos_service_upgrade.py'))
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class Contracts(unittest.TestCase):
    def test_real_loopback_provider_holds_post_tool_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            effect = root / 'effects'
            provider = fixture.LocalProvider(root, effect, 'effect-once', 'reply-once')
            self.addCleanup(provider.server.server_close)
            self.addCleanup(provider.server.shutdown)
            def post(messages):
                req = urllib.request.Request('http://127.0.0.1:18792/v1/chat/completions',
                    data=json.dumps({'messages': messages, 'tools': [{'type': 'function', 'function': {'name': 'terminal'}}]}).encode(),
                    headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=10) as response:
                    return json.load(response)
            first = post([{'role': 'user', 'content': 'explicit synthetic contract input'}])
            call = first['choices'][0]['message']['tool_calls'][0]
            self.assertEqual(call['id'], 'native-call-once')
            subprocess.run(json.loads(call['function']['arguments'])['command'], shell=True, check=True)
            self.assertEqual(effect.read_text(), 'effect-once\n')
            replies = []
            worker = threading.Thread(target=lambda: replies.append(post([{'role': 'tool', 'tool_call_id': call['id'], 'content': 'synthetic tool result'}])))
            worker.start()
            try:
                self.assertTrue(provider.entered.wait(3))
                self.assertTrue(worker.is_alive())
                self.assertEqual(replies, [])
            finally:
                provider.release.set()
                worker.join(10)
            self.assertEqual(replies[0]['choices'][0]['message']['content'], 'reply-once')
            self.assertEqual(effect.read_text(), 'effect-once\n')

    def test_linux_refuses_before_install(self):
        result = subprocess.run(['python', str(Path(fixture.__file__)), '--source', '/not-an-install'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires native macOS', result.stderr)

    def test_no_acceptance_driver_restart(self):
        source = Path(fixture.__file__).read_text()
        self.assertNotIn("[bin, 'gateway', 'restart']", source)
        self.assertNotIn("[bin, 'gateway', 'start']", source)
        self.assertIn("[bin, 'gateway', 'install']", source)
        self.assertIn('proof.check_receipt(read_json(receipt), began, a.old, a.target)', source)
        self.assertIn('witness.verify(pending, home, effect, receiver)', source)


if __name__ == '__main__':
    unittest.main()
