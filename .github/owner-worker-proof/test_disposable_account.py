"""Portable fail-closed account contracts. Not native logon/ACL proof."""
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent


def load_token():
    spec = importlib.util.spec_from_file_location('account_token', HERE / 'restricted_token.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AccountContracts(unittest.TestCase):
    def test_genuine_account_requires_new_sid_and_no_administrators_sid(self):
        helper = load_token()
        expected = 'S-1-5-21-111-222-333-1001'
        good = dict(sid=expected, admin_member=False, elevated=False,
                    integrity_rid=8192, groups={'S-1-5-32-545': 7},
                    enabled_privileges=['SeChangeNotifyPrivilege'], is_user_an_admin=False)
        helper.require_limited(good, expected, current=True)
        for replacement in ({'sid': 'runner-sid'}, {'groups': {helper.ADMIN: 0x10}},
                            {'groups': {}}, {'admin_member': True}, {'elevated': True},
                            {'integrity_rid': 12288}, {'is_user_an_admin': True},
                            {'enabled_privileges': ['SeDebugPrivilege']}):
            with self.subTest(replacement=replacement), self.assertRaises(RuntimeError):
                helper.require_limited(dict(good, **replacement), expected, current=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
