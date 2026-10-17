"""Admission is current and local; an old native paid review grants nothing."""
import time
import pytest
from gateway.config import Platform
from tests.test_proactive_picker import picker,query,tap,home  # noqa: F401
from tests.test_proactive_customize_native import button
from tests.gateway.test_proactive_command import event


class Source:
    account='owner@example.invalid'
    fingerprint='a'*64
    folder='INBOX'
    def inbox_watcher_metadata(self):
        return {'type':'native-imap-headers-v1','account':self.account,'folder':self.folder,
                'query':'new UID only','fingerprint':self.fingerprint}
    def inbox_watcher_read(self,*a,**k): raise AssertionError('no source read from menu')


def setup(picker,monkeypatch):
    r,a=picker
    r.config.platforms[Platform.TELEGRAM].extra['allow_admin_from']=['123']
    r.adapters[Platform.EMAIL]=Source();a.gateway_runner=r
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-offline-only')
    return r,a


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['foreign','thread','message','expired','source','credential','policy','managed','malformed','route','missing-key'])
async def test_paid_accept_rechecks_every_bound_scope(picker,home,monkeypatch,tmp_path,change):
    from cron.inbox_watcher import state_path
    r,a=setup(picker,monkeypatch)
    await r._handle_message(event('/proactive watcher',uid='123',chat_id='123'))
    current=a._send_message_with_thread_fallback.call_args.kwargs
    for label in ['INBOX · headers only','Every 30 min','This private chat','OpenAI · fixed route','Urgent · 7+','2/day · small']:
        q=query(uid='123',chat='123',data=button(current['reply_markup'],label));await tap(a,q)
        current=q.edit_message_text.call_args.kwargs
    q=query(uid='123',chat='123',data=button(current['reply_markup'],'Accept extra usage'))
    if change=='foreign':q.from_user.id='guest'
    if change=='thread':q.message.message_thread_id=1
    if change=='message':q.message.message_id=999
    if change=='expired':a._choice_picker_state['123']['guard']['expires_at']=time.monotonic()-1
    if change=='source':r.adapters[Platform.EMAIL].fingerprint='c'*64
    if change=='credential':a.config.token='rotated-synthetic'
    if change=='policy':
        (home/'config.yaml').write_text('platforms:\n  telegram:\n    allow_admin_from: [other]\n')
    if change in ('managed','malformed'):
        d=tmp_path/'managed';d.mkdir();monkeypatch.setenv('CLOVER_MANAGED_DIR',str(d))
        (d/'config.yaml').write_text('platforms:\n  telegram:\n    allow_admin_from: [other]\n' if change=='managed' else '[broken')
    if change=='route':
        import gateway.inbox_watcher_controls as controls
        monkeypatch.setattr(controls,'ROUTE','unapproved/model')
    if change=='missing-key':monkeypatch.delenv('OPENAI_API_KEY')
    await tap(a,q)
    assert not state_path(home).exists()
    from cron.jobs import load_jobs
    assert not load_jobs()


@pytest.mark.asyncio
@pytest.mark.parametrize('args',['on','recommended','customize','watcher on','watcher accept','watcher accept-extra-usage bad-token'])
async def test_normal_on_recommended_unknown_text_save_and_cancel_cannot_accept_paid_usage(picker,home,monkeypatch,args):
    from cron.inbox_watcher import state_path
    r,a=setup(picker,monkeypatch)
    await r._handle_message(event('/proactive '+args,uid='123',chat_id='123'))
    if args=='customize':
        current=a._send_message_with_thread_fallback.call_args.kwargs
        q=query(uid='123',chat='123',data=button(current['reply_markup'],'💾 Save'));await tap(a,q)
    assert not state_path(home).exists()
    from cron.jobs import load_jobs
    assert not load_jobs()


@pytest.mark.asyncio
async def test_text_review_exact_token_accept_off_and_main_off_independence(picker,home,monkeypatch):
    from cron.inbox_watcher import state_path,Store
    from cron.jobs import load_jobs,get_job
    r,a=setup(picker,monkeypatch)
    command='/proactive watcher review native-imap INBOX 30 7 2 openai this-private-chat headers-only'
    review=await r._handle_message(event(command,uid='123',chat_id='123'))
    assert 'accept extra usage' in review and not state_path(home).exists()
    accept=review.splitlines()[-1]
    text=await r._handle_message(event(accept,uid='123',chat_id='123'))
    assert 'active' in text and len(load_jobs())==1
    job_id=Store(state_path(home)).read()['policy']['job_id']
    await r._handle_message(event('/proactive off',uid='123',chat_id='123'))
    assert get_job(job_id)['enabled']
    assert 'refused' in await r._handle_message(event(accept,uid='123',chat_id='123'))
    off=await r._handle_message(event('/proactive watcher off',uid='123',chat_id='123'))
    assert 'revoked' in off and not get_job(job_id)['enabled']


@pytest.mark.asyncio
async def test_watcher_off_still_revokes_if_mail_source_disappeared(picker,home,monkeypatch):
    from cron.inbox_watcher import state_path,Store
    from cron.jobs import get_job
    r,a=setup(picker,monkeypatch)
    review=await r._handle_message(event('/proactive watcher review native-imap INBOX 30 7 2 openai this-private-chat headers-only',uid='123',chat_id='123'))
    await r._handle_message(event(review.splitlines()[-1],uid='123',chat_id='123'))
    job_id=Store(state_path(home)).read()['policy']['job_id']
    del r.adapters[Platform.EMAIL]
    off=await r._handle_message(event('/proactive watcher off',uid='123',chat_id='123'))
    assert 'revoked' in off and not get_job(job_id)['enabled']


@pytest.mark.asyncio
async def test_pending_registration_has_real_text_retry_without_new_review_or_duplicate(picker,home,monkeypatch):
    from cron import scheduler_provider,jobs
    from cron.inbox_watcher import Store,state_path
    r,a=setup(picker,monkeypatch)
    class Provider:
        fail=True
        def register_job(self,job):
            if self.fail:raise RuntimeError('synthetic registration error')
    provider=Provider();monkeypatch.setattr(scheduler_provider,'resolve_cron_scheduler',lambda:provider)
    review=await r._handle_message(event('/proactive watcher review native-imap INBOX 30 7 2 openai this-private-chat headers-only',uid='123',chat_id='123'))
    pending=await r._handle_message(event(review.splitlines()[-1],uid='123',chat_id='123'))
    assert 'pending' in pending
    provider.fail=False
    resumed=await r._handle_message(event('/proactive watcher retry-registration',uid='123',chat_id='123'))
    assert 'active' in resumed and len(jobs.load_jobs())==1
    assert Store(state_path(home)).read()['policy']['state']=='active'
