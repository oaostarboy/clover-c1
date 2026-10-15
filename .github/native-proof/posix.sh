#!/usr/bin/env bash
# Native disposable-runner adaptation of the historical POSIX/macOS proofs.
set -euo pipefail
[[ ${GITHUB_ACTIONS:-} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted ]]
MODE=$1
PROOF="$GITHUB_WORKSPACE/helpers/.github/native-proof/proof.py"
SOURCE="$GITHUB_WORKSPACE/source"
EVIDENCE="$RUNNER_TEMP/evidence"
mkdir -p "$EVIDENCE" "$RUNNER_TEMP/neutral"
python "$PROOF" preflight --repo "$SOURCE" --output "$EVIDENCE/plan.json"
unset PYTHONPATH VIRTUAL_ENV
export CLOVER_HOME="$HOME/.clover" CLOVER_INSTALL_DIR="$HOME/.clover/clover-c1"
INSTALL_DIR="$CLOVER_INSTALL_DIR"
PY="$INSTALL_DIR/venv/bin/python"
BIN="$INSTALL_DIR/venv/bin/clover"
# A fresh runner is mandatory. Never adopt or replace an existing install.
test ! -e "$CLOVER_HOME"
if command -v clover; then exit 1; fi
FROM_SHA=$TARGET_REF
TO_SHA=$TARGET_REF
TO_TREE=$TARGET_TREE
if [[ $MODE == old2new ]]; then FROM_SHA=$OLD_SHA; fi
if [[ $MODE == new2next ]]; then TO_SHA=$NEXT_SHA; TO_TREE=$NEXT_TREE; fi
# Preserve diagnostics even when set -e stops before the first identity probe.
# The installer and its npm arguments remain unchanged; no retry/cache repair.
collect_install_evidence() {
  python - "$1" "$EVIDENCE" "$INSTALL_DIR" "${INSTALL_EXIT:-unknown}" "${INSTALL_PIPESTATUS:-}" "${1:+$FROM_SHA}" <<'PY'
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

phase, evidence, install, installer_exit, pipeline, from_sha = sys.argv[1:]
evidence, install = Path(evidence), Path(install)
home = Path(os.environ['HOME'])
runner = Path(os.environ['RUNNER_TEMP'])
workspace = Path(os.environ['GITHUB_WORKSPACE'])
ended_ns = time.time_ns()
# Import only the reviewed helper's stdlib redactor, never the installed product.
os.environ['CLOVER_REDACT_SECRETS'] = 'true'
sys.path.insert(0, str(workspace / 'helpers'))
from agent.redact import redact_sensitive_text

LIMIT = 65536
MAX_LOGS = 8
aliases = [(str(install), '$INSTALL_DIR'), (str(home), '$HOME'),
           (str(runner), '$RUNNER_TEMP'), (str(workspace), '$WORKSPACE')]
# Cache paths may be custom paths outside HOME. Do not publish their parents.
cache_env = os.environ.get('npm_config_cache') or os.environ.get('NPM_CONFIG_CACHE')
if cache_env:
    aliases.append((cache_env, '$NPM_CACHE'))

def safe(text):
    text = redact_sensitive_text(str(text), force=True, redact_url_credentials=True)
    # npmrc-style auth assignments can contain opaque tokens without a prefix.
    text = re.sub(r'(?im)((?:_authToken|_auth|_password)\s*[=:]\s*)\S+',
                  r'\1[REDACTED]', text)
    for path, alias in sorted(aliases, key=lambda pair: len(pair[0]), reverse=True):
        text = text.replace(path, alias)
    return text

def run(argv, env):
    try:
        result = subprocess.run(argv, cwd=runner / 'neutral', env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=5)
        return {'exit': result.returncode,
                'output': result.stdout[:4096].decode('utf-8', errors='replace').strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'exit': None, 'output': type(exc).__name__}

def snapshot():
    env = dict(os.environ)
    managed = home / '.clover/node/bin'
    if (managed / 'node').is_file() and (managed / 'npm').is_file():
        env['PATH'] = str(managed) + os.pathsep + env['PATH']
    result = {'topology': {key: (install / path).exists() for key, path in [
        ('package_json', 'package.json'), ('root_node_modules', 'node_modules'),
        ('root_hidden_lock', 'node_modules/.package-lock.json'),
        ('tui_node_modules', 'ui-tui/node_modules'), ('root_lock', 'package-lock.json')]}}
    for tool in ['node', 'npm']:
        binary = shutil.which(tool, path=env['PATH'])
        result[tool] = {'path': binary, **run([binary, '--version'], env)} if binary else {
            'path': None, 'exit': None, 'output': 'not found'}
    caches = {str(home / '.npm')}
    if cache_env:
        caches.add(cache_env)
    npm = result['npm']['path']
    logs_dirs = set()
    if npm:
        # Allowlist two non-secret config keys; never dump npm config or env.
        for key in ['cache', 'logs-dir']:
            query = run([npm, 'config', 'get', key], env)
            result[key] = query
            if query['exit'] == 0 and query['output'] not in ['', 'null', 'undefined']:
                if key == 'cache':
                    caches.add(query['output'])
                else:
                    logs_dirs.add(query['output'])
    logs_dirs.update(str(Path(cache) / '_logs') for cache in caches)
    result['log_dirs'] = sorted(logs_dirs)
    return result

def bounded(path):
    # Keep complete boundary lines so truncation cannot expose half a secret.
    with path.open('rb') as handle:
        size = os.fstat(handle.fileno()).st_size
        if size <= LIMIT:
            data = handle.read(LIMIT)
        else:
            marker = b'\n[TRUNCATED: head/tail]\n'
            budget = (LIMIT - len(marker)) // 2
            head = handle.read(budget).rpartition(b'\n')[0]
            handle.seek(-budget, os.SEEK_END)
            tail = handle.read(budget).partition(b'\n')[2]
            data = head + marker + tail
    text = safe(data.decode('utf-8', errors='replace'))
    # Decoding/redaction can expand bytes; cap only AFTER secrets are removed.
    return text.encode('utf-8')[:LIMIT].decode('utf-8', errors='ignore'), size

private = runner / 'install-diagnostics-before.json'
if phase == 'before':
    before = snapshot()
    before['started_ns'] = time.time_ns()
    private.write_text(json.dumps(before))
else:
    before = json.loads(private.read_text())
    after = snapshot()
    dirs = set(before['log_dirs']) | set(after['log_dirs'])
    # Only logs written during installation, not stale logs or collector probes.
    paths = []
    for directory in sorted(dirs):
        root = Path(directory)
        if root.is_dir() and not root.is_symlink():
            for path in root.glob('*debug*.log'):
                if path.is_file() and not path.is_symlink():
                    stat = path.stat()
                    if before['started_ns'] <= stat.st_mtime_ns <= ended_ns:
                        paths.append((stat.st_mtime_ns, path))
    logs = []
    for index, (_, path) in enumerate(sorted(paths, reverse=True)[:MAX_LOGS]):
        text, size = bounded(path)
        filename = f'npm-debug-{index}.log'
        (evidence / filename).write_text(text)
        logs.append({'source': str(path), 'artifact': filename, 'bytes': size,
                     'truncated': size > LIMIT})
    raw = runner / 'native-install-output.log'
    if raw.is_file() and not raw.is_symlink():
        text, size = bounded(raw)
        (evidence / 'install.log').write_text(text)
    else:
        size = 0
    report = {'installer_exit': int(installer_exit) if installer_exit.isdigit() else None,
              'installer_pipeline_exits': [int(code) for code in pipeline.split()],
              'installer_command': ['bash', '$RUNNER_TEMP/install.sh', '--branch',
                  os.environ['NEXT_REF'], '--commit', from_sha, '--non-interactive',
                  '--skip-setup', '--skip-browser', '--skip-computer-use'],
              'npm_command': ['npm', 'install', '--silent'],
              'npm_command_basis': 'pinned installer root dependency function; actual argv/exit in npm debug logs when available',
              'before': before, 'after': after, 'npm_logs': logs,
              'npm_logs_available': len(paths), 'max_logs': MAX_LOGS,
              'log_byte_limit': LIMIT, 'stdout_bytes': size,
              'stdout_truncated': size > LIMIT,
              'window_end_ns': ended_ns}
    (evidence / 'install-diagnostics.json').write_text(safe(json.dumps(report, indent=2)) + '\n')
PY
}
finish_install_evidence() {
  local status=$?
  trap - EXIT
  collect_install_evidence after || printf '%s\n' 'Install diagnostic collection failed' >&2
  exit "$status"
}
INSTALL_EXIT=unknown
INSTALL_PIPESTATUS=''
collect_install_evidence before
trap finish_install_evidence EXIT
git -C "$SOURCE" show "$TARGET_REF:scripts/install.sh" > "$RUNNER_TEMP/install.sh"
set +e
bash "$RUNNER_TEMP/install.sh" --branch "$NEXT_REF" --commit "$FROM_SHA" \
  --non-interactive --skip-setup --skip-browser --skip-computer-use 2>&1 | tee "$RUNNER_TEMP/native-install-output.log" > /dev/null
