"""Native transport lifecycle regressions, not product/onboarding acceptance.

Every case uses the locked explicit native backend and original private-job,
20/15/35s budgets through run_console. Never print returned console data.
Controlled child delays force a read to be pending at quiet exit/trailing output;
they are regression stimuli, not extra deadline or retry allowances.
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest

import windows_console


class NativeConsoleTransport(unittest.TestCase):
    def exercise(self, mode):
        command = [sys.executable, '-c', '''import msvcrt,sys,time
print('Telegram confirmation code', flush=True)
while msvcrt.getwch() != '\\r':
    pass
mode = sys.argv[1]
if mode == 'quiet-exit':
    time.sleep(.3)
elif mode == 'delayed-tail':
    time.sleep(.3)
    print('NATIVE-TAIL-\\u20ac', flush=True)
    time.sleep(.3)
elif mode == 'cached-tail':
    sys.stdout.write('X' * 10000 + 'NATIVE-TAIL-\\u20ac')
    sys.stdout.flush()
''', mode]
        with tempfile.TemporaryDirectory(prefix='owner-console-transport-') as directory:
            # Harmless control input, not a Telegram proof or credential.
            value = 'native-fixture-control'
            transcript = windows_console.run_console(command, directory, dict(os.environ), value, False)
            assert value not in transcript, 'native control input echoed'
            assert 'Telegram confirmation code' in transcript, 'native control prompt missing'
            if mode != 'quiet-exit':
                assert 'NATIVE-TAIL-\u20ac' in transcript, 'native trailing output missing'
            if mode == 'cached-tail':
                assert transcript.count('X') == 10000, 'native cache output truncated'

    def test_quiet_exit_with_read_already_pending(self):
        self.exercise('quiet-exit')

    def test_delayed_trailing_output_is_fully_drained(self):
        self.exercise('delayed-tail')

    def test_native_cache_chunks_survive_process_exit(self):
        self.exercise('cached-tail')


if __name__ == '__main__':
    assert os.name == 'nt', 'native transport controls require Windows'
    unittest.main(verbosity=2)
