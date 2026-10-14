"""Ordinary installed compose retains the audited capsule, not just storage."""
from cron import notepad
from tests.agent.test_workstream_continuity_host import (
    SETTINGS, PATH_X, TEST_Z, produce, scope, _project_outcomes)
from tests.gateway.test_trusted_human_origin_ingress import (
    Harness, network_guard, SESSION_KEY)  # noqa: F401


def test_installed_newchat_compose_preserves_correction_obligation_progress_and_redaction(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, proactive=SETTINGS)
    try:
        _, correction = produce(h)
        outcomes = _project_outcomes(h)
        progress = next(r for r in notepad.list_workstreams(scope(h)) if r['kind'] == 'progress')
        secret = 'ghp_' + 'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8'
        notepad.update_workstream(scope(h), correction['workstream_id'],
            expected_revision=correction['revision'],
            summary=f'Use {PATH_X}; {TEST_Z} failed; legacy token {secret}')
        monkeypatch.setattr('agent.redact._REDACT_ENABLED', False)
        h.runner.session_store.reset_session(SESSION_KEY)
        h.inbound_human('Which packaging status and remediation remain?', message_id=990)
        wire = [m['content'] for m in h.scripted.requests[-1]['messages'] if m['role']=='user'][-1]
        assert PATH_X in wire and TEST_Z in wire
        assert outcomes[0]['workstream_id'] in wire
        assert progress['workstream_id'] in wire
        assert secret not in wire
        assert h.user_rows()[-1]['api_content'] == wire
    finally:
        h.close()
