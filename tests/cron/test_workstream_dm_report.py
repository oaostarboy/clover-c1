"""Installed monitor replies are owned persistence outcomes, not model claims."""
import sqlite3
from dataclasses import asdict

import pytest
from cron import jobs, notepad as np
from tests.cron.test_workstream_dm_scope import dm, network_guard, request, _text  # noqa: F401


@pytest.mark.parametrize('fault', ['denied', 'unknown_write', 'created'])
def test_installed_monitor_reply_never_reports_false_model_saved_or_delivered(dm, monkeypatch, fault):
    old = dm.scripted.client.chat.completions.create.side_effect
    def dishonest(*args, **kwargs):
        old(*args, **kwargs)
        return _text('Saved and delivered.')
    dm.scripted.client.chat.completions.create.side_effect = dishonest
    if fault == 'unknown_write':
        def broken_save(*args, **kwargs):
            raise sqlite3.OperationalError('local-only test persistence fault')
        monkeypatch.setattr(jobs, '_save_jobs_unlocked', broken_save)
    message = request(dm, scope={**asdict(dm.scope), 'root': 'foreign'}) if fault == 'denied' else request(dm)
    dm.inbound_human(message)
    sent = repr(dm.adapter.send.await_args_list[-1])
    assert 'Saved and delivered' not in sent
    if fault == 'created':
        assert 'created locally' in sent and 'not enrolled or linked' in sent
        assert dm.admissions[-1].handled
    else:
        assert 'not confirmed' in sent
        assert not dm.admissions[-1].handled
        if fault == 'unknown_write':
            assert 'unknown' in sent and 'before retry' in sent
    assert dm.scripted.calls == 1
    assert not np.list_receipts(dm.scope, kinds=['delivered', 'saved', 'authorized'])
