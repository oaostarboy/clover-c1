"""Ordinary composer owner -> real CLI/agent loop -> SQLite -> correction/recall.

Only provider/metadata and terminal rendering are scripted. No origin minting,
receipt attributes, consumer hooks or workstream rows are installed by tests.
"""
import json
import queue
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from clover_state import SessionDB
from cron import notepad
from agent import workstreams
from run_agent import AIAgent
from tests.gateway.test_trusted_human_origin_ingress import Harness, network_guard  # noqa: F401


@pytest.fixture
def local(monkeypatch, tmp_path):
    # Reuse the established hermetic primary+all-aux/metadata scripting, not
    # its Telegram producer, gateway dispatch, or authorization stubs.
    h = Harness(monkeypatch, tmp_path, proactive={
        'enabled': True, 'mode': 'active', 'recall': {'enabled': True},
    })
    import cli
    monkeypatch.setattr(cli, 'CLI_CONFIG', {
        'model': {'default': 'scripted/test'},
        'display': {'tool_progress': 'off'}, 'agent': {},
        'terminal': {'env_type': 'local'},
    })
    monkeypatch.setattr(cli, 'get_tool_definitions', lambda **kw: [])
    c = cli.CloverCLI(model='scripted/test', provider='openai', api_key='test-key',
                      base_url='http://127.0.0.1:1/v1')
    a = AIAgent(model='scripted/test', provider='openai', api_key='test-key',
                base_url='http://127.0.0.1:1/v1', platform='cli',
                session_id=c.session_id, session_db=h.db, quiet_mode=True,
                enabled_toolsets=[], skip_context_files=True, skip_memory=True,
                max_iterations=2)
    c.agent = a
    monkeypatch.setattr(c, '_ensure_runtime_credentials', lambda: True)
    monkeypatch.setattr(c, '_init_agent', lambda **kw: True)
    monkeypatch.setattr(c, '_resolve_turn_agent_config', lambda message: {
        'signature': c._active_agent_route_signature, 'model': 'scripted/test',
        'runtime': {},
    })
    monkeypatch.setattr(c, '_flush_credit_notices', lambda: None)
    yield SimpleNamespace(cli=c, agent=a, h=h)
    h.close()


def submit(local, text):
    c = local.cli
    # The exact production input-owner method used by the Enter callback.
    payload = c._capture_composer_submission(text)
    return c.chat(text, trusted_human_submission=payload)


def rows(local):
    return [m for m in local.h.db.get_messages(local.agent.session_id) if m['role'] == 'user']


def test_ordinary_composer_correction_persists_exact_row_and_fresh_chat_recall(local):
    text = 'Correction: use deployment-X not deployment-Y'
    submit(local, text)
    stored = rows(local)
    assert [r['content'] for r in stored] == [text]
    scope = workstreams.scope_for_agent(local.agent)
    corrections = notepad.list_workstreams(scope)
    assert len(corrections) == 1 and corrections[0]['kind'] == 'correction'
    with sqlite3.connect(local.h.db_path) as db:
        receipt = db.execute('SELECT session_id,message_row_id,consumed_by FROM human_origin_receipts').fetchone()
    assert receipt == (local.agent.session_id, stored[0]['id'], 'workstream_human_hooks')
    assert local.h.scripted.calls == 1
    # A new agent/session, same canonical project/audience, no provenance.
    fresh = AIAgent(model='scripted/test', provider='openai', api_key='test-key',
                    base_url='http://127.0.0.1:1/v1', platform='cli',
                    session_id='fresh-local-chat', session_db=local.h.db,
                    quiet_mode=True, enabled_toolsets=[], skip_context_files=True,
                    skip_memory=True, max_iterations=2)
    fresh.run_conversation('Which deployment-X path should I use?')
    request = json.dumps(local.h.scripted.requests[-1])
    assert 'deployment-Y' in request and corrections[0]['workstream_id'] in request
    assert workstreams.scope_for_agent(fresh) == scope