INSTALL_PIPESTATUS="${PIPESTATUS[*]}"
set -e
INSTALL_EXIT=${INSTALL_PIPESTATUS%% *}
# Match pipefail's rightmost failing status, never mask installer/tee failure.
INSTALL_STATUS=0
for status in $INSTALL_PIPESTATUS; do
  if [[ $status != 0 ]]; then INSTALL_STATUS=$status; fi
done
if [[ $INSTALL_STATUS != 0 ]]; then exit "$INSTALL_STATUS"; fi
collect_install_evidence after
trap - EXIT
cd "$RUNNER_TEMP/neutral"
FROM_TREE=$(git -C "$SOURCE" rev-parse "$FROM_SHA^{tree}")
"$PY" "$PROOF" identity --repo "$INSTALL_DIR" --sha "$FROM_SHA" --tree "$FROM_TREE" --output "$EVIDENCE/before-imports.json"
if [[ $MODE != fresh ]]; then
  # The updater's default main path sees a local bare origin with real objects.
  # Keep this fixture topology explicit in the evidence; it is NOT GitHub main.
  git -C "$INSTALL_DIR" fetch --no-tags "$SOURCE" "$TO_SHA"
  "$PY" "$PROOF" origin --repo "$INSTALL_DIR" --mirror "$RUNNER_TEMP/origin.git" --target "$TO_SHA"
