"""CI-only negative controls. No live-path opens, sockets or permission writes."""
import json
import os
from pathlib import Path
import subprocess


def run(python, source, out):
    home = out / 'safety-home'
    home.mkdir(exist_ok=True)
    env = {'HOME': str(home), 'USERPROFILE': str(home),
           'SYSTEMROOT': os.environ['SYSTEMROOT'], 'PATH': '/usr/bin:/bin',
           'CLOVER_HOME': str(home / 'profile'), 'CLOVER_TEST_ISOLATION': str(home)}
    code = r'''import json,os,pathlib,socket,sys
home=pathlib.Path(sys.argv[1]).resolve()
source=pathlib.Path(sys.argv[2]).resolve()
assert pathlib.Path.home().resolve()==home
before={str(p.relative_to(home)):p.read_bytes().hex() if p.is_file() else '<directory>' for p in home.rglob('*')}
blocked=[]
def audit(event,args):
 if event.startswith('socket.'):
  blocked.append(event);raise AssertionError('denied socket')
 if event=='open' and isinstance(args[0],str):
  mode=args[1];flags=args[2]
  if pathlib.Path(args[0]).name in {'.env','auth.json'} or (isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC)):
   blocked.append('file');raise AssertionError('denied file')
 if event in {'os.mkdir','os.remove','os.rename','os.rmdir','os.symlink','os.link','os.chmod'}:
  blocked.append(event);raise AssertionError('denied mutation')
sys.addaudithook(audit)
import clover_state as state
assert pathlib.Path(state.__file__).resolve()==source/'clover_state.py'
assert state.get_clover_home().resolve()==home/'profile'
assert state._running_under_pytest()
assert not blocked, blocked
# Constructor must refuse BEFORE any access to the actual home database.
try:
 state.SessionDB(db_path=state._real_platform_state_root()/'state.db')
except RuntimeError as e:
 assert 'live-system guard' in str(e)
else:raise AssertionError('production DB was not refused')
assert not blocked, blocked
for action in (socket.socket, lambda:socket.getaddrinfo('example.invalid',443),
               lambda:open(home/'auth.json'),lambda:open(home/'denied-write','w')):
 try:action()
 except AssertionError:pass
 else:raise AssertionError('audit accepted forbidden operation')
assert len(blocked)==4,blocked
after={str(p.relative_to(home)):p.read_bytes().hex() if p.is_file() else '<directory>' for p in home.rglob('*')}
assert before==after
print(json.dumps(dict(home_is_private=True,source_origin_exact=True,test_isolation_armed=True,
                     production_db_refused_before_access=True,import_socket_events=0,
                     denied=blocked,inventory_unchanged=True)))
'''
    proc = subprocess.run([str(python), '-B', '-c', code, str(home), str(source)],
                          cwd=source, env=env, text=True, capture_output=True, timeout=30)
    data = dict(command=[str(python), '-B', '-c', '<preserved safety_probe.py>', str(home), str(source)],
                env_keys=sorted(env), returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
    (out / 'safety-probes.json').write_text(json.dumps(data, indent=2))
    return proc.returncode
