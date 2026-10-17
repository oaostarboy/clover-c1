"""Direct locked-Windows Job API assertions; no product or console substitutes."""
import ast
from contextlib import ExitStack
import ctypes
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

FIXTURE = Path(__file__).with_name('windows_console.py')


def constructor_args():
    tree = ast.parse(FIXTURE.read_text(encoding='utf-8'))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_console')
    calls = [n for n in ast.walk(function) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
             and n.func.value.id == 'win32job' and n.func.attr == 'CreateJobObject']
    assert len(calls) == 1 and not calls[0].keywords
    return tuple(ast.literal_eval(arg) for arg in calls[0].args)


def create_job():
    import win32job
    # Execute the exact constructor arguments in the candidate, not a repaired
    # copy in this test. A baseline with None therefore fails natively too.
    return win32job.CreateJobObject(*constructor_args())


def object_name_length(handle):
    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = [('Length', wintypes.USHORT), ('MaximumLength', wintypes.USHORT),
                    ('Buffer', ctypes.c_void_p)]

    query = ctypes.WinDLL('ntdll').NtQueryObject
    query.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                      wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    query.restype = wintypes.LONG
    buffer = ctypes.create_string_buffer(4096)
    needed = wintypes.ULONG()
    # ObjectNameInformation: read-only observation of this exact job handle.
    status = query(int(handle), 1, buffer, len(buffer), ctypes.byref(needed))
    assert status == 0, f'NtQueryObject failed: {status}'
    return ctypes.cast(buffer, ctypes.POINTER(UnicodeString)).contents.Length


def configure(job):
    import win32job
    limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    limits['BasicLimitInformation']['LimitFlags'] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
    observed = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    assert observed['BasicLimitInformation']['LimitFlags'] == win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


def assign(job, process):
    import win32api
    import win32con
    import win32job
    handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
                                 False, process.pid)
    try:
        win32job.AssignProcessToJobObject(job, handle)
    finally:
        win32api.CloseHandle(handle)


def active(job):
    import win32job
    return win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)['ActiveProcesses']


def assert_empty(job):
    deadline = time.monotonic() + 2
    while active(job):
        assert time.monotonic() < deadline, 'owned job retained a process'


def wait_child():
    # Harmless until assignment. If the test host dies before assignment, EOF
    # closes stdin and exits the child; there is no unrelated process kill.
    return subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def stop_job(job):
    import win32job
    win32job.TerminateJobObject(job, 1)
    assert_empty(job)


def reap_child(process):
    if process.poll() is None:
        process.kill()  # Exact owned child, including failed assignment.
    process.communicate(timeout=2)


class JobApi(unittest.TestCase):
    def test_fixture_uses_empty_unicode_name(self):
        self.assertEqual(constructor_args(), (None, ''),
                         'pywin32 311 WCHAR* rejects None; use empty Unicode for a private unnamed job')

    @unittest.skipUnless(os.name == 'nt', 'real Windows API required')
    def test_original_none_is_rejected_by_locked_binding(self):
        import win32job
        self.assertEqual(importlib.metadata.version('pywin32'), '311')
        with self.assertRaisesRegex(TypeError, 'None is not a valid string'):
            win32job.CreateJobObject(None, None)

    @unittest.skipUnless(os.name == 'nt', 'real Windows API required')
    def test_fixture_job_has_no_object_manager_name(self):
        import win32api
        job = create_job()
        try:
            self.assertEqual(object_name_length(job), 0, 'job must be unnamed, not a shared global name')
            self.assertEqual(active(job), 0)
        finally:
            win32api.CloseHandle(job)

    @unittest.skipUnless(os.name == 'nt', 'real Windows API required')
    def test_all_used_apis_terminate_only_the_assigned_private_job(self):
        import win32api
        import win32job
        jobs, processes = [], []
        # ExitStack runs every registered cleanup even if one API fails.
        with ExitStack() as cleanup:
            for _ in range(2):
                job = create_job()
                jobs.append(job)
                cleanup.callback(win32api.CloseHandle, job)
                configure(job)
                self.assertEqual(object_name_length(job), 0)
                process = wait_child()
                processes.append(process)
                cleanup.callback(reap_child, process)
                assign(job, process)
                cleanup.callback(stop_job, job)
                self.assertEqual(active(job), 1)
            win32job.TerminateJobObject(jobs[0], 1)
            processes[0].communicate(timeout=2)
            self.assertEqual(processes[0].returncode, 1)
            assert_empty(jobs[0])
            self.assertIsNone(processes[1].poll(), 'another private job was killed')
            self.assertEqual(active(jobs[1]), 1)


    @unittest.skipUnless(os.name == 'nt', 'real Windows API required')
    def test_kill_on_close_reaps_owned_child(self):
        import win32api
        job = create_job()
        process = None
        try:
            configure(job)
            process = wait_child()
            assign(job, process)
            self.assertEqual(active(job), 1)
            win32api.CloseHandle(job)
            job = None
            process.communicate(timeout=2)
            self.assertIsNotNone(process.returncode)
        finally:
            if job is not None:
                win32api.CloseHandle(job)
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=2)


if __name__ == '__main__':
    suite = (unittest.TestSuite([JobApi('test_fixture_uses_empty_unicode_name')])
             if '--portable' in sys.argv else unittest.defaultTestLoader.loadTestsFromTestCase(JobApi))
    result = unittest.TextTestRunner(verbosity=2, failfast=True).run(suite)
    if os.name == 'nt':
        import win32api
        import win32job
        print(json.dumps({'pywin32': importlib.metadata.version('pywin32'),
                          'binding_origin': win32job.__file__,
                          'docs': {name: getattr(module, name).__doc__ for module, name in [
                              (win32job, 'CreateJobObject'), (win32job, 'QueryInformationJobObject'),
                              (win32job, 'SetInformationJobObject'), (win32job, 'AssignProcessToJobObject'),
                              (win32job, 'TerminateJobObject'), (win32api, 'OpenProcess'),
                              (win32api, 'CloseHandle')]},
                          'tests': result.testsRun, 'failures': len(result.failures),
                          'errors': len(result.errors), 'skips': len(result.skipped)}))
    sys.exit(0 if result.wasSuccessful() else 1)
