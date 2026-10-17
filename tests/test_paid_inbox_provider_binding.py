"""Additional frozen-candidate probe: provider rotation and late registration authority."""
import pytest
from tests.test_paid_inbox_admission import setup
from tests.test_proactive_picker import picker,home  # noqa: F401
from tests.gateway.test_proactive_command import event
from tests.test_paid_inbox_consent import FakeBinding
from tests.test_paid_inbox_lifecycle import draft


@pytest.mark.asyncio
async def test_accepted_credential_rotation_is_denied_before_source_read(picker,home,monkeypatch):
    from cron import inbox_watcher as w,jobs
    from gateway.inbox_watcher_controls import Binding
    r,a=setup(picker,monkeypatch)
    review=await r._handle_message(event('/proactive watcher review native-imap INBOX 30 7 2 openai this-private-chat headers-only',uid='123',chat_id='123'))
    await r._handle_message(event(review.splitlines()[-1],uid='123',chat_id='123'))
    job=jobs.load_jobs()[0]
    monkeypatch.setenv('OPENAI_API_KEY','rotated-synthetic-key')
    binding=Binding(r,event('/proactive watcher',uid='123',chat_id='123').source)
    with pytest.raises(w.Denied):w._policy_guard(w.Store(w.state_path(home)),job,binding)


def test_late_registration_policy_loss_never_claims_active(home,monkeypatch):
    from cron import scheduler_provider
    b=FakeBinding(home);d=draft(b);d.render()
    class Provider:
        def register_job(self,job):b.allowed=False
    monkeypatch.setattr(scheduler_provider,'resolve_cron_scheduler',lambda:Provider())
    result=d.accept('accept-extra-usage')
    assert result['state']=='pending'
