"""Runtime public state -> CLI producer -> observer -> real renderer, offline."""
import io
import json
import pytest
from clover_cli.activity_events import ActivityEventWriter
from tools.agent_job_observer import AgentJobObserver
from agent.delegation_activity import DelegationActivityTracker
from tests.agent.test_delegation_checkpoint import _agent


class Sink:
    def __init__(self): self.tracker=DelegationActivityTracker()
    def observe(self,*args,**kw): self.tracker.observe(*args,**kw)


@pytest.mark.parametrize('state,expected,label',[
    ('requesting','waiting','requesting provider'), ('waiting','waiting','waiting for provider'),
    ('retrying','waiting','retrying provider'), ('provider_result','running','provider result received'),
    ('awaiting_input','blocked','awaiting input'),
])
def test_public_producer_state_survives_observer_and_renderer(state,expected,label):
    stream=io.StringIO(); writer=ActivityEventWriter(stream); sink=Sink()
    observer=AgentJobObserver(session_id='test-owned-worker',sink=sink,group_id='test-owned-group',index=0,title='Saved build',model='test-model',parser='clover-activity')
    observer.start()
    agent=_agent(); agent.tool_progress_callback=writer.tool_progress_callback
    agent._emit_public_status(state)
    observer.feed(stream.getvalue())
    snap=sink.tracker.snapshot('test-owned-group')[0]
    assert snap['state']==expected
    assert snap['reason']==label
    card=sink.tracker.render('test-owned-group')
    assert 'Saved build' in card
    if expected in ('waiting','blocked'): assert label in card
    writer.result('done','completed'); observer.feed(stream.getvalue()[len(stream.getvalue()):])


@pytest.mark.parametrize('parser',['none','clover-activity','claude-stream-json'])
def test_exit_zero_without_result_is_not_task_completion(parser):
    sink=Sink(); observer=AgentJobObserver(session_id='test-worker',sink=sink,group_id='test-group',index=0,title='Saved build',parser=parser)
    observer.start(); observer.feed('process is alive\n'); observer.finish(0)
    snap=sink.tracker.snapshot('test-group')[0]
    assert snap['state']=='incomplete'
    assert 'not verified' in snap['reason']
    observer.finish(0)
    observer.feed(json.dumps({'clover_activity':1,'event':'result','status':'completed','text':'late'})+'\n')
    assert sink.tracker.snapshot('test-group')[0]['state']=='incomplete'


def test_built_unverified_is_not_failure_or_complete():
    stream=io.StringIO(); writer=ActivityEventWriter(stream); sink=Sink()
    observer=AgentJobObserver(session_id='test-worker',sink=sink,group_id='test-group',index=0,title='Saved build',parser='clover-activity')
    observer.start(); writer.result('patch saved, tests not run','built_unverified')
    observer.feed(stream.getvalue()); observer.finish(0)
    snap=sink.tracker.snapshot('test-group')[0]
    assert snap['state']=='incomplete' and 'not verified' in snap['reason']
