"""Read-only differential for the canonical test's native shell timeout."""
import json
import os
from pathlib import Path
import subprocess

SCRIPT = r'''
$ErrorActionPreference='Stop'
Write-Output 'phase-start'
$policy = Get-ExecutionPolicy
Write-Output 'phase-policy'
$module = Get-Module Microsoft.PowerShell.Security
Write-Output 'phase-module'
@{policy=$policy.ToString(); module=$module.Path; home=$PSHOME; edition=$PSVersionTable.PSEdition} | ConvertTo-Json -Compress
Write-Output 'phase-json'
'''


def main():
    out = Path(os.environ['RUNNER_TEMP']) / 'module-diagnose'
    out.mkdir()
    wide={k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'}
    allowed={'PATH','HOME','USERPROFILE','HOMEDRIVE','HOMEPATH','LOCALAPPDATA','APPDATA','SYSTEMROOT','TEMP','TMP','PYTHONUTF8','PYTHONIOENCODING'}
    narrow={k:v for k,v in wide.items() if k.upper() in allowed}
    rows=[]
    variants=[('wide',wide,['-ExecutionPolicy','Bypass']),('canonical-locations',narrow,['-ExecutionPolicy','Bypass']),('canonical-no-profile',narrow,['-NoProfile','-ExecutionPolicy','Bypass'])]
    groups = {
        'module-cache': {'PSMODULEANALYSISCACHEPATH'},
        'powershell-host': {'POWERSHELL_DISTRIBUTION_CHANNEL','POWERSHELL_UPDATECHECK'},
        'dotnet-host': {'DOTNET_MULTILEVEL_LOOKUP','DOTNET_NOLOGO','DOTNET_SKIP_FIRST_TIME_EXPERIENCE'},
        'executable-extensions': {'PATHEXT'},
    }
    for label, keys in groups.items():
        env = dict(narrow)
        env.update({k:v for k,v in wide.items() if k.upper() in keys})
        variants.append(('canonical-plus-' + label, env, ['-ExecutionPolicy','Bypass']))
    fresh=dict(narrow, PSMODULEANALYSISCACHEPATH=str(out / 'private-module-cache'))
    variants.append(('canonical-private-cache',fresh,['-ExecutionPolicy','Bypass']))
    for label,env,flags in variants:
        row={'label':label,'keys':sorted(env)}
        try:
            p=subprocess.run(['powershell',*flags,'-c',SCRIPT],env=env,stdin=subprocess.DEVNULL,capture_output=True,timeout=12)
            row.update(returncode=p.returncode,stdout=p.stdout.decode(errors='replace'),stderr=p.stderr.decode(errors='replace'))
        except subprocess.TimeoutExpired as exc:
            row.update(timed_out=True,stdout=(exc.stdout or b'').decode(errors='replace'),stderr=(exc.stderr or b'').decode(errors='replace'))
        rows.append(row)
        (out/'result.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
    print(json.dumps(rows,indent=2))

if __name__=='__main__': main()
