"""Read actual installed macOS components; never repair or skip missing pieces."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

root = Path.home() / '.clover/clover-c1'
evidence = Path(os.environ['RUNNER_TEMP']) / 'evidence'
report = {'product_sha': subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip(),
          'full_gui_acceptance': 'NOT_EXERCISED: no interactive user-session/permission-flow verification',
          'components': {}}

def command(label, argv, timeout=30, required=True, env=None):
    try:
        p = subprocess.run(argv, cwd=root, env=env, text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
        result = {'argv': argv, 'exit': p.returncode, 'output': p.stdout[-4000:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = {'exit': None, 'error': type(exc).__name__}
    report['components'][label] = dict(required=required, **result)
    return result.get('exit') == 0

failed = False
command('cli', [str(root / 'venv/bin/clover'), '--version']) or (failed := True)
node = shutil.which('node')
command('tui', [node, '--input-type=module', '-e', "await import('ink'); await import('react'); console.log('tui imports OK')"]) or (failed := True)
command('playwright', [node, '--input-type=module', '-e', "import fs from 'node:fs'; import {chromium} from 'playwright'; if (!fs.existsSync(chromium.executablePath())) throw Error('Chromium missing'); const b=await chromium.launch({headless:true});const p=await b.newPage();await p.setContent('<h1>native-browser-ok</h1>');console.log(await p.textContent('h1'));await b.close();"], timeout=60) or (failed := True)
command('browser-use', [str(root.parent / 'bin/browser-use'), '--version'], timeout=30) or (failed := True)
cua = shutil.which('cua-driver')
command('cua-driver', [cua or 'cua-driver', '--version'], required=True) or (failed := True)
# This executes the real verified binary but is not a graphical application test.
command('electron', [node, '-e', "const cp=require('child_process'); const exe=require('electron'); console.log(cp.execFileSync(exe,['-p','JSON.stringify(process.versions)'],{env:{...process.env,ELECTRON_RUN_AS_NODE:'1'},encoding:'utf8'}))"]) or (failed := True)
apps = list((root / 'apps/desktop/release').glob('**/Clover.app/Contents/MacOS/Clover'))
report['packaged_apps'] = [{'path': str(p.relative_to(root)), 'bytes': p.stat().st_size,
                          'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in apps]
if not apps:
    failed = True
for path in ['apps/desktop/dist/index.html', 'apps/desktop/dist/electron-main.mjs']:
    report['components'][path] = {'exists': (root / path).is_file(), 'required': True}
    if not (root / path).is_file():
        failed = True
report['status'] = 'FAIL' if failed else 'PASS_INSTALLED_COMPONENTS_NOT_FULL_GUI'
(evidence / 'installed-components.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
raise SystemExit(1 if failed else 0)
