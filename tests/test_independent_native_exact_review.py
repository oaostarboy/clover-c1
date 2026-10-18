"""Independent native wire identity and lossless record-budget probes.

Decoder implements only grammar generated here: plain escaped MarkdownV2,
inline code, fenced python code, and terminal chunk indicators. It never
normalizes whitespace, underscores, numbering within body, or backslashes.
"""
import json,re
from collections import Counter
import pytest,yaml
from tests.gateway.test_trusted_human_origin_ingress import network_guard

def decode(text,parse_mode):
    if str(parse_mode).lower()!='markdownv2':return text
    out=[];i=0;code=False;inline=False
    while i<len(text):
        if text[i]=='\\':
            assert i+1<len(text),'dangling MarkdownV2 escape'
            out.append(text[i+1]);i+=2;continue
        if text.startswith('```',i):
            if not code:
                stop=text.find('\n',i+3)
                assert stop>=0,'invalid opening fence'
                i=stop+1;code=True
            else:i+=3;code=False
            continue
        if text[i]=='`' and not code:
            inline=not inline;i+=1;continue
        out.append(text[i]);i+=1
    assert not code and not inline,'unclosed generated MarkdownV2'
    return ''.join(out)

@pytest.mark.parametrize('style',['escape','inline','code'])
@pytest.mark.parametrize('rows',[117,239])
@pytest.mark.parametrize('progressive',[False,True])
@pytest.mark.parametrize('presence',[False,True])
def test_lossless_records_through_actual_native_budget(monkeypatch,tmp_path,style,rows,progressive,presence):
    run_native(monkeypatch,tmp_path,style,rows,progressive,presence)

@pytest.mark.parametrize('progressive',[False,True])
@pytest.mark.parametrize('presence',[False,True])
def test_nested_native_budget_preserves_indented_code(monkeypatch,tmp_path,progressive,presence):
    run_native(monkeypatch,tmp_path,'code',239,progressive,presence,nested=True)

def run_native(monkeypatch,tmp_path,style,rows,progressive,presence,nested=False):
    import tests.test_presence_stream_native_wire as native
    from telegram import Bot
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from gateway.platforms.base import utf16_len
    from gateway.stream_consumer import GatewayStreamConsumer
    from tests.test_owner_telegram_wire import TelegramEndpoint
    from cron.presence import ThoughtStore
    records=[];rendered=[]
    for i in range(rows):
        payload='  雪🙂e\u0301  !()[]{}.+-=|  \\x  '
        if style=='code':payload+='`literal`  '
        records.append(('    ' if style=='code' else '')+f'R{i:04d}:'+payload+f':E{i:04d}')
        rendered.append(('    ' if style=='code' else '')+f'R{i:04d}:'+('`'+payload+'`' if style=='inline' else payload)+f':E{i:04d}')
    body='\n'.join(rendered)
    target='Deployment \n'+('```python\n'+body+'\n```' if style=='code' else body)
    stream=native.enable_native_stream
    def enable(mp,h,_text,**kw):
        if nested and progressive:kw['first_delta']=target[:len(target)//2]
        return stream(mp,h,target,**kw)
    monkeypatch.setattr(native,'enable_native_stream',enable)
    if nested:monkeypatch.setattr(GatewayStreamConsumer,'_raw_message_limit',lambda self:32768)
    if not presence:
        setup=native.setup_host
        def default_off(*a,**kw):
            h,requests,bot=setup(*a,**kw);path=h.home/'config.yaml';cfg=yaml.safe_load(path.read_text());cfg.pop('presence',None);cfg.pop('proactive',None);path.write_text(yaml.safe_dump(cfg));return h,requests,bot
        monkeypatch.setattr(native,'setup_host',default_off)
    http=TelegramEndpoint.do_request;attempts=[]
    async def bounded(self,url,method,request_data=None,**kw):
        params=request_data.parameters if request_data else {}
        if url.endswith(('sendMessage','editMessageText')):
            attempts.append((url.rsplit('/',1)[-1],params));assert utf16_len(params['text'])<=4096
        return await http(self,url,method,request_data,**kw)
    monkeypatch.setattr(TelegramEndpoint,'do_request',bounded)
    visible={};ops=[];initiatives=set()
    presence_send=TelegramAdapter.send_presence
    async def initiative(self,payload):
        mid=await presence_send(self,payload);initiatives.add(str(mid));return mid
    monkeypatch.setattr(TelegramAdapter,'send_presence',initiative)
    for method in ('send_message','edit_message_text','delete_message'):
        original=getattr(Bot,method)
        def bind(method,original):
            async def observe(self,*args,**kw):
                result=await original(self,*args,**kw)
                mid=str(result.message_id) if method=='send_message' else str(kw['message_id'])
                ops.append((method,mid,kw.get('text'),str(kw.get('parse_mode'))))
                if method=='delete_message':visible.pop(mid,None)
                else:visible[mid]=(kw['text'],kw.get('parse_mode'))
                return result
            return observe
        monkeypatch.setattr(Bot,method,bind(method,original))
    with native.native_telegram_stream(monkeypatch,tmp_path,'multipart',progressive=progressive) as observed:
        answer=[decode(text,mode) for mid,(text,mode) in visible.items() if mid not in initiatives]
        # Only a native-generated terminal suffix may be removed. Body syntax
        # and whitespace stay unchanged; known row IDs prove order and count.
        answer=[re.sub(r' \(\d+/\d+\)$','',text) for text in answer]
        actual=''.join(answer).removeprefix('Deployment \n').replace('\n','')
        expected=''.join(records)
        found=re.findall(r'([ \t]*R\d{4}:.*?:E\d{4})',actual)
        snapshot=dict(target=target,expected_records=records,actual_records=found,visible=visible,operations=ops,attempts=attempts)
        (tmp_path/'physical-wire.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2,default=str))
        assert all(utf16_len(params['text'])<=4096 for _,params in attempts)
        assert len(observed.wires)==1 and observed.wires[0]['stream'] is True
        assert len(observed.requests)==int(presence)
        assert len(ThoughtStore().publications())==int(presence)
        assert actual==expected,'actual native text changed a literal character, horizontal whitespace, count, or order'
        assert Counter(re.findall(r'R(\d{4}):',actual))==Counter(f'{i:04d}' for i in range(rows))
