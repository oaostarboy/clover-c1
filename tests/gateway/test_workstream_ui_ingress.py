"""Authenticated WS input owner -> actual UI handler -> agent -> durable row.

Uses the real single-use dashboard credential verifier, not RPC user fields.
Only socket bytes and model/provider/metadata/rendering edges are scripted.
"""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect

from agent import workstreams
from cron import notepad
from tests.cli.test_workstream_composer_ingress import local  # noqa: F401
from tests.gateway.test_trusted_human_origin_ingress import network_guard  # noqa: F401


class SocketFixture:
    def __init__(self, text, *, params=None, credential=None):
        self.query_params = credential or {}
        self.headers = {}
        self.client = SimpleNamespace(host='127.0.0.1', port=1234)
        self.url = SimpleNamespace(path='/api/ws')
        self.frames = []
        self.text = text
        self.params = params or {}
        self.count = 0
        self.done = asyncio.Event()

    async def accept(self, **kw):
        pass

    async def send_text(self, frame):
        data = json.loads(frame)
        self.frames.append(data)
        if (data.get('params') or {}).get('type') in {'message.complete', 'error'}:
            self.done.set()

    async def receive_text(self):
        self.count += 1
        if self.count == 1:
            return json.dumps({'jsonrpc': '2.0', 'id': 'submit-1', 'method': 'prompt.submit',
                               'params': {'session_id': 'ui-fixture', 'text': self.text, **self.params}})
        await asyncio.wait_for(self.done.wait(), timeout=20)
        raise WebSocketDisconnect(code=1000)


@pytest.fixture
def ui(local, monkeypatch):
    # server imports kick off this unrelated network owner immediately.
    # Patch its launch boundary BEFORE import; suppressing session startup
    # callbacks below is too late for the banner worker.
    from clover_cli import banner
    monkeypatch.setattr(banner, 'prefetch_update_check', lambda: None)
    from tui_gateway import server
    from clover_cli import web_server
    monkeypatch.setattr(web_server.app.state, 'auth_required', True, raising=False)
    a = local.agent
    a.platform = 'desktop'
    session = {
        'agent': a, 'session_key': a.session_id, 'history': [],
        'history_lock': threading.RLock(), 'running': False,
        'cwd': str(local.h.project), 'profile_home': str(local.h.home),
        'attached_images': [], 'last_active': 0,
        'agent_ready': threading.Event(), 'agent_error': None,
        'cols': 80, 'transport': None,
    }
    session['agent_ready'].set()
    monkeypatch.setattr(server, '_sessions', {'ui-fixture': session})
    # Suppress unrelated startup/background owners, never the prompt handler,
    # persisted-turn consumer, origin registry, or actual conversation loop.
    for name in ('_schedule_startup_orphan_sweep', '_start_agent_build', '_wire_callbacks',
                 '_sync_agent_model_with_config', '_sync_agent_compression_with_config',
                 '_sync_bot_capabilities', '_register_session_cwd'):
        monkeypatch.setattr(server, name, lambda *a, **kw: None)
    monkeypatch.setattr(server, '_load_dashboard_process_isolation_config', lambda: {})
    monkeypatch.setattr(server, '_load_cfg', lambda: {})
    monkeypatch.setattr(server, '_ensure_active_session_slot', lambda *a: None)
    return SimpleNamespace(local=local, session=session, server=server, web=web_server)


