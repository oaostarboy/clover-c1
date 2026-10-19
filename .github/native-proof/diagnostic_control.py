"""Native stdlib control for concurrent stack diagnostics, no product imports."""
import faulthandler
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

HERE=Path(__file__).resolve().parent
if len(sys.argv)==1:
 out=Path('evidence');out.mkdir(exist_ok=True)
 results=[]
 for mode in ['none','watchdog','gil-held']:
  with (out/(mode+'.log')).open('wb') as log:
   proc=subprocess.run([sys.executable,str(Path(__file__).resolve()),mode,str((out/(mode+'-stacks.log')).resolve())],stdout=log,stderr=subprocess.STDOUT,timeout=30)
  results.append({'mode':mode,'returncode':proc.returncode})
 (out/'control.json').write_text(json.dumps({'python':sys.version,'results':results},indent=2))
 print(json.dumps(results))
else:
 mode,path=sys.argv[1:]
 stack=open(path,'w')
 faulthandler.enable(stack,all_threads=True)
 stop=threading.Event()
 def busy():
  while not stop.wait(.001):
   # Pure Python work concurrent with native thread start/exit churn.
   sum(range(50))
 workers=[threading.Thread(target=busy) for _ in range(8)]
 for worker in workers:worker.start()
 def sampler():
  while not stop.wait(.02):faulthandler.dump_traceback(file=stack,all_threads=True)
 if mode=='watchdog':faulthandler.dump_traceback_later(.02,repeat=True,file=stack)
 observer=None
 if mode=='gil-held':
  observer=threading.Thread(target=sampler);observer.start()
 end=time.monotonic()+8
 n=0
 while time.monotonic()<end:
  thread=threading.Thread(target=lambda:time.sleep(.001));thread.start();thread.join();n+=1
 stop.set()
 for worker in workers:worker.join()
 if observer is not None:observer.join()
 faulthandler.cancel_dump_traceback_later()
 print(json.dumps({'mode':mode,'completed':True,'threads_created':n}))
