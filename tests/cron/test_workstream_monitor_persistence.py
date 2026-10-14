"""Artifact-only independent owner-path checks on the immutable snapshot.
No authority receipts are minted here: Telegram -> GatewayRunner -> SessionDB
mints the human receipt. No host loop/monitor/scheduler method is replaced.
Only provider/network boundaries and the explicitly faulted SQLite write edge
are scripted. This is a negative failure gate, not an all-seven positive claim.
"""
import sqlite3
from pathlib import Path

from tests.gateway.test_trusted_human_origin_ingress import Harness
from tests.gateway.test_workstream_human_consumers import _scope, _user_content
from agent import workstreams
from cron import jobs, monitor, notepad, scheduler


def test_actual_correction_end_turn_fallback_matches_next_turn_scope(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, proactive={
        'enabled': True, 'mode': 'active', 'recall': {'enabled': True}})
    monkeypatch.setattr(notepad, 'NOTEPAD_FILE', h.home / 'notepad.db')
    try:
        h.inbound_human('Correction: scope-check uses deployment-X not deployment-Y', message_id=900)
        scope = _scope(h)
        rows = notepad.list_workstreams(scope, kinds=['correction'])
        assert len(rows) == 1 and rows[0]['approved']
        assert rows[0]['session_id'] == h.session_id()
        assert rows[0]['source'].startswith('human:')
        h.inbound_human('What does scope-check use?', message_id=901)
        api = _user_content(h.scripted.requests[-1])
        assert 'deployment-X not deployment-Y' in api
        assert '[ws:' + rows[0]['workstream_id'] in api
        assert h.user_rows()[-1]['api_content'] == api
        print('CANONICAL_OWNER_SCOPE', scope)
    finally:
        h.close()


def test_linked_monitor_persistence_failure_never_falls_through_to_model(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, proactive={'enabled': True, 'mode': 'active'})
    monkeypatch.setattr(notepad, 'NOTEPAD_FILE', h.home / 'notepad.db')
    try:
        # Real ordinary Telegram turn creates an unconsumed host-minted origin.
        h.inbound_human('Please retain the scope-check CI blocker', message_id=902)
        origin = h.db.get_human_origin(h.session_id(), h.user_rows()[-1]['id'])
        assert origin is not None and not origin.consumed
        scripts = h.home / 'scripts'
        scripts.mkdir(exist_ok=True)
        (scripts / 'ci-monitor.sh').write_text("printf 'CI check changed\\n'\n")
        job = jobs.create_job(prompt='Read the local CI state only', schedule='every 5m',
            monitor_script='ci-monitor.sh', deliver='local', model='scripted/test',
            provider='openai', workdir=str(h.project))
        # Use the actual cron owner's canonical scope and public validated
        # link writer; a genuine human receipt approves evidence, not execution.
        scope = workstreams.job_scope(job)
        row = notepad.create_workstream(scope, kind='blocker', title='CI blocker',
            summary='Waiting for the local CI check', fingerprint='ci-failure-probe',
            approved=False, session_id=h.session_id())
        from agent.workstream_origin import read_human_origin
        approved = workstreams.approve_workstream(scope, row['workstream_id'],
            settings=workstreams.load_settings(), origin=origin, db=h.db,
            human_reader=read_human_origin)
        assert approved and approved['approved']
        import yaml
        (h.home / 'config.yaml').write_text(yaml.safe_dump({'proactive': {
            'enabled': True, 'mode': 'active', 'eligible_jobs': [job['id']]}}))
        job = workstreams.link_job_to_workstream(scope, row['workstream_id'],
            job['id'], workstreams.load_settings(), origin=origin, db=h.db,
            human_reader=read_human_origin)
        assert workstreams.job_scope(job) == scope
        assert jobs.get_job(job['id'])['workstream_id'] == row['workstream_id']
        import clover_cli.runtime_provider as rp
        monkeypatch.setattr(rp, 'resolve_runtime_provider', lambda **kw: {
            'provider':'openai', 'api_mode':'chat_completions', 'api_key':'test-key',
            'base_url':'http://127.0.0.1:1/v1'})

        # Conditional failure injected at the real durable occurrence I/O seam.
        # No occurrence/plan/authority/event is synthetically attached.
        def fail_write(*args, **kwargs):
            raise sqlite3.OperationalError('deterministic occurrence-write failure')
        monkeypatch.setattr(notepad, 'ensure_occurrence', fail_write)
        before = h.scripted.calls
        outcomes = []
        for _ in range(2):
            outcomes.append(scheduler.run_job(dict(job)))
        after = h.scripted.calls
        persisted = jobs.get_job(job['id']).get('monitor_state')
        pending = notepad.pending_occurrences(scope)
        print('REAL_OWNER_FAILURE', {'provider_calls': after-before,
            'scheduler_results': outcomes, 'monitor_state':persisted,
            'pending_occurrences':pending})
        assert persisted is None and pending == []
        assert after == before, 'failed occurrence persistence must not launch ordinary model work'
    finally:
        h.close()
