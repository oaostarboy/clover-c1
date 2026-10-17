"""Watcher-specific raw data logging must stay closed even with SDK debug on."""
import logging
import httpx
import pytest
from openai import OpenAI
from agent.auxiliary_client import call_bounded_inbox


def test_watcher_sdk_debug_never_logs_raw_mail_without_muting_other_callers(caplog):
    secret='PRIVATE-WATCHER-DEBUG-CANARY'
    def send(request):return httpx.Response(500,json={'error':{'message':secret}})
    with OpenAI(api_key='synthetic',base_url='https://offline.invalid/v1',max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(send),follow_redirects=False)) as c:
        with caplog.at_level(logging.DEBUG,logger='openai._base_client'):
            with pytest.raises(Exception):
                call_bounded_inbox(client=c,messages=[{'role':'user','content':secret}],
                                   max_output_tokens=128,reserve=lambda:None)
        assert secret not in caplog.text
        caplog.clear()
        with caplog.at_level(logging.DEBUG,logger='openai._base_client'):
            with pytest.raises(Exception):
                c.chat.completions.create(model='gpt-4o-mini-2024-07-18',messages=[{'role':'user','content':secret}])
        assert secret in caplog.text  # unrelated caller logging policy is untouched


def test_source_disables_protocol_debug_before_any_authenticated_imap_operation(monkeypatch):
    from tests.gateway._plugin_adapter_loader import load_plugin_adapter
    m=load_plugin_adapter('email')
    a=object.__new__(m.EmailAdapter)
    a._address='owner@example.invalid';a._password='synthetic';a._imap_host='offline.invalid';a._imap_port=993
    class Imap:
        def __init__(self,*args,**kwargs):self.debug=4
        def login(self,*args):assert self.debug==0, 'protocol debug can expose authenticated mail and secrets'
        def select(self,*args,**kwargs):return 'OK',[]
        def response(self,key):return key,[b'1']
        def logout(self):pass
    monkeypatch.setattr(m.imaplib,'IMAP4_SSL',Imap)
    assert a.inbox_watcher_read(None,max_items=1)['items']==[]
