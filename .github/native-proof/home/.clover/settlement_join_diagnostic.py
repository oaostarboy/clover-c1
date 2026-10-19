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

def _pipe_query_trace(frame, event, arg):
    if (frame.f_code.co_name != '_windows_readonly_pipe'
            or Path(frame.f_code.co_filename).name != 'presence_eval_guard.py'):
        return None
    if event == 'exception':
        sys.stderr.write('JOIN_PIPE_QUERY '+json.dumps({'event':event,'error_type':arg[0].__name__})+'\n')
    if event == 'return':
        facts={'event':event,'accepted':arg,'line':frame.f_lineno}
        # Deliberate finite native status/access fields only; no arbitrary locals.
        values=frame.f_locals
        if isinstance(values.get('status'),int):facts['status']=values['status']
        if 'returned' in values:facts['returned_bytes']=values['returned'].value
        if 'info' in values:facts['granted_access']=values['info'].GrantedAccess
        sys.stderr.write('JOIN_PIPE_QUERY '+json.dumps(facts)+'\n')
    return _pipe_query_trace
sys.settrace(_pipe_query_trace)
'''

def _run(*args,**kwargs):
    command=kwargs.get('args',args[0] if args else None)
    if (isinstance(command,list) and len(command)>2 and command[1]=='-c'
            and 'from scripts.presence_eval_guard import ReadOnlyEvaluationGuard' in command[2]
            and 'guard.install()' in command[2]):
        changed=list(command)
        changed[2]=changed[2].replace('guard.install()',INJECTION+'\nguard.install()',1)
        if 'args' in kwargs:kwargs['args']=changed
        else:args=(changed,*args[1:])
        result=ORIGINAL(*args,**kwargs)
        with (DEST/'cold-child-fd-facts.log').open('a',encoding='utf-8') as log:
            log.write('\n'.join(line for line in result.stderr.splitlines() if line.startswith('JOIN_'))+'\n')
        return result
    return ORIGINAL(*args,**kwargs)

def pytest_configure(config):
    subprocess.run=_run

def pytest_unconfigure(config):
    subprocess.run=ORIGINAL
