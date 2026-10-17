"""Registration crash reconciliation, data constraints, private output and scope."""
from types import SimpleNamespace
import json
import pytest
from tests.test_paid_inbox_lifecycle import draft
from tests.test_paid_inbox_consent import FakeBinding
from tests.test_paid_inbox_runner import InputBinding,accepted
from tests.gateway.test_proactive_command import home  # noqa: F401


def test_registration_failure_retains_owned_id_and_retry_does_not_duplicate(home,monkeypatch):
    from cron import scheduler_provider,jobs,inbox_watcher as w
    class Scheduler:
        fail=True
        def register_job(self,job):
            if self.fail:raise RuntimeError('synthetic external registration failure')
    provider=Scheduler();monkeypatch.setattr(scheduler_provider,'resolve_cron_scheduler',lambda:provider)
    b=FakeBinding(home);d=draft(b);d.render();p=d.accept('accept-extra-usage')
    assert p['state']=='pending' and p['job_id'] and len(jobs.load_jobs())==1
    assert w.Store(w.state_path(home)).read()['policy']==p
    # Pending cannot fetch, bill or deliver even if external scheduling raced.
    r=InputBinding(home);w.run(jobs.get_job(p['job_id']),r,now=172800)
    assert not r.reads and not r.attempts and not r.sent
    provider.fail=False
    active=d.accept('accept-extra-usage')
    assert active['state']=='active' and active['job_id']==p['job_id'] and len(jobs.load_jobs())==1
    r.client.close()


def test_crash_after_durable_job_is_reconciled_from_typed_metadata(home,monkeypatch):
    from cron import scheduler,jobs
    b=FakeBinding(home);d=draft(b);d.render()
    original=scheduler.create_job_with_scheduler_registration
    def crash(**kw):
        original(**kw)
        raise RuntimeError('synthetic crash after durable job')
    monkeypatch.setattr(scheduler,'create_job_with_scheduler_registration',crash)
    with pytest.raises(Exception):d.accept('accept-extra-usage')
    assert len(jobs.load_jobs())==1
    monkeypatch.setattr(scheduler,'create_job_with_scheduler_registration',original)
    p=d.accept('accept-extra-usage')
    assert p['state']=='active' and len(jobs.load_jobs())==1


@pytest.mark.parametrize('scores',[ [{'index':0,'score':True,'reason':'x'}],
    [{'index':0,'score':11,'reason':'x'}], [{'index':1,'score':9,'reason':'x'}],
    [{'index':0,'score':9,'reason':'x'*161}], [{'index':0,'score':9,'reason':'x','destination':'group'}],
    [{'index':0,'score':9,'reason':'x'},{'index':0,'score':9,'reason':'x'}]])
def test_untrusted_classifier_cannot_invent_mail_or_actions(scores):
    from cron.inbox_watcher import _score,Denied
    response=SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
                message=SimpleNamespace(content=json.dumps({'scores':scores})))])
    with pytest.raises(Denied):_score(response,1,7)


@pytest.mark.parametrize('bad',['oversized','duplicate','wrong-id','bad-body','wrong-epoch'])
def test_invalid_input_has_no_physical_provider_attempt(home,bad):
    from cron import inbox_watcher as w
    b=InputBinding(home);job=accepted(home,b);w.run(job,b,now=172800)
    item={'id':'11','from':'x','subject':'urgent','date':'today'}
    if bad=='oversized':item['subject']='X'*501
    if bad=='wrong-id':item['id']='9'
    if bad=='bad-body':item['body']='PRIVATE-CANARY'
    b.items=[item,item] if bad=='duplicate' else [item]
    if bad=='wrong-epoch':b.fetch=lambda *a,**k:{'epoch':'2','cursor':11,'items':[item]}
    w.run(job,b,now=174600)
    assert not b.attempts and not b.sent and w.Store(w.state_path(home)).usage()['attempts']==0
    b.client.close()


def test_revocation_after_paid_request_prevents_delivery_and_does_not_refund(home):
    from cron import inbox_watcher as w
    b=InputBinding(home);job=accepted(home,b);w.run(job,b,now=172800)
    b.items=[{'id':'11','from':'x','subject':'urgent','date':'today'}]
    original=b.request
    def request(*a,**kw):
        response=original(*a,**kw);w.revoke(b);return response
    b.request=request
    w.run(job,b,now=174600)
    assert len(b.attempts)==1 and not b.sent
    assert w.Store(w.state_path(home)).usage()['attempts']==1
    b.client.close()


def test_physical_reservation_uses_current_utc_day_after_slow_fetch(home,monkeypatch):
    from cron import inbox_watcher as w
    b=InputBinding(home);job=accepted(home,b);w.run(job,b,now=172800)
    b.items=[{'id':'11','from':'x','subject':'urgent','date':'today'}]
    clock=[259199]
    monkeypatch.setattr(w.time,'time',lambda:clock[0])
    original=b.fetch
    def slow(*a,**kw):
        result=original(*a,**kw);clock[0]=259201;return result
    b.fetch=slow
    w.run(job,b)
    assert len(b.attempts)==1 and w.Store(w.state_path(home)).usage()['day']==3
    b.client.close()
