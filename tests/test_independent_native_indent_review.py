"""Minimal native indentation repro: no content decoder or inline markup."""
import json,re
import pytest,yaml
from tests.gateway.test_trusted_human_origin_ingress import network_guard

@pytest.mark.parametrize('presence',[False,True])
@pytest.mark.parametrize('progressive',[False,True])
def test_native_split_python_indentation_survives(monkeypatch,tmp_path,presence,progressive):
    import tests.test_presence_stream_native_wire as native
    from telegram import Bot
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from cron.presence import ThoughtStore
    target='Deployment \n```python\n'+''.join(f'    R{i:04d} = {i:04d} 雪🙂\n' for i in range(310))+'```'
    stream=native.enable_native_stream
    monkeypatch.setattr(native,'enable_native_stream',lambda mp,h,_text,**kw:stream(mp,h,target,**kw))
    if not presence:
        setup=native.setup_host
        def off(*a,**kw):
            h,requests,bot=setup(*a,**kw);path=h.home/'config.yaml';cfg=yaml.safe_load(path.read_text());cfg.pop('presence',None);cfg.pop('proactive',None);path.write_text(yaml.safe_dump(cfg));return h,requests,bot
        monkeypatch.setattr(native,'setup_host',off)
    visible={};operations=[];initiatives=set()
    ps=TelegramAdapter.send_presence
    async def initiative(self,payload):
        mid=await ps(self,payload);initiatives.add(str(mid));return mid
    monkeypatch.setattr(TelegramAdapter,'send_presence',initiative)
    for method in ('send_message','edit_message_text','delete_message'):
        original=getattr(Bot,method)
        def bind(method,original):
            async def call(self,*a,**kw):
                result=await original(self,*a,**kw);mid=str(result.message_id) if method=='send_message' else str(kw['message_id'])
                operations.append((method,mid,kw.get('text')))
                if method=='delete_message':visible.pop(mid,None)
                else:visible[mid]=kw['text']
                return result
            return call
        monkeypatch.setattr(Bot,method,bind(method,original))
    with native.native_telegram_stream(monkeypatch,tmp_path,'multipart',progressive=progressive) as observed:
        rows=[(mid,spaces,row) for mid,text in visible.items() if mid not in initiatives for spaces,row in re.findall(r'(?m)^([ \t]*)R(\d{4})',text)]
        snapshot=dict(target=target,visible=visible,operations=operations,rows=rows,assessments=len(observed.requests),publications=len(ThoughtStore().publications()),model_calls=len(observed.wires))
        (tmp_path/'physical-wire.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
        assert len(observed.wires)==1 and observed.wires[0]['stream'] is True
        assert len(observed.requests)==int(presence) and len(ThoughtStore().publications())==int(presence)
        assert [row for _,_,row in rows]==[f'{i:04d}' for i in range(310)]
        assert all(spaces=='    ' for _,spaces,_ in rows),'native split removed Python indentation'
