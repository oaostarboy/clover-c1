"""Portable real-plan/workflow/adapter seam, NOT native account or update proof.

Only account commands and the native driver's body are substituted. The actual
workflow request block, driver argument parser/result constructor, adapter run,
and PowerShell parent receipt admission execute without accounts or installers.
"""
import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PINS = json.loads((HERE / 'pins.json').read_text())


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLAN = load('plan').plan(PINS)
ROWS = [row for row in PLAN['matrix']['include'] if row['setup'] == 'zyra']


@pytest.fixture
def workflow_request(tmp_path):
    workflow = yaml.safe_load((ROOT / '.github/workflows/windows-owner-worker-proof.yml').read_text())
    steps = [step for job in workflow['jobs'].values() for step in job.get('steps', [])
             if 'startup-update-request.json' in step.get('run', '')]
    assert len(steps) == 1
    step = steps[0]
    pwsh = shutil.which('pwsh')
    assert pwsh, 'Portable PowerShell execution required; no skipped proof'
    # Execute the request construction/serialization verbatim, not a Python
    # reimplementation of the workflow's field names or entry mapping.
    body = step['run']
    start = body.index('$request = @{')
    end = body.index('\n', body.index('$request | ConvertTo-Json | Set-Content'))
    script = tmp_path / 'workflow-request.ps1'
    script.write_text("$ErrorActionPreference='Stop'\n" + body[start:end] + '\n')

    def build(row):
        contexts = {'matrix.' + key: value for key, value in row.items()}
        contexts.update({'needs.plan.outputs.target': PLAN['target'],
                         'github.workspace': str(tmp_path),
                         'steps.bootstrap-uv.outputs.uv-path': str(tmp_path / 'uv.exe')})
        env = dict(os.environ, GITHUB_WORKSPACE=str(tmp_path), RUNNER_TEMP=str(tmp_path),
                   UPDATE_TARGET=PLAN['target'], UPDATE_TREE=PLAN['target_tree'])
        for key, value in step['env'].items():
            env[key] = re.sub(r'\$\{\{\s*(.*?)\s*\}\}', lambda m: contexts[m[1]], value)
        subprocess.run([pwsh, '-NoLogo', '-NoProfile', '-File', str(script)],
                       env=env, check=True, capture_output=True, text=True, timeout=20)
        return json.loads((tmp_path / 'startup-update-request.json').read_text(encoding='utf-8-sig'))
    return build


def driver_contract_program():
    """Extract executable portable surfaces; never import/run native main()."""
    tree = ast.parse((HERE / 'windows_update.py').read_text())
    entrypoint = tree.body[-1]
    assert isinstance(entrypoint, ast.If)
    parser = []
    for node in entrypoint.body:
        if isinstance(node, ast.Try):
            break
        parser.append(node)
    assert ast.unparse(parser[-1]) == 'args = p.parse_args()'
    receipts = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == 'dump'
                and node.args and isinstance(node.args[0], ast.BinOp)
                and isinstance(node.args[0].right, ast.Constant)
                and node.args[0].right.value == 'result.json'
                and isinstance(node.args[1], ast.Call)
                and any(keyword.arg == 'status' and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value == 'PASS' for keyword in node.args[1].keywords)]
    assert len(receipts) == 1
    # Native outcome values here are test inputs, NOT measured update evidence.
    return ('import argparse, json, os\n' + '\n'.join(map(ast.unparse, parser)) +
            '\na = args\nfirst_transition = "PORTABLE_NOT_EXECUTED"\n'
            'retry = False\nlarge_lane = False\n' +
            'print(json.dumps(dict(arguments=vars(args), result=' +
            ast.unparse(receipts[0].args[1]) + ')))\n')


