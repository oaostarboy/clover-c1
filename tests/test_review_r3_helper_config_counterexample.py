"""Read-only finding probe: permitted helper must not execute configured code.

The only configured payload writes a sentinel inside this review's scratch.
No package install, network, service operation or live state is involved.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'tests/clover_cli/_inert_venv_holder.py'
REVIEW = ROOT.parent


@pytest.mark.parametrize('mode,tail', [
    ('scan', []),
    ('parent-scan', ['-m', 'clover_cli.main', 'gateway', 'run']),
])
def test_fixed_helper_cannot_execute_configured_command(mode, tail):
    home = Path(os.environ['CLOVER_HOME'])
    sentinel = REVIEW / ('forbidden-config-command-' + mode + '.txt')
    sentinel.unlink(missing_ok=True)
    # Synthetically configured negative control, NOT a native result/receipt.
    payload = 'from pathlib import Path; Path(' + repr(str(sentinel)) + ').write_text("CONFIGURED_CODE_EXECUTED\\n")'
    command = shlex.join([sys.executable, '-I', '-c', payload])
    config = {'secrets': {'command': {'enabled': True, 'command': command}}}
    config_path = home / 'config.yaml'
    original = config_path.read_bytes() if config_path.exists() else None
    config_path.write_text(json.dumps(config))  # JSON is a YAML subset.
    try:
        options = {
            'cwd': str(ROOT),
            'env': {k: os.environ[k] for k in (
                'SYSTEMROOT', 'SystemRoot', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP',
                'TMPDIR', 'HOME', 'USERPROFILE', 'LANG', 'TZ', 'CLOVER_HOME',
                'CLOVER_TEST_ISOLATION',
            ) if k in os.environ},
            'shell': False, 'close_fds': True, 'stdin': subprocess.DEVNULL,
            'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE, 'text': True,
        }
        argv = [sys.executable, '-I', '-B', str(HELPER), mode, *tail]
        proc = subprocess.run(argv, timeout=20, **options)
        evidence = {'mode':mode, 'argv':argv, 'options':{k:v for k,v in options.items() if k != 'env'},
                    'env':options['env'], 'returncode':proc.returncode, 'stdout':proc.stdout,
                    'stderr':proc.stderr, 'scratch_config':str(config_path),
                    'synthetic_command':command, 'sentinel':str(sentinel),
                    'sentinel_exists':sentinel.exists(),
                    'sentinel_content':sentinel.read_text() if sentinel.exists() else None}
        (REVIEW / ('configured-command-counterexample-' + mode + '.json')).write_text(json.dumps(evidence,indent=2))
        assert proc.returncode == 0, proc.stderr
        assert not sentinel.exists(), 'Pinned supposedly inert helper executed arbitrary config command'
    finally:
        if original is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_bytes(original)
