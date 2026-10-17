"""Independent watcher state remains visible when main proactivity is Off."""
import pytest
from gateway.config import Platform
from tests.test_proactive_picker import picker, query, tap, home  # noqa: F401
from tests.test_proactive_customize_native import button
from tests.test_paid_inbox_admission import setup
from tests.gateway.test_proactive_command import event


@pytest.mark.asyncio
async def test_main_off_discloses_active_watcher_without_source_or_provider_access(picker, home, monkeypatch):
    from cron import jobs
    r, a = setup(picker, monkeypatch)
    review = await r._handle_message(event(
        '/proactive watcher review native-imap INBOX 30 7 2 openai this-private-chat headers-only',
        uid='123', chat_id='123'))
    await r._handle_message(event(review.splitlines()[-1], uid='123', chat_id='123'))
    original = jobs.load_jobs()
    del r.adapters[Platform.EMAIL]
    import gateway.inbox_watcher_controls as controls
    monkeypatch.setattr(controls.Binding, 'validate_route', lambda self: pytest.fail('status cannot look up provider credentials'))
    text = await r._handle_message(event('/proactive off', uid='123', chat_id='123'))
    assert 'Watcher active' in text and 'watcher off' in text
    assert jobs.load_jobs() == original
    status = await r._handle_message(event('/proactive watcher status', uid='123', chat_id='123'))
    assert 'Watcher active' in status and 'attempts left' in status
    await r._handle_message(event('/proactive watcher', uid='123', chat_id='123'))
    page = a._send_message_with_thread_fallback.call_args.kwargs
    assert 'Watcher active' in page['text']
    assert button(page['reply_markup'], 'Watcher Off')
