"""Regression: a 24-hour skip cannot silently change mailbox UID identity."""
import pytest
from tests.gateway.test_proactive_command import home  # noqa: F401
from tests.test_paid_inbox_runner import InputBinding, accepted


def test_gap_rebaseline_preserves_mailbox_epoch_until_fresh_consent(home):
    from cron import inbox_watcher as w
    b = InputBinding(home)
    job = accepted(home, b)
    w.run(job, b, now=172800)
    seen = []
    def changed_epoch(cursor, *, max_items):
        seen.append(cursor)
        return {'epoch':'2', 'cursor':50, 'items':[]}
    b.fetch = changed_epoch
    w.run(job, b, now=262800)  # existing consent has been idle for 25 hours
    d = w.Store(w.state_path(home)).read()
    assert seen == [None], 'idle gap must skip history rather than fetch old IDs'
    assert d['cursor'] == {'epoch':'1', 'cursor':10}, 'an idle gap must not forget accepted mailbox identity'
    assert d['status'] == 'source cursor changed'
    w.run(job, b, now=264600)
    assert w.Store(w.state_path(home)).read()['cursor'] == {'epoch':'1','cursor':10}
    assert not b.attempts and not b.sent
    b.client.close()


@pytest.mark.parametrize('next_cursor,expected_cursor', [(5,10), (50,50)])
def test_gap_history_skip_keeps_uid_cursor_monotonic(home, next_cursor, expected_cursor):
    from cron import inbox_watcher as w
    b = InputBinding(home)
    job = accepted(home, b)
    w.run(job, b, now=172800)
    supplied = []
    def baseline(cursor, *, max_items):
        supplied.append(cursor)
        return {'epoch':'1', 'cursor':next_cursor, 'items':[]}
    b.fetch = baseline
    w.run(job, b, now=262800)
    d = w.Store(w.state_path(home)).read()
    assert supplied == [None]
    assert d['cursor'] == {'epoch':'1', 'cursor':expected_cursor}
    assert not b.attempts and not b.sent
    b.client.close()