def inbound(ui, *, identity='login', params=None, user='fixture-owner', text='Correction: use desktop-X not desktop-Y'):
    from clover_cli.dashboard_auth.ws_tickets import mint_ticket, internal_ws_credential
    from tui_gateway.ws import handle_ws
    if identity == 'login':
        credential = {'ticket': mint_ticket(user_id=user, provider='fixture-login')}
    elif identity == 'internal':
        credential = {'internal': internal_ws_credential()}
    elif identity == 'invalid':
        credential = {'ticket': 'not-a-ticket'}
    else:
        credential = {}
    ws = SocketFixture(text, params=params, credential=credential)
    reason, _ = ui.web._ws_auth_reason(ws)
    if reason is not None:
        return ws, reason
    # This is the production gateway_ws -> handle_ws authenticated handoff.
    # Attach the session to this exact connection as session.create/resume do.
    real_register = ui.server.register_live_transport
    def register(transport):
        ui.session['transport'] = transport
        return real_register(transport)
    ui.server.register_live_transport = register
    deadline = time.monotonic() + 20
    try:
        asyncio.run(handle_ws(ws, auth_identity=getattr(ws, '_clover_auth_identity', None)))
        # The terminal frame precedes real worker finalization. Wait for the
        # actual owner to retire before a test changes/reuses its principal;
        # never manufacture settlement by clearing the product's running flag.
        # Socket completion and settlement share the original 20-second budget.
        while (worker := ui.session.get('_run_thread')) is not None:
            worker.join(timeout=max(0, deadline - time.monotonic()))
            assert not worker.is_alive(), 'authenticated UI worker did not settle'
            if ui.session.get('_run_thread') is worker:
                break
        assert ui.session['running'] is False
    finally:
        ui.server.register_live_transport = real_register
    return ws, None


def test_ui_fixture_never_starts_unrelated_update_discovery(local, monkeypatch, request):
    from clover_cli import banner

    started = []
    monkeypatch.setattr(banner, 'prefetch_update_check', lambda: started.append(True))
    request.getfixturevalue('ui')
    assert started == [], 'script the startup edge before importing the server'


def test_inbound_waits_for_actual_worker_before_reusing_identity(ui, monkeypatch):
    """A terminal socket frame must not release the next authenticated owner."""
    emitted = threading.Event()
    release = threading.Event()
    joining = threading.Event()
    returned = threading.Event()
    settled = threading.Event()
    workers = []
    outcomes = []
    errors = []
    real_emit = ui.server._emit
    real_settled = ui.server._emit_settled_session_info

    def gate_terminal_frame(*args, **kwargs):
        result = real_emit(*args, **kwargs)
        if args and args[0] == 'message.complete':
            worker = threading.current_thread()
            workers.append(worker)
            real_join = worker.join

            def observe_join(*a, **kw):
                joining.set()
                return real_join(*a, **kw)

            monkeypatch.setattr(worker, 'join', observe_join)
            emitted.set()
            assert release.wait(20), 'test must release the original worker'
        return result

    def observe_settled(*args, **kwargs):
        try:
            return real_settled(*args, **kwargs)
        finally:
            settled.set()

    def invoke():
        try:
            outcomes.append(inbound(ui))
        except BaseException as exc:
            errors.append(exc)
        finally:
            returned.set()

    monkeypatch.setattr(ui.server, '_emit', gate_terminal_frame)
    monkeypatch.setattr(ui.server, '_emit_settled_session_info', observe_settled)
    caller = threading.Thread(target=invoke)
    caller.start()
    try:
        assert emitted.wait(20), 'real product must emit its terminal frame'
        assert joining.wait(5), 'inbound must join the real worker before changing principals'
        assert ui.session['running'] is True
        assert workers[0].is_alive()
        assert not returned.is_set(), 'socket completion alone must not settle the helper'
        assert not settled.is_set()
    finally:
        release.set()
        caller.join(20)
        for worker in workers:
            worker.join(20)
        assert not caller.is_alive(), 'fixture caller leaked past cleanup'
        assert all(not worker.is_alive() for worker in workers), 'fixture worker leaked'
    assert not errors
    assert len(outcomes) == 1 and outcomes[0][1] is None
    assert returned.is_set() and settled.is_set()
    assert ui.session['running'] is False


