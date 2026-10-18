"""Read-only human status projection. No model, credentials, transcript or history.

Only trusted hosts register a bounded ephemeral reference to their current affect
owner. Exact original context is used even for explicitly linked initiative.
"""
from collections import OrderedDict
from datetime import timedelta
import threading

_lock = threading.RLock()
_owners = OrderedDict()


def remember(host):
    from agent.presence_runtime import clock
    with _lock:
        for key, (owner, expires) in list(_owners.items()):
            if expires <= clock():
                _owners.pop(key, None)
        _owners[host.original_key] = (host.state.affect, clock()+timedelta(seconds=120))
        _owners.move_to_end(host.original_key)
        while len(_owners) > 128:
            _owners.popitem(last=False)


def render(*, key=None):
    """Caller authenticates key; absent key means unknown, not another scope."""
    from clover_cli.config import load_config_readonly
    from clover_cli.managed_scope import load_managed_config
    from agent import workstreams as ws
    from agent.bounded_completion import select_route, admitted
    from agent.presence_runtime import clock
    try:
        load_managed_config(strict=True)
        config = load_config_readonly()
        settings = ws.load_settings(config)
        enabled = config.get('presence',{}).get('enabled') is True
        active = settings.active and settings.suggestions_enabled
        mode = config.get('presence',{}).get('budget_mode','strict_tokens')
        if mode not in {'strict_tokens','requests'}:
            mode = 'invalid'
        calls_event = min(1, settings.aux_max_calls_per_event)
        calls_day = min(3, settings.aux_max_calls_per_day)
        output_limit = min(512, settings.aux_max_output_tokens)
    except Exception:
        return 'Emotions: unavailable (managed policy or configuration invalid).\nUser affect: unknown. No inference performed.'
    route_text, route_hold = 'not configured', None
    try:
        route = select_route(config)
        route_text = ws._redact(route.provider)[:80] + ' / ' + ws._redact(route.model)[:120]
        admitted(route)
    except Exception as exc:
        # Reasons are fixed identifiers, never exception text with source/secrets.
        route_hold = getattr(exc,'reason','configured_route_unavailable')
    held = ('feature_disabled' if not enabled else 'proactive_or_suggestions_disabled' if not active else
            'zero_assessment_budget' if min(settings.aux_max_calls_per_event,settings.aux_max_calls_per_day,
                                          settings.aux_max_output_tokens) <= 0 else route_hold)
    reading, expired = None, False
    if enabled and key is not None:
        with _lock:
            entry = _owners.get(key)
            if entry:
                owner, expiry = entry
                expired = clock() >= expiry
                if expired:
                    owner.correct()
                # The owner's validated read purges TTL/source/Stop-invalid state.
                reading = owner.latest
                if reading is None:
                    _owners.pop(key,None)
        try:
            from cron.presence import ThoughtStore
            store = ThoughtStore()
            if store.initiative_status()[1]:
                held = 'stopped'
            elif not held and store.assessment_count(clock().date().isoformat()) >= calls_day:
                held = 'daily_request_quota_exhausted'
        except Exception:
            held = 'state_unavailable'
    posture = 'careful_brief' if reading and reading.valence < -0.3 and reading.confidence >= 0.7 else 'ordinary'
    affect = 'unknown (expired)' if expired else 'unknown (no recent reading)'
    if reading:
        age = max(0, int((clock()-reading.observed_at).total_seconds()))
        remaining = max(0,120-age)
        affect = (f'tentative valence={reading.valence:.2f}, arousal={reading.arousal:.2f}; '
                  f'confidence={reading.confidence:.2f}; age {age}s; expires in {remaining}s. '
                  'Reason: bounded current authenticated evidence; source text withheld.')
    return (f'Emotions: {"enabled" if enabled else "disabled"}.\n'
            f'Clover interaction posture: {posture} (response style, not a felt emotion).\n'
            f'User affect: {affect}.\n'
            f'Provider route: {route_text}; budget mode: {mode}.\n'
            f'Initiative: {"eligible only after all gates" if active and enabled and not held else "held"}; '
            f'reason: {held or "event_and_evidence_gates_required"}.\n'
            f'Assessment limits: {calls_event}/event, {calls_day}/day/profile, '
            f'{output_limit} output envelope. '
            + ('Request mode is not a server-token or cost guarantee; usage may be unknown.\n' if mode == 'requests' else '')
            + 'Read only. No inference or changes. No emotional history is stored.')


async def telegram(runner, event):
    from agent.workstream_origin import submission_is_current
    if (getattr(event,'internal',False) or event.source.platform.value != 'telegram'
            or event.source.chat_type not in {'dm','private'} or not runner._is_user_authorized(event.source)
            or not submission_is_current(event,platform='telegram')):
        return 'Use /emotions in your authenticated private conversation. No emotional state is disclosed here.'
    if runner._is_stale_offline_command(event):
        return runner._stale_offline_command_reply('emotions')
    if event.get_command_args().strip():
        return '/emotions is read only and takes no arguments.'
    from agent import workstreams as ws
    from agent.presence_state import ContextKey
    key = None
    store = runner.session_store
    entry = store.lookup_by_session_key(store._generate_session_key(event.source))
    db = getattr(store,'_db',None)
    if entry is not None and db is not None:
        scope = ws.scope_for_surface(platform='telegram',chat_type=event.source.chat_type,
                                    chat_id=event.source.chat_id,user_id=event.source.user_id)
        key = ContextKey(scope,db.get_conversation_root(entry.session_id))
    return render(key=key)


def desktop(server, params):
    from pathlib import Path
    from clover_constants import set_clover_home_override, reset_clover_home_override
    from tui_gateway.methods_browser_control import _is_authenticated_identity, _principal_digest
    from agent.presence_state import ContextKey
    from agent import workstreams as ws
    sid = params.get('session_id')
    if not isinstance(sid,str) or not sid or len(sid)>512:
        return 'Current literal session identifier required.'
    transport, session = server._current_session_steer_authority(sid)
    if transport is None or transport.closed or not _is_authenticated_identity(transport.auth_identity):
        return 'Use /emotions in the current authenticated desktop conversation.'
    home = server._session_home(session)
    project = str(Path(server._session_cwd(session)).resolve())
    identity_instance = transport.auth_identity
    identity = dict(identity_instance)
    token = set_clover_home_override(home)
    try:
        key = None
        agent = session.get('agent')
        db = getattr(agent,'_session_db',None)
        if db is not None and session.get('session_key'):
            key = ContextKey(ws.scope_for_surface(platform='desktop',chat_type='dm',
                user_id=_principal_digest(identity),project=project),
                db.get_conversation_root(session['session_key']))
        text = render(key=key)
        actual_transport, actual_session = server._current_session_steer_authority(sid)
        if (actual_transport is not transport or actual_session is not session or transport.closed
                or transport.auth_identity is not identity_instance or transport.auth_identity != identity
                or server._session_home(session) != home
                or str(Path(server._session_cwd(session)).resolve()) != project
                or session.get('agent') is not agent):
            return 'Current authenticated conversation changed; emotional state withheld.'
        return text
    finally:
        reset_clover_home_override(token)