@pytest.mark.parametrize('source', ['direct', 'model', 'tool', 'cron', 'replay', 'seed', 'background'])
def test_generic_cli_text_never_becomes_human(local, source):
    local.cli.chat('Correction: use no-proof-' + source)
    assert notepad.list_workstreams(workstreams.scope_for_agent(local.agent)) == []
    assert local.h.db.get_human_origin(local.agent.session_id, rows(local)[-1]['id']) is None


@pytest.mark.parametrize('change', ['body', 'project', 'profile', 'session', 'stop', 'actor', 'audience'])
def test_composer_scope_or_invocation_change_fails_closed(local, monkeypatch, change):
    text = 'Correction: keep literal-X not literal-Y'
    c, a = local.cli, local.agent
    submission = c._capture_composer_submission(text)
    if change == 'body':
        text = 'Correction: middleware-derived input'
    elif change == 'project':
        monkeypatch.setenv('TERMINAL_CWD', str(local.h.project / 'foreign'))
    elif change == 'profile':
        monkeypatch.setenv('CLOVER_HOME', str(local.h.home / 'foreign'))
    elif change == 'session':
        c.session_id = 'foreign-session'
    elif change == 'stop':
        a.interrupt(hard_cancel=True)
    elif change == 'actor':
        a._user_id, a._chat_type = 'foreign-actor', 'dm'
    elif change == 'audience':
        a._chat_type, a._chat_id = 'group', 'foreign-group'
    c.chat(text, trusted_human_submission=submission)
    assert notepad.list_workstreams(workstreams.scope_for_surface(platform='cli', project=str(local.h.project),)) == []


def test_composer_submission_cannot_replay(local):
    text = 'Correction: use path-X not path-Y'
    payload = local.cli._capture_composer_submission(text)
    local.cli.chat(text, trusted_human_submission=payload)
    local.cli.chat(text, trusted_human_submission=payload)
    assert len(notepad.list_workstreams(workstreams.scope_for_agent(local.agent))) == 1
    assert local.h.db.get_human_origin(local.agent.session_id, rows(local)[-1]['id']) is None


def test_real_prompt_toolkit_enter_queue_process_loop(local, monkeypatch):
    """Real key parser, Enter callback, queue and process_loop (not chat shortcut)."""
    import cli
    from prompt_toolkit.application import Application
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    c = local.cli
    monkeypatch.setattr(c, 'show_banner', lambda: None)
    monkeypatch.setattr(c, '_show_security_advisories', lambda: None)
    monkeypatch.setattr(c, '_show_browser_backend_notice', lambda: None)
    monkeypatch.setattr(c, '_maybe_start_wake_word', lambda: None)
    monkeypatch.setattr('clover_cli.model_switch.prewarm_picker_cache_async', lambda: None)
    monkeypatch.setattr('agent.curator.maybe_run_curator', lambda **kw: None)
    apps = []
    with create_pipe_input() as pipe:
        def app_factory(**kwargs):
            kwargs.update(input=pipe, output=DummyOutput())
            app = Application(**kwargs)
            async def drive():
                import asyncio
                pipe.send_text('Correction: use composer-X not composer-Y\r')
                for _ in range(500):
                    if local.h.scripted.calls == 1 and not c._agent_running:
                        pipe.send_text('/exit\r')
                        return
                    await asyncio.sleep(.02)
                app.exit(exception=AssertionError('composer turn did not settle'))
            app.pre_run_callables.append(lambda: app.create_background_task(drive()))
            apps.append(app)
            return app
        monkeypatch.setattr(cli, 'Application', app_factory)
        c.run()
    assert apps and local.h.scripted.calls == 1
    assert len(notepad.list_workstreams(workstreams.scope_for_agent(local.agent))) == 1
    assert rows(local)[0]['content'] == 'Correction: use composer-X not composer-Y'