def test_authenticated_desktop_input_persists_correction_in_actor_scope(ui):
    ws, reason = inbound(ui)
    assert reason is None
    scope = workstreams.scope_for_surface(platform='desktop', chat_type='dm',
        user_id=ui.local.agent._user_id, project=str(ui.local.h.project))
    records = notepad.list_workstreams(scope)
    assert len(records) == 1 and records[0]['kind'] == 'correction'
    assert scope.audience.startswith('private:principal:dashboard:')
    assert scope.root == scope.audience.removeprefix('private:')
    assert scope != workstreams.scope_for_surface(platform='cli')
    assert ui.local.h.scripted.calls == 1
    rows = ui.local.h.db.get_messages(ui.local.agent.session_id)
    assert [m['content'] for m in rows if m['role'] == 'user'] == ['Correction: use desktop-X not desktop-Y']
    from run_agent import AIAgent
    fresh = AIAgent(model='scripted/test', provider='openai', api_key='test-key',
        base_url='http://127.0.0.1:1/v1', platform='desktop',
        session_id='fresh-ui-session', session_db=ui.local.h.db, quiet_mode=True,
        enabled_toolsets=[], skip_context_files=True, skip_memory=True, max_iterations=2)
    ui.session.update(agent=fresh, session_key=fresh.session_id, history=[])
    inbound(ui, text='Which desktop-X path is configured?')
    assert ui.local.h.scripted.calls == 2
    assert records[0]['workstream_id'] in str(ui.local.h.scripted.requests[-1])
    # A different authenticated principal is a different private audience,
    # not a common "private" bucket (even while using the same profile/project).
    ui.session.update(history=[])
    inbound(ui, user='other-owner', text='Which desktop-X path is configured?')
    assert ui.local.h.scripted.calls == 3
    assert records[0]['workstream_id'] not in str(ui.local.h.scripted.requests[-1])


@pytest.mark.parametrize('identity', ['none', 'invalid', 'internal'])
def test_unauthenticated_or_service_identity_never_writes(ui, identity):
    inbound(ui, identity=identity, params={'user_id': 'fixture-owner', 'origin': 'human'})
    assert notepad.list_workstreams(workstreams.scope_for_surface(platform='cli')) == []
    with ui.local.h.db._read_ctx() as db:
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='human_origin_receipts'").fetchone()[0] == 0


@pytest.mark.parametrize('params', [
    {'display_kind': 'hidden'}, {'truncate_before_user_ordinal': 0},
    {'queued': True}, {'origin': 'human', 'user_id': 'spoof'},
])
def test_nonoriginal_ui_operations_do_not_mint(ui, params):
    inbound(ui, params=params)
    with ui.local.h.db._read_ctx() as db:
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='human_origin_receipts'").fetchone()[0] == 0


@pytest.mark.parametrize('change', ['stop', 'generation', 'transport-identity', 'project', 'profile'])
def test_ui_revocation_after_original_row_blocks_consumer(ui, monkeypatch, tmp_path, change):
    from run_agent import AIAgent
    real = AIAgent._persist_session
    fired = []
    def persist_then_revoke(agent, *args, **kw):
        out = real(agent, *args, **kw)
        if not fired:
            fired.append(True)
            if change == 'stop':
                agent.interrupt(hard_cancel=True)
                agent.clear_interrupt()
            elif change == 'generation':
                ui.session['_queued_prompt_generation'] = 999
            elif change == 'transport-identity':
                ui.session['transport'].auth_identity = {'user_id': 'foreign', 'provider': 'fixture-login'}
            elif change == 'project':
                ui.session['cwd'] = str(tmp_path)
            else:
                ui.session['profile_home'] = str(tmp_path)
        return out
    monkeypatch.setattr(AIAgent, '_persist_session', persist_then_revoke)
    inbound(ui)
    assert fired
    scope = workstreams.scope_for_surface(platform='desktop', chat_type='dm',
        user_id=ui.local.agent._user_id, project=str(ui.local.h.project))
    assert notepad.list_workstreams(scope) == []
