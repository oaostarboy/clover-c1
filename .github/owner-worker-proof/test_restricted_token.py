"""Portable fixture contracts; genuine token/ACL behavior needs the native probe."""
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent


def load():
    spec = importlib.util.spec_from_file_location('restricted_token', HERE / 'restricted_token.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Contracts(unittest.TestCase):
    def test_registration_does_not_confuse_task_policy_with_filtered_token(self):
        text = (HERE / 'register_startup.ps1').read_text(encoding='utf-8')
        self.assertIn("'restricted_token.py'", text)
        self.assertNotIn('New-ScheduledTaskPrincipal', text)
        self.assertIn('if ($admin) { throw', text)
        self.assertIn("'n' | & $Python @argv", text)
        self.assertNotIn('_install_startup_entry', text)

    def test_token_predicates_reject_each_unsafe_or_missing_observation(self):
        import copy
        helper = load()
        # Synthetic inputs test only fail-closed validation, never OS identity.
        good = dict(sid='S-1-5-21-fixture', admin_member=False, elevated=False,
                    integrity_rid=8192, groups={helper.ADMIN: 0x10},
                    enabled_privileges=['SeChangeNotifyPrivilege'], is_user_an_admin=False)
        helper.require_limited(good, good['sid'])
        for key, bad in [('sid', 'foreign'), ('admin_member', True), ('admin_member', None),
                         ('elevated', True), ('elevated', None), ('integrity_rid', 12288),
                         ('groups', {}), ('groups', {helper.ADMIN: 0x14}),
                         ('enabled_privileges', ['SeDebugPrivilege']), ('is_user_an_admin', True)]:
            with self.subTest(key=key, bad=bad), self.assertRaises(RuntimeError):
                helper.require_limited(dict(good, **{key: bad}), good['sid'])
        for key in good:
            if key == 'is_user_an_admin':
                continue  # Selected (not current) tokens cannot call the shell API.
            missing = copy.deepcopy(good)
            del missing[key]
            with self.subTest(missing=key), self.assertRaises((KeyError, RuntimeError)):
                helper.require_limited(missing, good['sid'])

    def test_child_environment_is_allowlisted_and_keeps_owned_profile(self):
        helper = load()
        env = dict(USERPROFILE='owned-profile', HOME='private-home', CLOVER_HOME='private-clover',
                   PATH='system-path', SystemRoot='windows', GITHUB_TOKEN='not-a-real-token',
                   ACTIONS_RUNTIME_TOKEN='not-a-real-token', OPENAI_API_KEY='not-a-real-key',
                   PYTHONPATH='foreign-imports', PYTHONHOME='foreign-runtime', LD_PRELOAD='foreign')
        limited = helper.child_environment(env)
        for key in ('USERPROFILE', 'HOME', 'CLOVER_HOME', 'PATH', 'SystemRoot'):
            self.assertEqual(limited[key], env[key])
        self.assertTrue(all(k not in limited for k in ('GITHUB_TOKEN', 'ACTIONS_RUNTIME_TOKEN',
                            'OPENAI_API_KEY', 'PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD')))
        self.assertEqual(limited['PYTHONNOUSERSITE'], '1')

    def test_non_windows_entry_refuses_before_native_actions(self):
        import sys
        if sys.platform == 'win32':
            self.skipTest('portable refusal control')
        with self.assertRaisesRegex(RuntimeError, 'Native Windows required'):
            load().WinAPI()


if __name__ == '__main__':
    unittest.main(verbosity=2)
