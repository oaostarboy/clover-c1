"""Read-only native PowerShell module boundary diagnostic; no installer or policies changed."""
import json
import os
from pathlib import Path
import shutil
import subprocess

SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$r = [ordered]@{version=$PSVersionTable.PSVersion.ToString(); edition=$PSVersionTable.PSEdition; home=$PSHOME; module_path=$env:PSModulePath; executable=(Get-Process -Id $PID).Path}
$r.available = @(Get-Module -ListAvailable Microsoft.PowerShell.Security | ForEach-Object { [ordered]@{path=$_.Path; version=$_.Version.ToString(); compatible=$_.CompatiblePSEditions} })
try { Import-Module Microsoft.PowerShell.Security -ErrorAction Stop; $r.policy=(Get-ExecutionPolicy).ToString(); $r.loaded=@(Get-Module Microsoft.PowerShell.Security | ForEach-Object {$_.Path}); $r.ok=$true }
catch { $r.ok=$false; $r.error=$_.ToString(); $r.exception=$_.Exception.ToString(); $r.error_id=$_.FullyQualifiedErrorId }
$r | ConvertTo-Json -Depth 8 -Compress
if (-not $r.ok) { exit 17 }
'''

def main():
    output = Path(os.environ['RUNNER_TEMP']) / 'module-diagnose'
    output.mkdir()
    rows=[]
    for label, remove, extra in [('inherited', (), []), ('no-module-path', ('PSModulePath',), []), ('no-env-pshome', ('PSHOME',), []), ('no-profile', (), ['-NoProfile'])]:
        env=dict(os.environ)
        for key in list(env):
            if key.upper() in {value.upper() for value in remove}:
                env.pop(key)
        p=subprocess.run(['powershell', *extra, '-c', SCRIPT], env=env, capture_output=True, text=True, timeout=45)
        rows.append(dict(label=label, executable=shutil.which('powershell'), removed=list(remove), returncode=p.returncode, stdout=p.stdout, stderr=p.stderr))
    (output/'result.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
    print(json.dumps(rows,indent=2))

if __name__=='__main__': main()
