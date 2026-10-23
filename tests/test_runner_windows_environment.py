"""Canonical runner keeps Windows module locations, never credential variables."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import pytest


@pytest.mark.parametrize('spelling,value', [
    ('PSModulePath', r'C:\Windows\System32\WindowsPowerShell\v1.0\Modules;C:\Program Files\PowerShell\7\Modules'),
    ('PSMODULEPATH', r'C:\Windows\System32\WindowsPowerShell\v1.0\Modules;C:\Program Files\PowerShell\7\Modules'),
    ('PSModuleAnalysisCachePath', r'C:\PowerShellCache\ModuleAnalysisCache'),
    ('PSMODULEANALYSISCACHEPATH', r'C:\PowerShellCache\ModuleAnalysisCache'),
])
def test_canonical_runner_keeps_powershell_module_locations_without_credentials(tmp_path, spelling, value):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('native_env_contract', root / '.github/native-proof/native_tests.py')
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    canonical_name = 'PSModulePath' if spelling.upper() == 'PSMODULEPATH' else 'PSModuleAnalysisCachePath'
    # Keep the nested probe in the repository's guarded test scope, and prove
    # its single case actually executed rather than trusting exit status alone.
    with tempfile.TemporaryDirectory(prefix='_runner-env-probe-', dir=root / 'tests') as directory:
        probe = Path(directory) / 'test_forwarded_module_location.py'
        probe.write_text('import os\ndef test_forwarded_location():\n'
            '    assert (os.environ.get(' + repr(canonical_name) + ') or os.environ.get(' + repr(canonical_name.upper()) + ')) == ' + repr(value) + '\n'
            '    assert os.environ.get("OPENAI_API_KEY") is None\n', encoding='utf-8')
        # Explicitly inert credential sentinel, never a real key.
        env = {k: v for k, v in os.environ.items() if k.casefold() not in {'psmodulepath', 'psmoduleanalysiscachepath', 'openai_api_key'}}
        env.update({spelling: value, 'OPENAI_API_KEY': 'not-a-real-key-runner-contract', 'CLOVER_PYTHON': sys.executable})
        xml = tmp_path / 'nested.xml'
        command = [runner.canonical_shell(), (root / 'scripts/run_tests.sh').as_posix(), probe.relative_to(root).as_posix(),
            '-j', '1', '--file-retries', '0', '--file-timeout', '60', '-o', 'addopts=', '--junitxml=' + xml.as_posix()]
        result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        cases = list(ET.parse(xml).getroot().iter('testcase'))
        assert len(cases) == 1 and cases[0].get('name') == 'test_forwarded_location'
        assert all(cases[0].find(tag) is None for tag in ['failure', 'error', 'skipped'])