fi
printf '\nAPI_SERVER_ENABLED=true\nAPI_SERVER_KEY=native-proof-dummy-key-123456\nAPI_SERVER_PORT=18791\nAPI_SERVER_HOST=127.0.0.1\n' >> "$CLOVER_HOME/.env"
"$BIN" gateway run > "$EVIDENCE/gateway-launch.log" 2>&1 &
"$PY" "$PROOF" gateway --repo "$INSTALL_DIR" --home "$CLOVER_HOME" --sha "$FROM_SHA" --port 18791 --seconds 0 --output "$EVIDENCE/before-state.json"
if [[ $MODE != fresh ]]; then
  "$PY" "$PROOF" begin --output "$EVIDENCE/begin.json"
  # This is gateway-mode CLI, not a claimed chat/Telegram transport test.
  "$BIN" update --yes --gateway 2>&1 | tee "$EVIDENCE/update.log"
  "$PY" "$PROOF" gateway --repo "$INSTALL_DIR" --home "$CLOVER_HOME" --sha "$TO_SHA" --port 18791 --seconds 90 --previous "$EVIDENCE/before-state.json" --output "$EVIDENCE/after-state.json"
  "$PY" "$PROOF" receipt --home "$CLOVER_HOME" --begin "$EVIDENCE/begin.json" --old "$FROM_SHA" --target "$TO_SHA" --output "$EVIDENCE/update-receipt.json"
else
  "$PY" "$PROOF" gateway --repo "$INSTALL_DIR" --home "$CLOVER_HOME" --sha "$TO_SHA" --port 18791 --seconds 90 --output "$EVIDENCE/fresh-state.json"
fi
"$PY" "$PROOF" identity --repo "$INSTALL_DIR" --sha "$TO_SHA" --tree "$TO_TREE" --output "$EVIDENCE/after-imports.json"
# Native feature checks execute separately, after the actual installed-source
# identity probe, in the installed interpreter. Existing guards remain active.
if [[ $MODE == fresh ]]; then
  "$PY" "$GITHUB_WORKSPACE/helpers/.github/native-proof/native_tests.py" --repo "$INSTALL_DIR" --evidence "$EVIDENCE"
fi
