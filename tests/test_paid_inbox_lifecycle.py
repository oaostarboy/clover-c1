"""Consent lifecycle and real current-source policy failures, offline only."""
import json
import sqlite3
import pytest
from tests.test_paid_inbox_consent import FakeBinding
from tests.gateway.test_proactive_command import home  # noqa: F401


def draft(binding):
    from cron.inbox_watcher import Draft
    d=Draft(binding)
    for x in ['source','cadence 30','destination','route','urgency 7','limits 2']: d.choose(x)
    return d


def test_ordinary_accept_without_presented_paid_review_is_refused(home):
    from cron.inbox_watcher import Denied
    with pytest.raises(Denied): draft(FakeBinding(home)).accept('accept-extra-usage')
    assert not (home/'inbox-watcher'/'state.db').exists()


def test_zero_allowance_means_disabled_without_provider_key_or_new_job(home):
    from cron import inbox_watcher as w,jobs
    b=FakeBinding(home);d=draft(b);d.choose('limits 0');d.render()
    result=d.accept('accept-extra-usage')
    assert result['state']=='disabled' and not jobs.load_jobs()
    assert not w.state_path(home).exists()


def test_missing_allowance_cannot_be_recreated_while_an_owned_job_exists(home):
    from cron import inbox_watcher as w
    b=FakeBinding(home);d=draft(b);d.render();d.accept('accept-extra-usage')
    w.state_path(home).unlink()
    fresh=draft(b);fresh.render()
    with pytest.raises(w.Denied):fresh.accept('accept-extra-usage')
    assert not w.state_path(home).exists()


def test_revoked_review_cannot_restart_but_new_review_can_and_retains_budget(home):
    from cron import inbox_watcher as w
    from cron.jobs import get_job,load_jobs
    b=FakeBinding(home); d=draft(b); d.render(); p=d.accept('accept-extra-usage')
    store=w.Store(w.state_path(home))
    store.reserve(w.Limits(attempts_day=2),now=172800,run_id='spent',input_units=100)
    w.revoke(b)
    with pytest.raises(w.Denied): d.accept('accept-extra-usage')
    fresh=draft(b);fresh.render();p2=fresh.accept('accept-extra-usage')
    assert p2['state']=='active' and p2['job_id']!=p['job_id']
    assert not get_job(p['job_id'])['enabled'] and get_job(p2['job_id'])['enabled']
    assert store.usage()['attempts']==1 and len(load_jobs())==2


def test_valid_json_state_corruption_cannot_reset_allowance(tmp_path):
    from cron import inbox_watcher as w
    s=w.Store(tmp_path/'s.db',create=True)
    s.reserve(w.Limits(attempts_day=1),now=172800,run_id='spent',input_units=100)
    with sqlite3.connect(s.path) as c:
        d=json.loads(c.execute('SELECT body FROM state').fetchone()[0]);d['budget'].update(attempts=0,input_units=0,output_tokens=0,runs=[])
        c.execute('UPDATE state SET body=?',(json.dumps(d),))
    with pytest.raises(w.Denied): s.reserve(w.Limits(attempts_day=1),now=172801,run_id='bypass',input_units=100)
