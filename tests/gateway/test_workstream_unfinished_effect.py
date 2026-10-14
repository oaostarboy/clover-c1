"""Genuine accepted helper effect is not proof of its unfinished deliverable."""
import json
import time

import pytest

from cron import notepad
from tools import async_delegation as ad
from tests.gateway.test_workstream_suggestion_offer import OfferHarness, scope
from tests.gateway.test_trusted_human_origin_ingress import network_guard, SESSION_KEY  # noqa: F401
from tests.agent.test_handoff_completion_seam import _tools, _call, _text

pytestmark = pytest.mark.real_delegation_router


def test_actual_helper_partial_artifact_keeps_unverified_packaging_open_across_newchat(monkeypatch, tmp_path):
    ad._reset_for_tests()
    h = OfferHarness(monkeypatch, tmp_path, proactive={
        'enabled': True, 'mode': 'active', 'recall': {'enabled': True}})
    artifact = h.project / 'audit-observed.txt'
    package = h.project / 'packaging-not-run.txt'
    sent = {'parent': False, 'child': False}
    old = h.scripted.client.chat.completions.create.side_effect

    def provider(*args, **kwargs):
        old(*args, **kwargs)  # retain actual request accounting
        system = str(kwargs.get('messages', [{}])[0].get('content', ''))
        is_child = 'SUBAGENT' in system or 'subagent' in system.lower()
        if not is_child and not sent['parent']:
            sent['parent'] = True
            return _tools(_call('delegate_task', {'goal':
                'Write a local audit observation, then prepare packaging; report anything not run honestly.'}))
        if is_child and not sent['child']:
            sent['child'] = True
            return _tools(_call('write_file', {'path': str(artifact), 'content': 'local audit observation\n'}))
        return _text('Audit observation is ready; packaging was not run.')

    h.scripted.client.chat.completions.create.side_effect = provider
    try:
        h.inbound_human('Prepare the local audit observation and packaging.', message_id=950)
        deadline = time.monotonic() + 15
        while not artifact.exists() or any((ad.get_durable_delegation(r['delegation_id']) or {}).get('state') in ('running', 'finalizing') for r in ad.list_async_delegations()):
            assert time.monotonic() < deadline, 'real child did not finish its local artifact'
            time.sleep(0.02)
        assert artifact.read_text() == 'local audit observation\n' and not package.exists()
        rows = notepad.list_workstreams(scope(h), kinds=['obligation'], statuses=['open'])
        assert rows and any('packaging' in r['title'].lower() for r in rows)
        assert all(r['source'] == 'runtime' for r in rows)
        assert not notepad.list_workstreams(scope(h), kinds=['progress'])
        h.runner.session_store.reset_session(SESSION_KEY)
        h.inbound_human('What packaging work remains unverified?', message_id=951)
        wire = [m['content'] for m in h.scripted.requests[-1]['messages'] if m['role']=='user'][-1]
        assert any(r['workstream_id'] in wire for r in rows)
        assert 'unverified' in wire.lower() or 'running' in wire.lower()
        assert h.user_rows()[-1]['api_content'] == wire
        print('UNFINISHED_REAL_EFFECT', json.dumps({'artifact': str(artifact),
              'packaging_exists': package.exists(), 'open_ids': [r['workstream_id'] for r in rows]}))
    finally:
        h.close()
        ad._reset_for_tests()
