"""Bounded disposable cold/warm CIM experiment, NOT native acceptance."""
import argparse
import ast
from datetime import datetime
import json
import ntpath
import os
from pathlib import Path
import subprocess
import sys
import time

p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--first',choices=['full','filtered'],required=True);args=p.parse_args()
assert os.name=='nt' and os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
assert sys.version_info[:3]==(3,11,9) and os.environ.get('PSModulePath')
args.evidence.mkdir(parents=True,exist_ok=True)
s=ast.parse((args.source/'clover_cli/windows_update_receipt_handoff.py').read_text(encoding='utf-8'))
f=next(n for n in s.body if isinstance(n,ast.FunctionDef) and n.name=='process_chain')
n=next(n for n in f.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='command' for t in n.targets))
assert isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Attribute) and isinstance(n.value.func.value,ast.Constant)
raw=n.value.func.value.value
assert isinstance(raw,str)
assert raw.count('$all = @(Get-CimInstance Win32_Process)')==1
assert raw.count('$item = @($all | Where-Object { $_.ProcessId -eq $cursor })')==1
powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
rows=[]
child=subprocess.Popen([sys.executable,'-c','import sys; sys.stdin.buffer.read()'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
    for mode in [args.first,'filtered' if args.first=='full' else 'full',args.first]:
        command=raw.replace('CURRENT_PID',str(child.pid))
        if mode=='filtered':
            command=command.replace('$all = @(Get-CimInstance Win32_Process)','')
            command=command.replace('$item = @($all | Where-Object { $_.ProcessId -eq $cursor })', '$item = @(Get-CimInstance Win32_Process -Filter "ProcessId = $cursor" -Property ProcessId,ParentProcessId,ExecutablePath,CreationDate)')
        command=command.replace("$ErrorActionPreference = 'Stop'", "$ErrorActionPreference = 'Stop'\n[Console]::Error.WriteLine('CIM_PROBE_PS_START')")
        command=command.replace('ConvertTo-Json -InputObject $chain', "[Console]::Error.WriteLine('CIM_PROBE_QUERY_DONE')\nConvertTo-Json -InputObject $chain")
        began=time.monotonic();row=dict(mode=mode,cold_order=len(rows),query_budget_seconds=5,verified=False)
        try:
            result=subprocess.run([str(powershell),'-NoProfile','-NonInteractive','-Command',command],stdin=subprocess.DEVNULL,capture_output=True,text=True,encoding='utf-8-sig',errors='strict',timeout=5,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
            chain=json.loads(result.stdout);assert isinstance(chain,list) and 0<len(chain)<=8
            first=chain[0];assert first['pid']==child.pid and first['parent_pid']==os.getpid()
            assert ntpath.normcase(first['executable'])==ntpath.normcase(sys.executable)
            assert datetime.fromisoformat(first['created_utc']).tzinfo is not None
            row.update(verified=True,chain=chain,phases=[l for l in result.stderr.splitlines() if l.startswith('CIM_PROBE_')])
        except subprocess.TimeoutExpired as exc:
            stderr=exc.stderr or b''
            if isinstance(stderr,bytes):stderr=stderr.decode('utf-8-sig',errors='replace')
            row.update(error='TimeoutExpired',phases=[l for l in stderr.splitlines() if l.startswith('CIM_PROBE_')])
        except Exception as exc:
            row['error']=type(exc).__name__
        row['elapsed_seconds']=time.monotonic()-began
        rows.append(row)
        (args.evidence/'samples.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
finally:
    if child.stdin:child.stdin.close()
    try:child.wait(timeout=3)
    except subprocess.TimeoutExpired:child.kill();child.wait(timeout=3)
(args.evidence/'SUMMARY.json').write_text(json.dumps(dict(experiment_complete=len(rows)==3,scope='diagnostic only; never release acceptance',first=args.first,rows=rows),indent=2),encoding='utf-8')
print(json.dumps(dict(experiment_complete=len(rows)==3,first=args.first,outcomes=[{k:v for k,v in r.items() if k!='chain'} for r in rows])))
