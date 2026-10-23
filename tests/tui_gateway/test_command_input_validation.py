"""Generic native-command input validation, independent of shelved features."""
import pytest

@pytest.mark.parametrize('method', ['command.dispatch','slash.exec'])
@pytest.mark.parametrize('sid', [[],{},17,True,'runtime\n','runtime\u200b','runtime ','é'*600,'\ud800'])
def test_command_session_identifier_rejected_before_lookup(monkeypatch,method,sid):
 from tui_gateway import server
 before=dict(server._sessions)
 try:
  reply=server._methods[method]('fixture-invalid',{'session_id':sid,'name':'status','command':'/status'})
 except Exception as e:
  reply={'raised':type(e).__name__}
 assert reply.get('error',{}).get('code')==-32602,reply
 assert dict(server._sessions)==before

@pytest.mark.parametrize('sid',[None,''])
def test_generic_dispatch_retains_sessionless_status_path(monkeypatch,sid):
 from tui_gateway import server
 # Proactive status is an existing sessionless generic command; never create
 # a session or grant permission just to display these settings.
 reply=server._methods['command.dispatch']('fixture-status',{'session_id':sid,'name':'proactive','arg':'status'})
 assert 'result' in reply,reply

@pytest.mark.parametrize('field',['name','arg'])
def test_dispatch_rejects_nonliteral_command_text(field):
 from tui_gateway import server
 params={'name':'status','arg':'','session_id':None};params[field]=[]
 reply=server._methods['command.dispatch']('fixture-text',params)
 assert reply['error']['code']==-32602
