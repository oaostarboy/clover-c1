"""Consent -> actual disposable job owner -> exact durable readback."""
import importlib.util
from pathlib import Path
import pytest
from tests.gateway.test_proactive_command import home  # noqa: F401


class FakeBinding:
    def __init__(self, home): self.home, self.reads, self.sent, self.allowed = home, 0, [], True
    def snapshot(self):
        if not self.allowed: raise ValueError('revoked')
        return {'home': str(self.home.resolve()), 'actor': 'owner',
                'source': {'type':'native-imap-headers-v1', 'account':'owner@example.invalid',
                           'folder':'INBOX','query':'new UID only','fingerprint':'a'*64},
                'destination': {'platform':'telegram','chat_id':'123','thread_id':None,
                                'credential':'b'*16}, 'route': 'openai/gpt-4o-mini-2024-07-18',
                'authority': 'synthetic-policy'}
    def validate_route(self): pass


def test_explicit_accept_registers_one_owned_script_job_and_readback(home, monkeypatch):
    from cron import inbox_watcher as w
    assert hasattr(w, 'Draft'), 'missing explicit consent transaction'
    from cron import jobs, scheduler_provider
    monkeypatch.setattr(jobs, 'CLOVER_DIR', home)
    monkeypatch.setattr(jobs, 'CRON_DIR', home/'cron')
    monkeypatch.setattr(jobs, 'JOBS_FILE', home/'cron'/'jobs.json')
    binding = FakeBinding(home)
    d = w.Draft(binding)
    assert not (home/'inbox-watcher'/'state.db').exists()
    for value in ['source', 'cadence 30', 'destination', 'route', 'urgency 7', 'limits 2']:
        d.choose(value)
    review = d.render()['text']
    assert all(s in review for s in ['extra usage', '4096', '256', '2/day', 'UTC', 'From', 'no fallback'])
    with pytest.raises(w.Denied): d.accept('save')
    result = d.accept('accept-extra-usage')
    assert result['state'] == 'active'
    job = jobs.get_job(result['job_id'])
    assert job['no_agent'] and job['attach_to_session'] is False
    assert job['deliver'] == 'local' and job['monitor_script'] is None and job['monitor_url'] is None
    assert job['inbox_watcher']['digest'] == result['digest']
    assert w.Store(home/'inbox-watcher'/'state.db').read()['policy'] == result
    assert d.accept('accept-extra-usage') == result
    assert len(jobs.load_jobs()) == 1 and binding.reads == 0
