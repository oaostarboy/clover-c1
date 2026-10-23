"""Hosted Linux only: real installer, real systemd service, deterministic local LLM/IRC.
No live Telegram/provider credentials. No driver restart after update.
"""
from __future__ import annotations
import http.server
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback

HERE=Path(__file__).resolve().parent
TARGET=os.environ.get('TARGET_REF','')
E=Path(os.environ.get('RUNNER_TEMP','/tmp'))/'evidence'
TOKEN='LINUX_NATIVE_FINAL_REPLY_59312'
EFFECT='LINUX_NATIVE_EFFECT_59312'
CALL='call_linux_native_59312'

def dump(p,v):p.write_text(json.dumps(v,indent=2,default=str)+'\n')
def loadmodule(name):
    spec=importlib.util.spec_from_file_location(name,HERE/(name+'.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def wait(predicate,seconds=180):
    deadline=time.monotonic()+seconds;last=None
    while time.monotonic()<deadline:
        try:
            value=predicate()
            if value:return value
        except AssertionError:raise
        except Exception as exc:last=exc
        time.sleep(1)
    raise TimeoutError(str(last or predicate))

def fixture():
    # Separate base-Python process. The product alone writes effect + transcript.
    lock=threading.Lock();clients=[]
    def audit(name,data):
        with lock:
            with (E/name).open('a') as f:f.write(json.dumps(data)+'\n')
    class Provider(http.server.BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            payload={'object':'list','data':[{'id':'native-test','object':'model','context_length':131072}]}
            raw=json.dumps(payload).encode();self.send_response(200);self.end_headers();self.wfile.write(raw)
        def do_POST(self):
            req=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            messages=req.get('messages',[])
            tool_enabled=any(t.get('function',{}).get('name')=='terminal' for t in req.get('tools',[]))
            target=tool_enabled and any('LINUX_NATIVE_TASK' in str(m.get('content','')) for m in messages)
            tool_done=any(m.get('role')=='tool' and m.get('tool_call_id')==CALL for m in messages)
            audit('provider-events.jsonl',dict(at=time.time(),pid=os.getpid(),path=self.path,target=target,tool_done=tool_done,stream=req.get('stream'),roles=[m.get('role') for m in messages]))
            if target and tool_done:
                (E/'provider-barrier').touch()
                wait(lambda:(E/'release').exists(),2400)
            if target and not tool_done:
                command="printf '%s\\n' "+EFFECT+" | tee -a "+__import__('shlex').quote(str(E/'effects.txt'))
                msg={'role':'assistant','content':None,'tool_calls':[{'id':CALL,'type':'function','function':{'name':'terminal','arguments':json.dumps({'command':command,'timeout':30})}}]}
                finish='tool_calls'
            else:msg={'role':'assistant','content':TOKEN if target else 'OK'};finish='stop'
            base={'id':'chatcmpl-native','object':'chat.completion','created':int(time.time()),'model':'native-test'}
            if req.get('stream'):
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                delta=dict(msg);delta.pop('role',None)
                if 'tool_calls' in delta:delta['tool_calls'][0]['index']=0
                for d,fin in [(dict(role='assistant'),None),(delta,None),({},finish)]:
                    payload=dict(base,object='chat.completion.chunk',choices=[dict(index=0,delta=d,finish_reason=fin)])
                    self.wfile.write(('data: '+json.dumps(payload)+'\n\n').encode());self.wfile.flush()
                self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
            else:
                payload=dict(base,choices=[dict(index=0,message=msg,finish_reason=finish)],usage=dict(prompt_tokens=30,completion_tokens=10,total_tokens=40))
                raw=json.dumps(payload).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    threading.Thread(target=http.server.ThreadingHTTPServer(('127.0.0.1',18792),Provider).serve_forever,daemon=True).start()
    def send(c,line):c['socket'].sendall((line+'\r\n').encode());audit('irc.jsonl',dict(direction='to-product',line=line,at=time.time()))
    def handle(sock):
        c={'socket':sock,'nick':'cloverbot','ready':False};clients.append(c);buf=b''
        try:
            while True:
                data=sock.recv(4096)
                if not data:break
                buf+=data
                while b'\n' in buf:
                    raw,buf=buf.split(b'\n',1);line=raw.decode().rstrip('\r');parts=line.split(' ')
                    audit('irc.jsonl',dict(direction='from-product',line=line,at=time.time()))
                    if parts[0]=='NICK':c['nick']=parts[1]
                    elif parts[0]=='USER':send(c,f":native 001 {c['nick']} :Welcome");c['ready']=True;(E/'irc-ready').touch()
                    elif parts[0]=='PING':send(c,':native PONG native :'+parts[-1].lstrip(':'))
                    elif parts[0]=='JOIN':send(c,f":{c['nick']}!bot@localhost JOIN {parts[1]}")
                    elif parts[0]=='PRIVMSG' and TOKEN in line:
                        with (E/'received.txt').open('a') as f:f.write(TOKEN+'\n')
        finally:
            if c in clients:clients.remove(c)
    def trigger():
        while True:
            time.sleep(.2);p=E/'irc-trigger'
            if p.exists() and any(c['ready'] for c in clients):
                text=p.read_text();p.unlink();c=[c for c in clients if c['ready']][-1]
                send(c,f":ant!ant@localhost PRIVMSG {c['nick']} :{text}")
    threading.Thread(target=trigger,daemon=True).start()
    srv=socket.socket();srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);srv.bind(('127.0.0.1',16667));srv.listen()
    (E/'fixture-ready').touch()
    while True:
        conn,_=srv.accept();threading.Thread(target=handle,args=(conn,),daemon=True).start()

def main():
    assert platform.system()=='Linux' and os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
    E.mkdir(parents=True,exist_ok=True);scratch=Path(os.environ['RUNNER_TEMP']);source=HERE.parents[1]
    home=Path.home()/'.clover';root=home/'clover-c1'
    assert not home.exists() and shutil.which('clover') is None,'fresh hosted runner required'
    env=dict(os.environ,CLOVER_HOME=str(home),CLOVER_INSTALL_DIR=str(root),PYTHONUNBUFFERED='1')
    env.pop('PYTHONPATH',None);env.pop('VIRTUAL_ENV',None)
    mode=os.environ['MODE'];base=os.environ['BASE'];entry=os.environ['ENTRY'];layout=os.environ['LAYOUT']
    commands=[]
    def run(argv,name,check=True,timeout=1800,cwd=scratch):
        print('START '+name,flush=True)
        with (E/(name+'.log')).open('w') as log:
            try:rc=subprocess.run(list(map(str,argv)),env=env,cwd=cwd,stdout=log,stderr=subprocess.STDOUT,timeout=timeout).returncode
            except subprocess.TimeoutExpired:rc=124
        commands.append(dict(name=name,argv=list(map(str,argv)),exit=rc,at=time.time()));dump(E/'commands.json',commands)
        if rc and check:raise RuntimeError(name+' exit='+str(rc))
        return rc
    def git(*a):return subprocess.check_output(['git','-C',str(source),*a],text=True).strip()
    old=TARGET if base=='target' else git('rev-parse',base+'^{commit}')
    assert git('rev-parse',TARGET+'^{tree}')==os.environ['TARGET_TREE']
    dump(E/'pins.json',dict(target=TARGET,tree=os.environ['TARGET_TREE'],old=old,workflow_sha=os.environ['GITHUB_SHA'],run_url='https://github.com/'+os.environ['GITHUB_REPOSITORY']+'/actions/runs/'+os.environ['GITHUB_RUN_ID'],native_os=platform.platform(),os_release=Path('/etc/os-release').read_text(),mode=mode,entry=entry,layout=layout))
    if os.environ.get('PREPARE_RUNNER_APT')=='true':
        # Real official Ubuntu packages, bounded downloads on disposable CI only.
        # The Azure mirror stalled earlier trials before product installation.
        aptconf=E/'apt-acquire.conf'
        aptconf.write_text('Acquire::http::Timeout "30";\nAcquire::https::Timeout "30";\nAcquire::Retries "1";\n')
        run(['sudo','cp',aptconf,'/etc/apt/apt.conf.d/99-native-proof-acquire'],'runner-apt-timeout')
        changed=[]
        for path in [Path('/etc/apt/sources.list'),*Path('/etc/apt/sources.list.d').glob('*.sources')]:
            if not path.exists():continue
            text=path.read_text()
            if 'azure.archive.ubuntu.com' not in text:continue
            replacement=E/('apt-'+path.name)
            replacement.write_text(text.replace('azure.archive.ubuntu.com','archive.ubuntu.com'))
            run(['sudo','cp',replacement,path],'runner-apt-mirror-'+path.name)
            changed.append(str(path))
        dump(E/'runner-package-prerequisite.json',dict(official_mirror='archive.ubuntu.com',changed_sources=changed,network_timeout_seconds=30,retries=1,product_installer_unchanged=True))
    installer=scratch/'installer.sh';installer.write_text(subprocess.check_output(['git','-C',str(source),'show',old+':scripts/install.sh'],text=True))
    run(['bash',installer,'--branch','fix/windows-installer-module-20261022','--commit',old,'--non-interactive','--skip-setup','--skip-browser','--skip-computer-use'],'installer',timeout=2100)
    py=root/'venv/bin/python';uv=home/'bin/uv'
    if layout=='external':
        # Historical managed Python is a real vulnerable SQLite baseline.
        run([uv,'python','install','3.11.14','--no-config'],'historical-python')
        live=scratch/'external-editable';run([uv,'venv',live,'--python','3.11.14','--managed-python','--no-config'],'external-venv')
        run([uv,'pip','install','--python',live/'bin/python','-e',str(root)+'[all]'],'external-editable-install')
        py=live/'bin/python'
    cli=[py.parent/'clover']
    run([py,HERE/'linux_proof.py','identity','--repo',root,'--sha',old,'--tree',git('rev-parse',old+'^{tree}'),'--output',E/'before-imports.json'],'before-identity')
    # Supported CLI config writes, never edit live config files.
    for key,value in [('model.provider','custom'),('model.base_url','http://127.0.0.1:18792/v1'),('model.default','native-test'),('agent.max_iterations','6'),('display.streaming','false')]:
        run([*cli,'config','set',key,value],'config-'+key)
    with (home/'.env').open('a') as f:f.write('\nAPI_SERVER_ENABLED=true\nAPI_SERVER_KEY=native-test-key-not-secret\nAPI_SERVER_PORT=18791\nAPI_SERVER_HOST=127.0.0.1\nOPENAI_API_KEY=native-fixture-dummy\nOPENROUTER_API_KEY=native-fixture-dummy\nIRC_SERVER=127.0.0.1\nIRC_PORT=16667\nIRC_USE_TLS=false\nIRC_CHANNEL=#clover\nIRC_NICKNAME=cloverbot\nIRC_ALLOWED_USERS=ant\nGATEWAY_ALLOW_ALL_USERS=true\n')
    fixture_log=(E/'fixture.log').open('w');fixture_proc=subprocess.Popen([sys.executable,__file__,'fixture'],env=env,stdout=fixture_log,stderr=subprocess.STDOUT)
    wait(lambda:(E/'fixture-ready').exists(),15)
    uid=os.getuid();env['XDG_RUNTIME_DIR']=f'/run/user/{uid}';env['DBUS_SESSION_BUS_ADDRESS']=f'unix:path=/run/user/{uid}/bus'
    run(['sudo','loginctl','enable-linger',os.environ['USER']],'enable-linger')
    run(['sudo','systemctl','start',f'user@{uid}.service'],'user-manager')
    run([*cli,'gateway','install','--start-now','--start-on-login'],'service-install')
    def observe(name,sha,seconds=0,previous=None):
        args=[py,HERE/'linux_proof.py','gateway','--repo',root,'--home',home,'--sha',sha,'--port','18791','--seconds',str(seconds),'--output',E/(name+'.json')]
        if previous:args+=['--previous',E/(previous+'.json')]
        run(args,name,timeout=300)
        state=json.loads((E/(name+'.json')).read_text())
        props=subprocess.check_output(['systemctl','--user','show','clover-gateway.service','--property=MainPID,ActiveState,ExecStart,FragmentPath,ActiveEnterTimestamp'],env=env,text=True)
        (E/(name+'-systemd.txt')).write_text(props)
        parsed=dict(line.split('=',1) for line in props.splitlines() if '=' in line)
        assert parsed['ActiveState']=='active' and int(parsed['MainPID'])==state['pid'],props
        return state
    def runtime_observation(state,label,require_safe=False):
        code="import sqlite3,_sqlite3,sys,json; c=sqlite3.connect(':memory:'); print(json.dumps(dict(sqlite=sqlite3.sqlite_version,source_id=c.execute('select sqlite_source_id()').fetchone()[0],module=getattr(_sqlite3,'__file__',None),module_origin=_sqlite3.__spec__.origin,executable=sys.executable,base_executable=sys._base_executable,prefix=sys.prefix)))"
        data=json.loads(subprocess.check_output([str(py),'-I','-c',code],env=env,cwd=scratch,text=True))
        exe=os.readlink(f"/proc/{state['pid']}/exe")
        maps=Path(f"/proc/{state['pid']}/maps").read_text()
        data['gateway_pid']=state['pid'];data['gateway_executable']=exe
        data['gateway_sqlite_extension_loaded']=bool(data['module'] and data['module'] in maps)
        data['sqlite_statically_built_in']=data['module_origin']=='built-in' and data['module'] is None
        data['gateway_executable_matches_cli_base']=os.path.samefile(exe,data['base_executable'])
        dump(E/(label+'-sqlite-runtime.json'),data)
        assert data['gateway_executable_matches_cli_base'] and (data['gateway_sqlite_extension_loaded'] or data['sqlite_statically_built_in']),data
        if require_safe:
            run([py,'-I','-c','import sqlite3;from clover_cli.sqlite_runtime import is_sqlite_wal_reset_vulnerable as bad;assert not bad(sqlite3.sqlite_version_info)'],label+'-sqlite-safe')
        return data
    before=observe('before',old)
    runtime_observation(before,'before')
    run(['systemctl','--user','cat','clover-gateway.service'],'registered-unit')
    if mode=='fresh':
        observe('fresh-stable',TARGET,90)
        # Candidate's real installed interpreter must select a safe SQLite.
        run([py,'-c','import sqlite3,sys,json;from clover_cli.sqlite_runtime import is_sqlite_wal_reset_vulnerable as bad;print(json.dumps(dict(executable=sys.executable,prefix=sys.prefix,sqlite=sqlite3.sqlite_version)));assert not bad(sqlite3.sqlite_version_info)'],'safe-sqlite')
        dump(E/'result.json',dict(status='PASS_NATIVE_FRESH_REGISTERED_SYSTEMD',tests=4,automatic_updater_restart='NOT_APPLICABLE',target=TARGET));return
    # Real-object local download source, not GitHub/main or a product patch.
    run(['git','-C',root,'fetch','--no-tags',source,TARGET],'candidate-objects')
    run([py,HERE/'linux_proof.py','origin','--repo',root,'--mirror',scratch/'origin.git','--target',TARGET],'isolated-origin')
    (home/'.skip_upstream_prompt').touch()
    wait(lambda:(E/'irc-ready').exists(),60)
    (E/'effects.txt').write_text('');(E/'received.txt').write_text('')
    (E/'irc-trigger').write_text('LINUX_NATIVE_TASK execute the requested terminal tool once and then answer exactly '+TOKEN)
    wait(lambda:(E/'provider-barrier').exists(),180)
    # Barrier is witnessed by a real second provider request with the real tool result.
    # Find actual canonical session, never insert or fabricate witness rows.
    def pending():
        import sqlite3
        with sqlite3.connect((home/'state.db').as_uri()+'?mode=ro',uri=True) as db:
            rows=db.execute('select session_id from messages where role="tool" and tool_call_id=?',(CALL,)).fetchall()
        return rows[0][0] if len(rows)==1 else None
    session=wait(pending,30)
    w=loadmodule('inflight_witness');snapshot=w.capture(home,session,CALL,E/'effects.txt',EFFECT,TOKEN);dump(E/'inflight-before.json',snapshot)
    dump(E/'active-turn-barrier.json',dict(provider_second_request=True,tool_call_id=CALL,session=session,pid=before['pid'],at=time.time()))
    if entry == 'quiet-cli':
        began = loadmodule('quiet_boundary').perform(home=home, cli=cli, py=py,
            env=env, E=E, TOKEN=TOKEN, before=before, run=run, wait=wait)
        dump(E/'begin.json', began)
        updater = None
    else:
        began=time.time();dump(E/'begin.json',began)
        if entry=='cli':
            out=(E/'update.log').open('w');updater=subprocess.Popen(list(map(str,[*cli,'update','--yes'])),env=env,cwd=scratch,stdout=out,stderr=subprocess.STDOUT)
        else:
            (E/'irc-trigger').write_text('/update');updater=None
        # Hold while updater runs. Then release only after the real command started.
        def started():
            p=home/'logs/update_receipts/latest.json'
            if p.exists():
                r=json.loads(p.read_text());return r.get('started_at') and p.stat().st_mtime>=began
            return (home/'.update_output.txt').exists() and (home/'.update_output.txt').stat().st_mtime>=began
        wait(started,90)
        def draining():
            # Keep progress before the gateway/watcher rotates or replaces its
            # output with a final verdict. Diagnostics never change product state.
            saved=E/'updater-progress-longest.log'
            for raw in (home/'.update_output.txt',home/'logs/update-output.last.txt'):
                if raw.exists():
                    data=raw.read_bytes()
                    if len(data)>(saved.stat().st_size if saved.exists() else 0):saved.write_bytes(data)
            state=json.loads((home/'gateway_state.json').read_text())
            telemetry=dict(at=time.time(),gateway=state)
            for name in ['.update_prompt.json','.update_refusal.json','.update_exit_code']:
                path=home/name
                if path.exists():telemetry[name]=path.read_text()[:5000]
            dump(E/'waiting-for-drain.json',telemetry)
            if updater is not None and updater.poll() is not None:
                assert updater.returncode==0, 'updater exited before drain: '+str(updater.returncode)
            exit_path=home/'.update_exit_code'
            if exit_path.exists():
                assert exit_path.read_text().strip()=='0','chat updater exited before drain: '+exit_path.read_text()

            assert state['pid']==before['pid'], 'original gateway exited before in-flight turn drained'
            return state.get('restart_requested') is True
        # Wait for actual product drain state, not merely updater spawn/build time.
        wait(draining,1200)
        dump(E/'during-update.json',dict(gateway_state=json.loads((home/'gateway_state.json').read_text()),barrier_released=False,observed_restart_requested=True,effects=(E/'effects.txt').read_text().splitlines()))
        (E/'release').touch()
        wait(lambda:(E/'received.txt').exists() and TOKEN in (E/'received.txt').read_text(),180)
        if updater:
            rc=updater.wait(timeout=1500);out.close();dump(E/'update-exit.json',dict(exit=rc));assert rc==0,rc
        else:wait(lambda:(home/'.update_exit_code').exists(),1500)
    # Automatic rows forbid driver recovery. quiet-cli explicitly owns its
    # documented public stop/start, and must never be called automatic proof.
    after=observe('after',TARGET,90,'before')
    run([py,HERE/'linux_proof.py','identity','--repo',root,'--sha',TARGET,'--tree',os.environ['TARGET_TREE'],'--output',E/'after-imports.json'],'after-identity')
    receipt=json.loads((home/'logs/update_receipts/latest.json').read_text());dump(E/'update-receipt.json',receipt)
    loadmodule('proof').check_receipt(receipt,began,old,TARGET)
    runtime_observation(after,'after',require_safe=True)
    try:
        dump(E/'inflight-after.json',w.verify(snapshot,home,E/'effects.txt',E/'received.txt'))
    except ValueError as exc:
        transcript=w.rows(home,snapshot['session'])
        dump(E/'witness-failure-counts.json',dict(error=str(exc),
            prior_rows_unchanged=transcript[:len(snapshot['rows'])]==snapshot['rows'],
            canonical_tool_calls=w.call_count(transcript,CALL),
            canonical_tool_results=sum(x['role']=='tool' and x['tool_call_id']==CALL for x in transcript),
            canonical_final_replies=sum(x['role']=='assistant' and x['content']==TOKEN for x in transcript),
            external_tool_effects=w.line_count(E/'effects.txt',EFFECT),
            received_final_replies=w.line_count(E/'received.txt',TOKEN)))
        raise
    run([py,'-c','import sqlite3,sys,json;from clover_cli.sqlite_runtime import is_sqlite_wal_reset_vulnerable as bad;print(json.dumps(dict(executable=sys.executable,prefix=sys.prefix,sqlite=sqlite3.sqlite_version)));assert not bad(sqlite3.sqlite_version_info)'],'safe-sqlite')
    dump(E/'result.json',dict(status=('PASS_NATIVE_ACKNOWLEDGED_STOP_UPDATE_START' if entry == 'quiet-cli' else 'PASS_NATIVE_OLD_TO_TARGET_SYSTEMD_INFLIGHT'),tests=9,old=old,target=TARGET,before_pid=before['pid'],after_pid=after['pid'],service='real registered user systemd',automatic_restart=entry != 'quiet-cli',driver_owned_stop_start=entry == 'quiet-cli',transport='local IRC, not Telegram',layout=layout))

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='fixture':fixture()
    else:
        try:main()
        except Exception:
            E.mkdir(parents=True,exist_ok=True);dump(E/'failure.json',dict(error=traceback.format_exc(),status='FAIL_NOT_ACCEPTED'));raise
