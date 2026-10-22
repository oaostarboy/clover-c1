"""Verifier-only binding contracts; native runner proves the actual process."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('native_proof', Path(__file__).with_name('proof.py'))
assert spec is not None and spec.loader is not None
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


class ExternalLaunch(unittest.TestCase):
    def test_exact_external_interpreter_or_entrypoint_is_bound(self):
        repo = Path('/private/code')
        runtime = Path('/private/owned external environment')
        for argv in [
            [str(runtime / 'bin/python'), str(runtime / 'bin/clover'), 'gateway', 'run'],
            [str(runtime / 'bin/clover'), 'gateway', 'run'],
        ]:
            self.assertTrue(proof.gateway_launch_matches(argv, repo, runtime))
        for argv in [
            ['/foreign/bin/python', '/foreign/bin/clover', 'gateway', 'run'],
            ['/foreign/bin/python', '-c', str(runtime / 'bin/clover'), 'gateway', 'run'],
            [str(runtime / 'bin/python'), 'chat', '-q', 'gateway run'],
            [str(runtime / 'bin/python'), '/foreign/bin/clover', 'gateway', 'run'],
            [str(runtime / 'bin/clover'), 'chat', '--tag', 'gateway', 'run'],
            [str(runtime / 'bin/clover'), 'gateway', '--unrelated', 'run'],
        ]:
            self.assertFalse(proof.gateway_launch_matches(argv, repo, runtime))
        # Never accept sibling environments merely because base Python matches.
        self.assertFalse(proof.gateway_launch_matches(
            ['/private/other/bin/python', '/private/other/bin/clover', 'gateway', 'run'],
            repo, runtime))

    def test_nonexternal_existing_repository_match_stays_available(self):
        self.assertTrue(proof.gateway_launch_matches(
            ['/private/code/venv/bin/python', '/private/code/venv/bin/clover', 'gateway', 'run'],
            Path('/private/code'), None))

    def test_external_probe_uses_reviewed_helper_not_product_checkout_helper(self):
        spec = importlib.util.spec_from_file_location('external_probe', Path(__file__).with_name('macos_external_env.py'))
        assert spec is not None and spec.loader is not None
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        self.assertEqual(probe.proof_support(), Path(__file__).with_name('proof.py'))
        self.assertTrue(probe.proof_support().is_file())


if __name__ == '__main__':
    unittest.main()
