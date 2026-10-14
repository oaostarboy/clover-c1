"""Inline owner truth, not model prose, for explicit authenticated operations."""
import sqlite3

import pytest

from cron import notepad
from tests.gateway.test_workstream_suggestion_offer import OfferHarness, scope
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from tests.agent.test_handoff_completion_seam import _text


@pytest.fixture
def human(monkeypatch, tmp_path):
    h = OfferHarness(monkeypatch, tmp_path, proactive={'enabled': True, 'mode': 'active'})
    old = h.scripted.client.chat.completions.create.side_effect
    def dishonest(*args, **kwargs):
        old(*args, **kwargs)
        return _text('Saved and delivered.')
    h.scripted.client.chat.completions.create.side_effect = dishonest
    yield h
    h.close()


def test_denied_operation_overrides_false_model_saved_claim(human):
    human.inbound_human('Feedback: ws_0123456789abcdef reject: no')
    result = human.results[-1]['final_response']
    assert result.startswith('Not recorded')
    assert 'Saved' not in result and 'delivered' not in result
    assert notepad.list_feedback(scope(human)) == []
    human.inbound_human('An ordinary unrelated question', message_id=43)
    assert human.results[-1]['final_response'] == 'Saved and delivered.'


def test_unknown_write_reports_nothing_recorded_without_success(human, monkeypatch):
    def fault(*args, **kwargs):
        raise sqlite3.OperationalError('test-only write fault')
    monkeypatch.setattr(notepad, 'create_workstream', fault)
    human.inbound_human('Correction: use deployment-X not deployment-Y')
    result = human.results[-1]['final_response']
    assert result.startswith('Not recorded') and 'write failed' in result
    assert 'Saved' not in result and 'delivered' not in result
    assert notepad.list_workstreams(scope(human)) == []
    assert not human.db.get_human_origin(human.session_id(), human.user_rows()[-1]['id']).consumed


def test_unknown_validation_read_never_leaves_model_saved_claim(human, monkeypatch):
    def fault(*args, **kwargs):
        raise sqlite3.OperationalError('test-only validation read fault')
    monkeypatch.setattr(notepad, 'get_workstream', fault)
    human.inbound_human('Correction: ws_0123456789abcdef use deployment-X not deployment-Y')
    result = human.results[-1]['final_response']
    assert result.startswith('Not recorded') and 'validation unavailable' in result
    assert 'Saved' not in result and 'delivered' not in result
    assert notepad.list_workstreams(scope(human)) == []
    assert not human.db.get_human_origin(human.session_id(), human.user_rows()[-1]['id']).consumed


def test_success_is_recorded_not_delivered_and_provider_history_unchanged(human):
    human.inbound_human('Correction: use deployment-X not deployment-Y')
    assert human.results[-1]['final_response'].startswith('Recorded correction ws_')
    assert human.scripted.calls == 1
    assert [m['content'] for m in human.db.get_messages(human.session_id()) if m['role']=='assistant'] == ['Saved and delivered.']
    assert not notepad.list_receipts(scope(human), kinds=['delivered', 'saved'])
