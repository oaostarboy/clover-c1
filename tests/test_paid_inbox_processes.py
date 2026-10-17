"""Independent processes racing at the actual SDK physical HTTP seam."""
import multiprocessing
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from cron.inbox_watcher import Denied, Limits, Store


def attempt(path, endpoint, index, queue, crash=False):
    import httpx
    from openai import OpenAI
    from agent.auxiliary_client import call_bounded_inbox
    store=Store(path)
    def reserve():
        store.reserve(Limits(attempts_day=2),now=172800,run_id=str(index),input_units=100)
        if crash: os._exit(0)
    try:
        with OpenAI(api_key='synthetic',base_url=endpoint,max_retries=0,
                    http_client=httpx.Client(follow_redirects=False,trust_env=False)) as client:
            call_bounded_inbox(client=client,messages=[{'role':'user','content':'synthetic'}],
                               max_output_tokens=128,reserve=reserve)
    except Denied: queue.put('denied')
    except Exception: queue.put('attempted')


def test_separate_process_allowance_counts_real_localhost_http_and_crash(tmp_path):
    hits=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.rfile.read(int(self.headers.get('Content-Length','0')))
            self.send_response(500);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(b'{"error":{"message":"offline synthetic"}}')
        def log_message(self,*a): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
    path=tmp_path/'state.db';Store(path,create=True)
    ctx=multiprocessing.get_context('spawn');q=ctx.Queue()
    endpoint=f'http://127.0.0.1:{server.server_port}/v1'
    # Crash AFTER committed reservation, BEFORE transmission: spent forever today.
    child=ctx.Process(target=attempt,args=(str(path),endpoint,'crash',q,True));child.start();child.join(30)
    assert child.exitcode==0 and Store(path).usage()['attempts']==1 and not hits
    workers=[ctx.Process(target=attempt,args=(str(path),endpoint,i,q)) for i in range(6)]
    for p in workers:p.start()
    for p in workers:p.join(30);assert p.exitcode==0
    results=[q.get(timeout=5) for _ in workers]
    server.shutdown();server.server_close();t.join(5)
    assert results.count('attempted')==1 and results.count('denied')==5
    assert len(hits)==1 and Store(path).usage()['attempts']==2


@pytest.mark.parametrize('code',[401,408,429,500,307])
def test_auth_transient_redirect_and_fallback_errors_are_single_physical_attempt(code):
    import httpx
    from openai import OpenAI
    from agent.auxiliary_client import call_bounded_inbox
    calls=[];reservations=[]
    def transport(request):
        calls.append(str(request.url))
        return httpx.Response(code,headers={'Location':'https://unapproved.invalid/resend'},
                              json={'error':{'message':'synthetic error'}})
    with OpenAI(api_key='synthetic',base_url='https://offline.invalid/v1',max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(transport),follow_redirects=False)) as c:
        with pytest.raises(Exception):
            call_bounded_inbox(client=c,messages=[{'role':'user','content':'test'}],max_output_tokens=128,
                               reserve=lambda:reservations.append(1))
    assert len(calls)==len(reservations)==1 and 'unapproved' not in calls[0]
