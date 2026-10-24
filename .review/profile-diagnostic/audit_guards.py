"""Read-only guard audit for the bounded diagnostic delta."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.review/profile-diagnostic'
PROOF = '.github/owner-worker-proof/'
BASE = '8ab74772c5187f74600994dcd31059b5b998b4a3'
old = subprocess.check_output(['git', 'show', BASE + ':' + PROOF + 'disposable_account.ps1'], cwd=ROOT, text=True)
new = (ROOT / PROOF / 'disposable_account.ps1').read_text()
checks = {}
def same(label, start, end):
    checks[label] = old.split(start, 1)[1].split(end, 1)[0] == new.split(start, 1)[1].split(end, 1)[0]
same('hosted_windows_fence', '# No account operations', '$runner =')
same('private_acl_function', 'function Set-PrivateDirectory {', '\ntry {')
same('secret_mask_account_flags_membership', '# A fresh random runtime secret', '    # Standard userenv')
same('exact_native_declaration', '    Add-Type -TypeDefinition', "'@")
same('acl_env_credentials_child_guards', '    Set-PrivateDirectory -Path $Evidence', '} finally {\n    # Continue all cleanup')
for line in ["$name = 'clv' + $tag.Substring(0,16)", "$description = 'Clover CI ' + $tag",
             '$hr = [CloverDisposableProfile]::CreateProfile($sid, $name, $buffer, $buffer.Capacity)',
             'if (-not $profileRecord -or $profileRecord.LocalPath -ne $profile -or $profileRecord.Special)',
             "if (-not $profile -or $record.LocalPath -ne $profile -or $record.Special -or $record.Loaded) { throw 'Refuse unrelated or loaded profile removal' }",
             "if ($owned.Description -ne $description -or $owned.Name -ne $name) { throw 'Refuse unrelated user removal' }",
             "if ($ownerAgain.ReturnValue -ne 0 -or $ownerAgain.Sid -ne $sid) { throw 'Owned process identity changed' }"]:
    checks[line] = line in old and line in new
old_throws = re.findall(r"throw '([^']+)'", old)
new_throws = re.findall(r"throw '([^']+)'", new)
checks['all_existing_other_throw_guards_retained'] = sorted(x for x in old_throws if x != 'OS CreateProfile failed') == sorted(new_throws)
pattern = r'@\{ passed=\(\$passed -and \$cleanupPassed\);.*?ConvertTo-Json \| Set-Content \(Join-Path \$Evidence \'RESULT.json\'\) -Encoding utf8'
old_result = re.search(pattern, old, re.S)
new_result = re.search(pattern, new, re.S)
assert old_result is not None and new_result is not None
checks['result_schema_expressions_unchanged'] = re.sub(r'\s+', '', old_result[0]) == re.sub(r'\s+', '', new_result[0])
unchanged = ['account_common.ps1', 'account_child.ps1', 'token_prerequisite.ps1', 'pins.json',
             'plan.py', 'windows_update.py', 'register_startup.ps1', 'test_owner_worker_native.py',
             'test_status_lock_windows_native.py']
identities = {}
for name in unchanged:
    a = subprocess.check_output(['git', 'cat-file', '--filters', BASE + ':' + PROOF + name], cwd=ROOT)
    b = (ROOT / PROOF / name).read_bytes()
    checks['unchanged:' + name] = a == b
    identities[name] = hashlib.sha256(b).hexdigest()
assert all(checks.values()), {k: v for k, v in checks.items() if not v}
pins = json.loads((ROOT / PROOF / 'pins.json').read_text())
assert pins['target'] == '1a606496182ef48eafd78889eea8878165b829ae'
assert pins['target_tree'] == '43825538944a3d836e8a28ad509f39b0eca7710e'
(OUT / 'GUARD-AUDIT.json').write_text(json.dumps(dict(passed=True, checks=checks, unchanged_sha256=identities, product=pins['target'], product_tree=pins['target_tree']), indent=2) + '\n')
print('PASS guard audit:', len(checks), 'checks; native declaration, all original other throws, child boundary, cleanup helper and pins retained')
