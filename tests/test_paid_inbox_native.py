"""Native PTB serialization through the real gateway and scheduler registration."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from gateway.config import Platform
from tests.test_proactive_picker import picker, query, tap, home  # noqa: F401
from tests.test_proactive_customize_native import button
from tests.gateway.test_proactive_command import event


@pytest.mark.asyncio
async def test_native_paid_review_explicit_accept_persists_exact_job(picker,home,monkeypatch):
    from cron import inbox_watcher as w
    r,a=picker
    r.config.platforms[Platform.TELEGRAM].extra['allow_admin_from']=['123']
    class Source:
        def inbox_watcher_metadata(self):
            return {'type':'native-imap-headers-v1','account':'owner@example.invalid','folder':'INBOX',
                    'query':'new UID only','fingerprint':'a'*64}
        def inbox_watcher_read(self,*args,**kwargs): raise AssertionError('no inbox at activation')
    r.adapters[Platform.EMAIL]=Source()
    a.gateway_runner=r
    # Explicit acceptance checks credential availability, not a provider request.
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-offline-only')
    ev=event('/proactive customize',uid='123',chat_id='123')
    await r._handle_message(ev)
    current=a._send_message_with_thread_fallback.call_args.kwargs
    async def choose(label):
        nonlocal current
        q=query(uid='123',chat='123',data=button(current['reply_markup'],label))
        await tap(a,q)
        current=q.edit_message_text.call_args.kwargs
        return q
    await choose('⚙️ Advanced / costs')
    await choose('📬 Inbox watcher · Paid')
    assert 'INBOX' in current['text'] and 'draft' in current['text']
    for label in ['INBOX · headers only','Every 30 min','This private chat','OpenAI · fixed route','Urgent · 7+','2/day · small']:
        await choose(label)
        assert not w.state_path(home).exists()
        assert all(len(row)<=2 for row in current['reply_markup'].inline_keyboard)
    assert 'accept extra usage' in current['text'] and 'no fallback' in current['text']
    old=button(current['reply_markup'],'Accept extra usage')
    await choose('Accept extra usage')
    assert 'active' in current['text']
    policy=w.Store(w.state_path(home)).read()['policy']
    from cron.jobs import get_job,load_jobs
    job=get_job(policy['job_id'])
    assert job['inbox_watcher']['digest']==policy['digest'] and job['no_agent']
    replay=query(uid='123',chat='123',data=old)
    await tap(a,replay)
    assert len(load_jobs())==1 and not (home/'config.yaml').exists()
