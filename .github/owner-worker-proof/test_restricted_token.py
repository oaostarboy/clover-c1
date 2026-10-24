"""Portable fixture contracts; genuine token/ACL behavior needs the native probe."""
import ctypes as C
import importlib.util
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class TokenInformationFixture:
    """Portable API contract, NOT native Windows token/ABI/permission proof.

    ctypes.wintypes.DWORD follows host unsigned long on Linux. Use an explicit
    32-bit DWORD at the API boundary; fixture structures retain host layout.
    """
    def __init__(self, helper, elevated=0, elevation_type=3):
        self.helper = helper
        self.error = 0
        self.calls = []
        self.values = {20: elevated, 18: elevation_type}
        groups = helper.TG()
        groups.Count = 1
        groups.Groups[0] = helper.SA(102, 0x10)
        self.variable = {1: helper.SA(101, 0), 2: groups,
                         3: helper.TP(), 25: helper.SA(103, 0x20)}
        self.api = object.__new__(helper.WinAPI)
        self.api.a = SimpleNamespace(GetTokenInformation=self.query,
                                    DuplicateTokenEx=lambda *args: True,
                                    CheckTokenMembership=lambda *args: True)
        self.api.k = SimpleNamespace(CloseHandle=lambda handle: True)
        self.api.s = SimpleNamespace(IsUserAnAdmin=lambda: False)
        self.api.sid = lambda text: nullcontext(102)
        self.api.sid_text = lambda sid: {101: 'S-1-5-21-fixture', 102: helper.ADMIN,
                                        103: 'S-1-16-8192'}[sid]

    def query(self, token, kind, buffer, size, returned):
        size = size.value if hasattr(size, 'value') else size
        self.calls.append((kind, buffer is None, size))
        length = C.cast(returned, C.POINTER(C.c_uint32))
        if kind in self.values:
            length.contents.value = 4
            if buffer is None or size != 4:
                self.error = 24  # Exact captured ERROR_BAD_LENGTH on null query.
                return False
            C.cast(buffer, C.POINTER(C.c_uint32)).contents.value = self.values[kind]
            return True
        source = self.variable[kind]
        length.contents.value = C.sizeof(source)
        if buffer is None:
            self.error = 122
            return False
        C.memmove(buffer, C.byref(source), C.sizeof(source))
        return True

    def patches(self):
        return patch.multiple(self.helper.C, get_last_error=lambda: self.error,
                              FormatError=lambda error: 'fixture error ' + str(error),
                              create=True)

    def windows_types(self):
        return patch.object(self.helper, 'W', SimpleNamespace(
            DWORD=C.c_uint32, BOOL=C.c_int32, HANDLE=C.c_void_p))

HERE = Path(__file__).resolve().parent


def load():
    spec = importlib.util.spec_from_file_location('restricted_token', HERE / 'restricted_token.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Contracts(unittest.TestCase):
    def test_snapshot_reads_fixed_elevation_without_a_null_size_probe(self):
        helper = load()
        for elevated, elevation_type in ((0, 3), (1, 2)):
            with self.subTest(elevated=elevated):
                fixture = TokenInformationFixture(helper, elevated, elevation_type)
                with fixture.patches(), fixture.windows_types():
                    observed = fixture.api.snapshot(1, current=True)
                self.assertIs(observed['elevated'], bool(elevated))
                self.assertEqual(observed['elevation_type'], elevation_type)
                self.assertEqual([call for call in fixture.calls if call[0] in (18, 20)],
                                 [(20, False, 4), (18, False, 4)])
                if elevated:
                    with self.assertRaises(RuntimeError):
                        helper.require_limited(observed, observed['sid'], current=True)
                else:
                    helper.require_limited(observed, observed['sid'], current=True)

    def test_fixed_elevation_queries_fail_closed_on_api_errors_and_bad_lengths(self):
        helper = load()
        for kind in (18, 20):
            for error in (5, 24, 122):
                fixture = TokenInformationFixture(helper)
                fixture.error = error
                fixture.api.a.GetTokenInformation = lambda *args: False
                with self.subTest(kind=kind, error=error), fixture.patches(), fixture.windows_types():
                    with self.assertRaises(OSError) as raised:
                        fixture.api.elevation_info(1, kind)
                    self.assertEqual(raised.exception.errno, error)
                    self.assertIn('class=' + str(kind), str(raised.exception))
            for length in (0, 3, 5, 8):
                fixture = TokenInformationFixture(helper)

                def query(token, cls, buffer, size, returned):
                    C.cast(returned, C.POINTER(C.c_uint32)).contents.value = length
                    return True

                fixture.api.a.GetTokenInformation = query
                with self.subTest(kind=kind, length=length), fixture.patches(), fixture.windows_types():
                    with self.assertRaisesRegex(RuntimeError, 'Unexpected elevation information length'):
                        fixture.api.elevation_info(1, kind)
        fixture = TokenInformationFixture(helper)
        with fixture.windows_types(), self.assertRaises(ValueError):
            fixture.api.elevation_info(1, 19)
        self.assertEqual(fixture.calls, [])

    def test_variable_queries_still_require_insufficient_buffer(self):
        helper = load()
        for kind in (1, 2, 3, 25):
            for error in (5, 24):
                fixture = TokenInformationFixture(helper)
                fixture.error = error
                fixture.api.a.GetTokenInformation = lambda *args: False
                with self.subTest(kind=kind, error=error), fixture.patches(), fixture.windows_types():
                    with self.assertRaises(OSError) as raised:
                        fixture.api.info(1, kind)
                    self.assertEqual(raised.exception.errno, error)
            fixture = TokenInformationFixture(helper)
            with fixture.patches(), fixture.windows_types():
                fixture.api.info(1, kind)
            self.assertEqual(fixture.calls, [(kind, True, 0),
                             (kind, False, C.sizeof(fixture.variable[kind]))])

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