@pytest.mark.parametrize('row', ROWS, ids=lambda row: row['caller'] + '-' + row['entry'])
def test_actual_workflow_startup_row_is_accepted(row, workflow_request, tmp_path, monkeypatch):
    """Same eight causal RED IDs as independent review, extended past validation."""
    request = workflow_request(row)
    adapter = load('account_update')
    assert adapter.validate_request(request) == request
    for key in ('setup', 'entry', 'turn_mode', 'old'):
        assert request[key] == row[key]
    assert request['candidate'] == request['target'] == PINS['target']
    assert request['tree'] == PINS['target_tree']

    profile = tmp_path / 'profile'
    home = profile / 'AppData/Local/clover'
    home.mkdir(parents=True)
    (home / 'owned-write.txt').write_text('Private non-admin home write control')
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    inputs = dict(request=request, helper_hashes={}, helpers=str(tmp_path / 'historical'))
    for key in ('source_bundle', 'fixture_bundle', 'uv'):
        path = tmp_path / key
        path.write_bytes(b'Portable inert boundary input; never executed')
        inputs[key] = str(path)
        inputs[key + '_sha256'] = adapter.digest(path)
    config = dict(update=inputs, expected_sid='portable-created-sid', profile=str(profile),
                  evidence=str(evidence), python='/portable/machine/python')
    program = driver_contract_program()
    real_run, real_python = subprocess.run, sys.executable
    pwsh = shutil.which('pwsh')
    assert pwsh, 'Portable PowerShell execution required; no skipped proof'
    calls = []

    def native_boundary(argv, **kwargs):
        calls.append(argv)
        assert profile.exists()
        assert kwargs['cwd'] == profile
        assert kwargs['env']['CLOVER_FIXTURE_ACCOUNT_SID'] == config['expected_sid']
        assert kwargs['env']['CLOVER_PROOF_HELPERS'] == inputs['helpers']
        assert 'GITHUB_TOKEN' not in kwargs['env']
        if len(argv) > 2 and Path(argv[2]).name == 'windows_update.py':
            assert not home.exists(), 'prerequisite marker must not reach the installer'
            assert argv[:3] == [str(profile / 'driver-tools/Scripts/python.exe'), '-I',
                               str(profile / 'fixture/.github/owner-worker-proof/windows_update.py')]
            # Run the unchanged driver's actual parser and result constructor in
            # an isolated process. No native guard, installer or updater runs.
            observed = real_run([real_python, '-I', '-c', program, *argv[3:]],
                                env=dict(os.environ, GITHUB_SHA=PINS['target']),
                                check=True, capture_output=True, text=True, timeout=20)
            observed = json.loads(observed.stdout)
            for key in ('setup', 'old', 'candidate', 'target', 'tree', 'entry', 'turn_mode'):
                assert observed['arguments'][key] == request[key]
            for key, value in dict(source=profile / 'source', evidence=evidence / 'update',
                                   uv=inputs['uv']).items():
                assert observed['arguments'][key] == str(value)
            assert '--helpers' not in argv
            (evidence / 'update/result.json').write_text(json.dumps(observed['result']))
        return subprocess.CompletedProcess(argv, 0)

    # Hosted/account gating is simulated only at this portable seam. These
    # tests do not certify Windows credentials, ACLs, account creation or update.
    with monkeypatch.context() as patch:
        patch.setattr(adapter.sys, 'platform', 'win32')
        patch.setattr(adapter.os, 'environ', dict(GITHUB_ACTIONS='true', RUNNER_ENVIRONMENT='github-hosted',
                                                CLOVER_HOME=str(home)))
        patch.setattr(adapter.subprocess, 'run', native_boundary)
        adapter.run(config)
    assert len(calls) == 5
    assert profile.exists(), 'outer SID-bound cleanup alone owns profile removal'
    receipt_path = evidence / 'update-result.json'
    receipt = json.loads(receipt_path.read_text())
    assert receipt['request'] == request
    assert receipt['sid'] == config['expected_sid'] and receipt['profile'] == str(profile)
    adapter.check_result(receipt['driver_result'], request)

    # Real PowerShell parent admission must accept the complete handoff and
    # reject either spelling/direction of entry or a changed turn mode.
    contract = str(HERE / 'account_update_contract.ps1').replace("'", "''")
    receipt_literal = str(receipt_path).replace("'", "''")
    script = tmp_path / 'parent-receipt.ps1'
    script.write_text("$ErrorActionPreference='Stop'\n. '" + contract + "'\n" +
                      "$receipt=Get-Content -Raw -LiteralPath '" + receipt_literal + "' | ConvertFrom-Json\n" + """
$request = $receipt.request | ConvertTo-Json | ConvertFrom-Json
Assert-AccountUpdateResult -Result $receipt -ExpectedSid $receipt.sid -Profile $receipt.profile -Request $request
foreach ($entry in @('cli', 'chat', 'invalid', $(if ($request.entry -eq 'terminal') {'gateway'} else {'terminal'}))) {
    $receipt.driver_result.entry=$entry
    $rejected=$false
    try { Assert-AccountUpdateResult -Result $receipt -ExpectedSid $receipt.sid -Profile $receipt.profile -Request $request }
    catch { $rejected=$true }
    if (-not $rejected) { throw 'Changed driver entry admitted' }
}
$receipt.driver_result.entry=$request.entry
$receipt.driver_result.turn_mode=$(if ($request.turn_mode -eq 'inflight') {'completed'} else {'inflight'})
$rejected=$false
try { Assert-AccountUpdateResult -Result $receipt -ExpectedSid $receipt.sid -Profile $receipt.profile -Request $request }
catch { $rejected=$true }
if (-not $rejected) { throw 'Changed turn mode admitted' }
""")
    real_run([pwsh, '-NoLogo', '-NoProfile', '-File', str(script)],
             check=True, capture_output=True, text=True, timeout=20)
    for entry in ('cli', 'chat', 'invalid', 'gateway' if row['entry'] == 'terminal' else 'terminal'):
        with pytest.raises(ValueError, match='acceptance did not complete'):
            adapter.check_result(dict(receipt['driver_result'], entry=entry), request)


@pytest.mark.parametrize('entry', ['cli', 'chat', 'shell', 'Terminal', 'Gateway', '', None, 1])
def test_adapter_rejects_nonplanner_entries(entry, workflow_request):
    request = workflow_request(ROWS[0])
    with pytest.raises(ValueError):
        load('account_update').validate_request(dict(request, entry=entry))


def test_real_plan_startup_coverage_is_complete():
    assert len(ROWS) == 8
    assert len({(row['caller'], row['entry']) for row in ROWS}) == 8
    assert {row['caller'] for row in ROWS} == {*load('plan').CALLERS, 'candidate'}
    assert {row['entry'] for row in ROWS} == {'terminal', 'gateway'}
    assert {row['turn_mode'] for row in ROWS} == {'completed', 'inflight'}
