"""Real offline npm failure through the POSIX driver; not native macOS proof."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


SCRIPT = Path(__file__).with_name('posix.sh')
ROOT = SCRIPT.parents[2]


def fixture(tmp_path, *, tee_fails=False):
    home = tmp_path / 'private-home'
    home.mkdir()
    runner = tmp_path / 'runner'
    runner.mkdir()
    workspace = tmp_path / 'workspace'
    helper = workspace / 'helpers'
    (helper / '.github/native-proof').mkdir(parents=True)
    (workspace / 'source').mkdir()
    # Use the actual helper redactor, with no product imports/venv installed.
    (helper / 'agent').mkdir()
    for name in ['__init__.py', 'redact.py', 'file_safety.py', 'jiter_preload.py']:
        shutil.copyfile(ROOT / 'agent' / name, helper / 'agent' / name)
    (helper / '.github/native-proof/proof.py').write_text(
        'import pathlib, sys\n'
        'if sys.argv[1] == "identity":\n'
        '    pathlib.Path("IDENTITY_MUST_NOT_RUN").touch()\n'
        '    raise SystemExit(99)\n'
    )
    bins = runner / 'bin'
    bins.mkdir()
    (bins / 'python').symlink_to(sys.executable)
    for name in ['bash', 'sh', 'env', 'mkdir', 'cp', 'tee', 'mktemp', 'rm', 'node', 'npm', 'cat']:
        executable = shutil.which(name)
        assert executable, f'required local contract executable missing: {name}'
        (bins / name).symlink_to(executable)
    # Only source extraction is substituted; the shell, npm and child are real.
    git = bins / 'git'
    git.write_text('#!/bin/sh\n/bin/cat "$FIXTURE_INSTALLER"\n')
    git.chmod(0o755)
    installer = tmp_path / 'fixture-installer.sh'
    installer.write_text('''#!/usr/bin/env bash
set -eu
mkdir -p "$CLOVER_INSTALL_DIR"
cd "$CLOVER_INSTALL_DIR"
printf '%s' '{"name":"offline-evidence-fixture","version":"1.0.0","scripts":{"preinstall":"node -e \\\"process.exit(37)\\\""}}' > package.json
printf 'fixture API_TOKEN=shortsecret in %s\\n' "$HOME"
printf 'https://user:shortpass@example.invalid/path?token=shorttoken\\n'
printf 'do not read this' > "$CLOVER_HOME/.env"
npm_log=$(mktemp)
if npm install --silent --offline > "$npm_log" 2>&1; then
  exit 98
else
  rm -f "$npm_log"
  printf 'npm install failed or timed out; Node.js dependencies were not installed\\n'
  exit 1
fi
''')
    cache = tmp_path / 'cache'
    (cache / '_logs').mkdir(parents=True)
    stale = cache / '_logs/stale-debug-0.log'
    stale.write_text('STALE_LOG_MUST_NOT_BE_COLLECTED\n')
    os.utime(stale, (1, 1))
    if tee_fails:
        tee = bins / 'tee'
        tee.unlink()
        tee.write_text('#!/bin/sh\n/bin/cat > "$1"\nexit 23\n')
        tee.chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if k not in ['PYTHONPATH', 'VIRTUAL_ENV', 'CLOVER_HOME', 'CLOVER_INSTALL_DIR']}
    env.update(HOME=str(home), RUNNER_TEMP=str(runner), GITHUB_WORKSPACE=str(workspace),
               GITHUB_ACTIONS='true', RUNNER_ENVIRONMENT='github-hosted',
               TARGET_REF='b' * 40, TARGET_TREE='c' * 40, OLD_SHA='a' * 40,
               NEXT_REF='ci/native-next/fixture', FIXTURE_INSTALLER=str(installer),
               PATH=str(bins),
               npm_config_cache=str(cache), CLOVER_REDACT_SECRETS='false')
    return env, runner


def test_failed_install_collects_real_npm_exit_before_identity(tmp_path):
    env, runner = fixture(tmp_path)
    proc = subprocess.run(['bash', str(SCRIPT), 'old2new'], env=env, cwd=tmp_path,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=35)
    assert proc.returncode == 1, proc.stdout
    assert 'shortsecret' not in proc.stdout and 'shortpass' not in proc.stdout
    assert not list(tmp_path.rglob('IDENTITY_MUST_NOT_RUN'))
    report_path = runner / 'evidence/install-diagnostics.json'
    assert report_path.exists(), 'failed npm exits before identity without collecting debug evidence'
    report = json.loads(report_path.read_text())
    assert report['installer_exit'] == 1
    assert report['installer_pipeline_exits'] == [1, 0]
    assert report['npm_command'] == ['npm', 'install', '--silent']
    assert report['after']['node']['exit'] == 0
    assert report['after']['npm']['exit'] == 0
    assert report['npm_logs'], report
    text = '\n'.join(p.read_text() for p in (runner / 'evidence').rglob('*') if p.is_file())
    assert 'verbose exit 37' in text
    assert 'STALE_LOG_MUST_NOT_BE_COLLECTED' not in text
    assert 'shortsecret' not in text and 'shortpass' not in text and 'shorttoken' not in text
    assert str(tmp_path) not in text
    assert 'do not read this' not in text
    assert '$HOME' in text
    assert report['before']['topology']['root_node_modules'] is False
    assert report['after']['topology']['package_json'] is True


def test_bounded_logs_drop_partial_secret_lines_and_exclude_symlinks(tmp_path):
    env, runner = fixture(tmp_path)
    installer = Path(env['FIXTURE_INSTALLER'])
    extra = '''
python - <<'PY'
import os
from pathlib import Path
logs = Path(os.environ['npm_config_cache']) / '_logs'
for index in range(12):
    (logs / f'fixture-{index:02}-debug-0.log').write_text(
        'HEAD_KEEP\\n' + 'x\\n' * 30000 + 'npm_' + 'A' * 160000
        + '\\n' + 'x\\n' * 30000 + 'TAIL_KEEP\\n')
(logs / 'symlink-debug-0.log').symlink_to(Path(os.environ['CLOVER_HOME']) / '.env')
print('STDOUT_HEAD')
print('npm_' + 'B' * 160000)
print('STDOUT_TAIL')
PY
'''
    installer.write_text(installer.read_text(encoding='utf-8').replace('  exit 1\n', extra + '  exit 1\n'), encoding='utf-8')
    proc = subprocess.run(['bash', str(SCRIPT), 'old2new'], env=env, cwd=tmp_path,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=35)
    assert proc.returncode == 1, proc.stdout[-1500:]
    report = json.loads((runner / 'evidence/install-diagnostics.json').read_text())
    assert len(report['npm_logs']) == 8
    assert report['npm_logs_available'] == 13
    assert report['stdout_truncated']
    assert all(entry['truncated'] for entry in report['npm_logs'])
    for entry in report['npm_logs']:
        path = runner / 'evidence' / entry['artifact']
        assert path.stat().st_size <= report['log_byte_limit']
        text = path.read_text()
        assert 'HEAD_KEEP' in text and 'TAIL_KEEP' in text
        assert 'AAAAA' not in text
        assert 'do not read this' not in text
    output = (runner / 'evidence/install.log').read_text()
    assert 'STDOUT_HEAD' in output and 'STDOUT_TAIL' in output
    assert 'BBBBB' not in output
    assert len(output.encode()) <= report['log_byte_limit']


def test_tee_failure_keeps_rightmost_pipefail_and_installer_status(tmp_path):
    env, runner = fixture(tmp_path, tee_fails=True)
    proc = subprocess.run(['bash', str(SCRIPT), 'old2new'], env=env, cwd=tmp_path,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=35)
    assert proc.returncode == 23, proc.stdout
    report = json.loads((runner / 'evidence/install-diagnostics.json').read_text())
    assert report['installer_exit'] == 1
    assert report['installer_pipeline_exits'] == [1, 23]
    assert not list(tmp_path.rglob('IDENTITY_MUST_NOT_RUN'))


def test_preinstaller_failure_is_not_fabricated_as_npm_exit(tmp_path):
    env, runner = fixture(tmp_path)
    git = runner / 'bin/git'
    git.write_text('#!/bin/sh\nexit 17\n')
    proc = subprocess.run(['bash', str(SCRIPT), 'old2new'], env=env, cwd=tmp_path,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=35)
    assert proc.returncode == 17, proc.stdout
    report = json.loads((runner / 'evidence/install-diagnostics.json').read_text())
    assert report['installer_exit'] is None
    assert report['installer_pipeline_exits'] == []
    assert report['npm_logs'] == []
    assert report['after']['topology']['package_json'] is False


def test_successful_installer_collects_before_unchanged_identity_rejection(tmp_path):
    env, runner = fixture(tmp_path)
    Path(env['FIXTURE_INSTALLER']).write_text('''#!/usr/bin/env bash
set -eu
python - <<'PY'
import os
from pathlib import Path
import sys
binary = Path(os.environ['CLOVER_INSTALL_DIR']) / 'venv/bin/python'
binary.parent.mkdir(parents=True)
binary.symlink_to(sys.executable)
print('successful fixture installer')
PY
''', encoding='utf-8')
    proc = subprocess.run(['bash', str(SCRIPT), 'old2new'], env=env, cwd=tmp_path,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=35)
    assert proc.returncode == 99, proc.stdout
    assert (runner / 'neutral/IDENTITY_MUST_NOT_RUN').exists()
    report = json.loads((runner / 'evidence/install-diagnostics.json').read_text())
    assert report['installer_exit'] == 0
    assert report['installer_pipeline_exits'] == [0, 0]
    assert report['npm_logs'] == []
    assert 'successful fixture installer' in (runner / 'evidence/install.log').read_text()
