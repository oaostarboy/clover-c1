"""Portable controller fixtures, not Windows acceptance or genuine case results."""
import importlib.util
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

SCRIPT = Path(__file__).with_name('native_tests.py')


class ReportBatchContract(unittest.TestCase):
    def exercise(self, *, bad=False, exhaust=False):
        spec = importlib.util.spec_from_file_location('report_batch_contract', SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        method = getattr(module, 'run_report_only_batches', None)
        self.assertTrue(callable(method), 'bounded native report batching is missing')
        assert callable(method)
        clock = [0.0]
        reads = [0]
        lock = threading.Lock()
        calls = []
        def monotonic():
            with lock:
                reads[0] += 1
                return (0 if reads[0] == 1 else 301) if exhaust else clock[0]
        def run(command, **kwargs):
            with lock:
                index = len(calls)
                calls.append((command, kwargs['timeout']))
            path = Path(next(a.split('=', 1)[1] for a in command if a.startswith('--junitxml=')))
            suite = ET.Element('testsuite', tests=str([12, 12, 11][index]), failures='0', errors='0', skipped='0')
            for i in range([12, 12, 11][index]):
                case = ET.SubElement(suite, 'testcase', classname='controller_fixture_only', name=f'part-{index}-case-{i}')
                if bad and index == 1 and i == 0:
                    ET.SubElement(case, 'failure').text = 'intentional controller fixture failure'
                    suite.set('failures', '1')
            ET.ElementTree(suite).write(path, encoding='utf-8', xml_declaration=True)
            with lock:
                clock[0] += 100
            return SimpleNamespace(returncode=1 if bad and index == 1 else 0)
        with tempfile.TemporaryDirectory(prefix='report batch controller ') as directory:
            with mock.patch.object(module, 'canonical_shell', return_value='bash'), \
                 mock.patch.object(module, 'time', SimpleNamespace(monotonic=monotonic)), \
                 mock.patch.object(module.subprocess, 'run', side_effect=run):
                result = method(Path('/fixture-repo'), Path(directory), {})
                assert isinstance(result, dict)
            cases = list(ET.parse(Path(directory)/'report-only.xml').getroot().iter('testcase'))
            return result, calls, cases

    def test_all_parts_keep_original_case_ids_once_and_share_the_existing_300s_budget(self):
        result, calls, cases = self.exercise()
        self.assertEqual(result['returncode'], 0)
        self.assertEqual(result['cases'], 35)
        self.assertEqual(len({(c.get('classname'), c.get('name')) for c in cases}), 35)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(0 < timeout <= 300 for command, timeout in calls))
        for command, timeout in calls:
            self.assertEqual(command[command.index('--file-retries')+1], '0')
            self.assertNotIn('--timeout', command)

    def test_failed_part_is_not_retried_or_converted_to_success(self):
        result, calls, cases = self.exercise(bad=True)
        self.assertNotEqual(result['returncode'], 0)
        self.assertEqual(len(calls), 3)
        self.assertTrue(any(c.find('failure') is not None for c in cases))

    def test_no_new_part_starts_after_the_shared_budget_expires(self):
        result, calls, cases = self.exercise(exhaust=True)
        self.assertNotEqual(result['returncode'], 0)
        self.assertEqual(len(calls), 0)
        self.assertLess(len(cases), 35)
