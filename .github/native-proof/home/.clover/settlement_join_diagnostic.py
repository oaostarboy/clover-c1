"""Transport-only cold-child FD facts; original audit return/refusal untouched."""
import subprocess
from pathlib import Path

DEST=Path(__file__).resolve().parents[5]/'evidence'
ORIGINAL=subprocess.run
INJECTION=r'''
_original_audit = guard.audit
def _observed_audit(event, args):
    if event == 'open' and args and isinstance(args[0], int):
        try:
            _s = os.fstat(args[0])
            _facts = {'mode': _s.st_mode, 'size': _s.st_size}
        except OSError as _e:
            _facts = {'error': type(_e).__name__}
        _frames = []
        _frame = sys._getframe()
        for _ in range(15):
            if _frame is None:
                break
            _frames.append({'file': Path(_frame.f_code.co_filename).name,
                            'function': _frame.f_code.co_name, 'line': _frame.f_lineno})
            _frame = _frame.f_back
        sys.stderr.write('JOIN_FD_FACT '+json.dumps({'fd': args[0], 'mode': args[1],
            'flags': args[2] if len(args)>2 else None, 'stat': _facts, 'frames': _frames})+'\n')
    return _original_audit(event, args)
guard.audit = _observed_audit
'''

def _run(*args,**kwargs):
    command=kwargs.get('args',args[0] if args else None)
    if (isinstance(command,list) and len(command)>2 and command[1]=='-c'
            and 'selection_auth_reads = guard.auth_reads' in command[2]
            and 'guard.install()' in command[2]):
        changed=list(command)
        changed[2]=changed[2].replace('guard.install()',INJECTION+'\nguard.install()',1)
        if 'args' in kwargs:kwargs['args']=changed
        else:args=(changed,*args[1:])
        result=ORIGINAL(*args,**kwargs)
        (DEST/'cold-child-fd-facts.log').write_text(result.stderr,encoding='utf-8')
        return result
    return ORIGINAL(*args,**kwargs)

def pytest_configure(config):
    subprocess.run=_run

def pytest_unconfigure(config):
    subprocess.run=ORIGINAL
