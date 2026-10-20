"""SCRIPTED attribution regressions; host safeguards are not an NL oracle."""
import json

import pytest

from agent.presence_assessment import parse
from agent.presence_state import ContextKey
from cron.presence import ThoughtStore
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401
from tests.gateway.test_workstream_suggestion_offer import scope
from tests.test_presence_current_turn_behavior import assessment, scripted_host

TEXT = 'I wonder how deployment packaging could be safer.'


def explicit_thought(text, **kwargs):
    value = {'summary': text, 'quote': text, 'confidence': .99,
             'owner': 'current_human', 'intent': 'unresolved_question', 'source_context': 'direct'}
    value.update(kwargs)
    return value


def test_original_wrong_colleague_extraction_is_not_stored(monkeypatch, tmp_path):
    # Exact independent review counterexample; legacy unqualified confidence
    # must not turn a colleague's literal quoted words into the human's interest.
    text = 'My colleague wrote: "' + TEXT + '" Please count the words only.'
    h, requests, bot = scripted_host(monkeypatch, tmp_path,
        [assessment('ordinary', thought={'summary': TEXT, 'quote': TEXT, 'confidence': .99})])
    try:
        h.inbound_human(text, message_id=200)
        assert len(requests) == 1
        assert not ThoughtStore().list(ContextKey(scope(h), h.session_id()))
        assert not ThoughtStore().publications() and bot.send_message.await_count == 0
    finally:
        h.close()


def test_explicit_direct_user_owned_interest_still_captures(monkeypatch, tmp_path):
    h, requests, bot = scripted_host(monkeypatch, tmp_path,
        [assessment('ordinary', thought=explicit_thought(TEXT))])
    try:
        h.inbound_human(TEXT, message_id=200)
        pending = ThoughtStore().list(ContextKey(scope(h), h.session_id()))
        assert len(requests) == 1
        assert len(pending) == 1 and pending[0].summary == TEXT
        assert not ThoughtStore().publications() and bot.send_message.await_count == 0
    finally:
        h.close()


@pytest.mark.parametrize('field,value', [('owner', 'third_party'), ('owner', 'unknown'),
    ('intent', 'task'), ('intent', 'resolved'), ('intent', 'unknown'),
    ('source_context', 'quoted'), ('source_context', 'reported'), ('source_context', 'unknown')])
def test_non_user_or_uncertain_intent_abstains_without_losing_affect(field, value):
    sample = assessment('gentle', thought=explicit_thought(TEXT, **{field: value}))
    parsed = parse(json.dumps(sample), TEXT)
    assert parsed['thought'] is None
    assert parsed['affect'] == sample['affect']


@pytest.mark.parametrize('text', ['My colleague wrote: "' + TEXT + '" Please count the words only.',
    'My colleague wrote: “' + TEXT + '” Please count the words only.',
    'My colleague wrote: «' + TEXT + '» Please count the words only.',
    '> ' + TEXT, '```\n' + TEXT + '\n```'])
def test_even_wrong_explicit_owner_cannot_carry_structural_quotation(text):
    # Full current text and a false current_human label are still insufficient.
    parsed = parse(json.dumps(assessment(thought=explicit_thought(text))), text)
    assert parsed['thought'] is None


def test_partial_source_extract_abstains_even_with_false_direct_owner():
    text = 'My colleague wrote: "' + TEXT + '" Please count the words only.'
    parsed = parse(json.dumps(assessment(thought=explicit_thought(TEXT))), text)
    assert parsed['thought'] is None


@pytest.mark.parametrize('text', ['I wonder how to bundle C:\\tmp\\Build-A01 and re\u0301sume\u0301 id=AB-007 safely.',
    'Je me demande comment le déploiement pourrait être plus sûr.',
    '我想知道怎样让部署更安全。'])
def test_direct_source_preserves_literal_identifiers_and_multilingual_text(text):
    result = parse(json.dumps(assessment(thought=explicit_thought(text))), text)
    assert result['thought']['summary'] == text
