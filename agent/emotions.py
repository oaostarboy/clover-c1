"""Read-only human status projection. No model, credentials, transcript or history.

Only trusted hosts register a bounded ephemeral reference to their current affect
owner. Exact original context is used even for explicitly linked initiative.
"""
from collections import OrderedDict
from datetime import timedelta
import threading
import weakref

_lock = threading.RLock()
_owners = OrderedDict()
_owner_hosts = weakref.WeakKeyDictionary()


def clock():
    # Reuse an already-loaded host clock (also the deterministic test seam),
    # but a cold/disabled status must not import the presence runtime.
    import sys
    from datetime import datetime, timezone
    runtime = sys.modules.get('agent.presence_runtime')
    return runtime.clock() if runtime else datetime.now(timezone.utc)


def _owner_current(owner, config):
    """The host's currentness contract, with a settings-only policy read.

    Do not call AffectState.latest/current: those invoke the ordinary startup
    config loader and may flush state. Never repair/rebind an uncertain owner.
    """
    from agent import workstreams as ws
    host_ref = _owner_hosts.get(owner)
    host = host_ref() if host_ref else None
    settings = ws.load_settings(config)
    try:
        return (host is not None and owner.enabled is True
                and host.state.affect is owner and owner.key == host.key
                and config.get('presence', {}).get('enabled') is True
                and settings.active and settings.suggestions_enabled
                and host.gateway_current() is True
                and ws.scope_for_agent(host.agent) == host.original_key.scope
                and host.store.linked_key(host.original_key) == host.key
                and host.input_generation >= 0 and host.input_generation == host.store.generation()
                and host.store.initiative_status() == (host.epoch, False)
                and host.stop_generation == getattr(host.agent, '_human_ingress_stop_generation', 0))
    except Exception:
        return False


class _EvidenceDBView:
    """Suppress get_session's write-side token flush during nominated evidence read."""
    def __init__(self, db):
        self.db = db

    def __getattr__(self, name):
        return getattr(self.db, name)

    def get_session(self, sid):
        import sqlite3
        from pathlib import Path
        with sqlite3.connect(Path(self.db.db_path).resolve().as_uri() + '?mode=ro', uri=True) as conn:
            row = conn.execute('SELECT source FROM sessions WHERE id=?', (sid,)).fetchone()
        return {'source': row[0]} if row else None


def _peek_reading(key, config):
    from copy import copy
    from agent.presence_state import evidence_matches
    with _lock:
        entry = _owners.get(key)
        if not entry:
            return None, False
        owner, expiry = entry
        reading = owner._latest
        expired = clock() >= expiry or (reading is not None and clock() - reading.observed_at >= timedelta(seconds=120))
        if expired:
            _owners.pop(key, None)  # Ephemeral registry expiry, not owner correction.
            return None, True
        try:
            host_ref = _owner_hosts.get(owner)
            host = host_ref() if host_ref else None
            if host is None:
                return None, False
            reader = copy(host.reader)
            reader.db = _EvidenceDBView(host.reader.db)
            if (reading is None or not _owner_current(owner, config)
                    or not host.store.source_is_current(owner.key, reading.source)
                    or not timedelta(0) <= clock()-reading.observed_at < timedelta(seconds=120)
                    or not evidence_matches(reader, owner.key, reading.source)
                    or not host.store.source_is_current(owner.key, reading.source)
                    or owner._latest is not reading or _owners.get(key) is not entry
                    or not _owner_current(owner, config)):
                return None, False
            return reading, False
        except Exception:
            return None, False


def remember(host):
    from agent.presence_runtime import clock
    with _lock:
        for key, (owner, expires) in list(_owners.items()):
            if expires <= clock():
                _owners.pop(key, None)
        _owners[host.original_key] = (host.state.affect, clock()+timedelta(seconds=120))
        _owner_hosts[host.state.affect] = weakref.ref(host)
        _owners.move_to_end(host.original_key)
        while len(_owners) > 128:
            _owners.popitem(last=False)


def _profile_hold(calls_day):
    """Read only durable profile control/quota metadata, never thought payloads.

    Avoid cron package import: its initializer imports scheduler/config owners.
    No missing database, migration or credential store is opened for creation.
    """
    import sqlite3
    from clover_constants import get_clover_home
    path = get_clover_home().resolve() / 'cron' / 'presence.db'
    if not path.exists():
        return None
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, 2, 3, 4) or conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('unsupported presence state')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if version == 0:
            if tables:
                raise ValueError('unversioned presence state')
            return None
        if version == 1:
            return None  # The v1 owner had no durable Stop/quota.
        row = conn.execute('SELECT epoch,stopped FROM presence_epoch WHERE id=1').fetchone()
        if row is None or type(row[0]) is not int or row[0] < 0 or row[1] not in (0, 1):
            raise ValueError('invalid profile Stop state')
        if row[1]:
            return 'stopped'
        count = conn.execute('SELECT COUNT(*) FROM presence_budget WHERE day=?',
                             (clock().date().isoformat(),)).fetchone()[0]
        return 'daily_request_quota_exhausted' if calls_day > 0 and count >= calls_day else None


def render(*, key=None):
    """Caller authenticates key; absent key means unknown, not another scope."""
    from agent.emotions_config import snapshot
    from agent import workstreams as ws
    from agent.bounded_completion import select_route
    try:
        config = snapshot()
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
        # Selection is local metadata only. Admission/caps are checked by the
        # assessment owner, never by importing credential/inference runtimes.
    except Exception as exc:
        # Reasons are fixed identifiers, never exception text with source/secrets.
        route_hold = getattr(exc,'reason','configured_route_unavailable')
    held = ('feature_disabled' if not enabled else 'proactive_or_suggestions_disabled' if not active else
            'zero_assessment_budget' if min(settings.aux_max_calls_per_event,settings.aux_max_calls_per_day,
                                          settings.aux_max_output_tokens) <= 0 else route_hold)
    reading, expired = None, False
    if enabled:
        # Profile-level Stop/quota does not require an affect owner key.
        try:
            durable_hold = _profile_hold(calls_day)
            held = durable_hold or held
        except Exception:
            held = 'state_unavailable'
        if key is not None and active and held not in {'stopped', 'state_unavailable'}:
            reading, expired = _peek_reading(key, config)
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
            f'Provider route: {route_text}; budget mode: {mode}. Configured metadata only; dispatch admission not checked.\n'
            f'Initiative: {"eligible only after all gates" if active and enabled and not held else "held"}; '
            f'reason: {held or "event_and_evidence_gates_required"}.\n'
            f'Assessment limits: {calls_event}/event, {calls_day}/day/profile, '
            f'{output_limit} output envelope. '
            + ('Request mode is not a server-token or cost guarantee; usage may be unknown.\n' if mode == 'requests' else '')
            + 'Read only. No inference or changes. No emotional history is stored.')


def literal_session_id(value):
    """Validate, never normalize, a runtime/persisted identifier."""
    import re
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,512}', value) is not None


def _conversation_root(db, session_id, *, surface):
    """Prove an existing lineage without get_session()'s pending-token flush.

    get_conversation_root alone treats deleted/unknown rows as their own root.
    A separate read-only connection checks existence and rejects broken chains.
    """
    import sqlite3
    from pathlib import Path
    from clover_constants import get_clover_home
    if not literal_session_id(session_id):
        raise ValueError('invalid stored session')
    path = Path(db.db_path).resolve()
    if path != get_clover_home().resolve() / 'state.db':
        raise ValueError('foreign profile database')
    root = db.get_conversation_root(session_id)
    if not literal_session_id(root):
        raise ValueError('invalid conversation root')
    seen, current = set(), session_id
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
        while current and len(seen) < 100:
            if current in seen:
                raise ValueError('cyclic conversation')
            seen.add(current)
            row = conn.execute('SELECT parent_session_id,source FROM sessions WHERE id=?', (current,)).fetchone()
            if row is None or (current == session_id and row[1] != surface):
                raise ValueError('missing current conversation')
            if not row[0]:
                if current != root:
                    raise ValueError('conversation root changed')
                return root
            current = row[0]
    raise ValueError('invalid conversation lineage')


def _owner_ref(key):
    with _lock:
        entry = _owners.get(key)
        return entry[0] if entry else None


def _read_disclosure(key, owner, reading, config, text):
    from agent.emotions_config import snapshot
    try:
        if snapshot() != config:
            return False
        if 'confidence=' not in text:
            return True
        if owner is None or _owner_ref(key) is not owner or owner._latest is not reading:
            return False
        actual, _ = _peek_reading(key, config)
        return actual is reading and actual is not None
    except Exception:
        return False


def _telegram_authority(runner, event, route_key):
    """Snapshot the existing auth owner's inputs; never recompute its policy.

    Authorization itself may read pairing files. Watch the exact approved file
    and compare live local grant/route/generation bindings without I/O at return.
    No tokens or credential environments are inspected.
    """
    from copy import deepcopy
    from gateway.authz_mixin import _auth_env
    source = event.source
    authorize = runner._is_user_authorized
    adapter = runner._adapter_for_source(source)
    # Capture the exact existing transport registration before any snapshot
    # reads. The general resolver can fall through to profile filesystem I/O
    # after a registration disappears, so it must not run in the final fence.
    platform = source.platform
    adapter_ref = getattr(source, '_transport_adapter_ref', None)
    adapters = getattr(runner, 'adapters', None)
    profiles = getattr(runner, '_profile_adapters', None)
    registry = adapters if adapters is not None and adapters.get(platform) is adapter else None
    profile_name = None
    if registry is None and profiles is not None:
        for name, candidate in profiles.items():
            if candidate.get(platform) is adapter:
                profile_name, registry = name, candidate
                break
    def registered():
        if (adapter is None or registry is None
                or getattr(source, '_transport_adapter_ref', None) is not adapter_ref
                or not callable(adapter_ref) or adapter_ref() is not adapter):
            return False
        if profile_name is None:
            return getattr(runner, 'adapters', None) is registry and registry.get(platform) is adapter
        return (profiles is not None and getattr(runner, '_profile_adapters', None) is profiles
                and profiles.get(profile_name) is registry and registry.get(platform) is adapter)
    pairing = runner._pairing_store_for(source)
    paths = (pairing._approved_path('telegram'),) if pairing is not None else ()
    names = ('TELEGRAM_ALLOWED_USERS', 'TELEGRAM_ALLOW_ALL_USERS',
             'GATEWAY_ALLOWED_USERS', 'GATEWAY_ALLOW_ALL_USERS')
    def grants():
        extra = getattr(getattr(adapter, 'config', None), 'extra', None) or {}
        return (tuple(_auth_env(name) for name in names), deepcopy(extra.get('allow_from')))
    grant = grants()
    bot = getattr(adapter, '_bot', None)
    state = runner._peek_session_state(route_key)
    generation = state.persistent.run_generation if state is not None else 0
    def current():
        # These existing owners only consult in-memory mappings. Do not replace
        # with a config loader, pairing read, credential fingerprint or resolver.
        return (runner._is_user_authorized == authorize and grants() == grant
                and registered()
                and getattr(adapter, '_bot', None) is bot
                and runner._pairing_store_for(source) is pairing
                and runner._is_session_run_current(route_key, generation))
    return paths, current


async def telegram(runner, event):
    from agent.workstream_origin import redeem_human_command
    command_current = redeem_human_command(event, platform='telegram', command='emotions')
    if (command_current is None or getattr(event,'internal',False) or event.source.platform.value != 'telegram'
            or event.source.chat_type not in {'dm','private'} or not runner._is_user_authorized(event.source)):
        return 'Use /emotions in your authenticated private conversation. No emotional state is disclosed here.'
    if runner._is_stale_offline_command(event):
        return runner._stale_offline_command_reply('emotions')
    if event.get_command_args().strip():
        return '/emotions is read only and takes no arguments.'
    from agent import workstreams as ws
    from agent.presence_state import ContextKey
    from agent.emotions_snapshot import ReadFence
    withheld = 'Current authenticated conversation changed; emotional state withheld.'
    store = runner.session_store
    fence = None
    try:
        route_key = store._generate_session_key(event.source)
        entry = store.lookup_by_session_key(route_key)
        db = getattr(store, '_db', None)
        stored_id = entry.session_id if entry else None
        authority_paths, local_authority = _telegram_authority(runner, event, route_key)
        fence = ReadFence(db, session_id=stored_id, authority_paths=authority_paths)
        scope = ws.scope_for_surface(platform='telegram', chat_type=event.source.chat_type,
                                    chat_id=event.source.chat_id, user_id=event.source.user_id)
        key = None
        if entry:
            try:
                key = ContextKey(scope, _conversation_root(db, stored_id, surface='telegram'))
            except (ValueError, AttributeError):
                pass  # No proven durable owner: configuration-only status.
        owner = _owner_ref(key)
        reading = owner._latest if owner else None
        fence.bind(key, owner if reading is not None else None)
        from agent.emotions_config import snapshot
        config = snapshot()
        text = render(key=key)
        current_root = _conversation_root(db, stored_id, surface='telegram') if key else None
        disclosure_valid = _read_disclosure(key, owner, reading, config, text)
        current_entry = store.lookup_by_session_key(route_key)
        # Collect all path/config/source/authorization observations BEFORE seal.
        authority_valid = (command_current() and runner._is_user_authorized(event.source)
                and runner.session_store is store and getattr(store, '_db', None) is db
                and store._generate_session_key(event.source) == route_key
                and current_entry is entry and (not entry or entry.session_id == stored_id)
                and ws.scope_for_surface(platform='telegram', chat_type=event.source.chat_type,
                    chat_id=event.source.chat_id, user_id=event.source.user_id) == scope
                and (not key or current_root == key.conversation_root))
        fence.seal()
        # FINAL non-I/O decision: no source/root/config helper after this point.
        if not (authority_valid and disclosure_valid
                and command_current(profile_observed=str(fence.home)) and local_authority()
                and runner.session_store is store and getattr(store, '_db', None) is db
                and (not entry or entry.session_id == stored_id)
                and fence.current(private='confidence=' in text)):
            return withheld
        return text
    except Exception:
        return withheld
    finally:
        if fence is not None:
            fence.close()


def desktop(server, params):
    from pathlib import Path
    from clover_constants import set_clover_home_override, reset_clover_home_override
    from tui_gateway.methods_browser_control import _is_authenticated_identity, _principal_digest
    from agent.presence_state import ContextKey
    from agent.emotions_snapshot import ReadFence
    from agent import workstreams as ws
    sid = params.get('session_id')
    if not literal_session_id(sid):
        return 'Current literal session identifier required.'
    transport, session = server._current_session_steer_authority(sid)
    if transport is None or transport.closed or not _is_authenticated_identity(transport.auth_identity):
        return 'Use /emotions in the current authenticated desktop conversation.'
    home = server._session_home(session)
    project = str(Path(server._session_cwd(session)).resolve())
    identity_instance = transport.auth_identity
    identity = dict(identity_instance)
    local_paths = (session.get('profile_home'), session.get('cwd'), server._clover_home)
    token = set_clover_home_override(home)
    fence = None
    try:
        key = None
        agent = session.get('agent')
        db = getattr(agent,'_session_db',None)
        stored_key = session.get('session_key')
        fence = ReadFence(db, session_id=stored_key)
        if db is not None and stored_key:
            try:
                key = ContextKey(ws.scope_for_surface(platform='desktop',chat_type='dm',
                    user_id=_principal_digest(identity),project=project),
                    _conversation_root(db, stored_key, surface='desktop'))
            except (ValueError, AttributeError):
                pass  # Fresh or unprovable root never selects an affect owner.
        owner = _owner_ref(key)
        reading = owner._latest if owner else None
        fence.bind(key, owner if reading is not None else None)
        from agent.emotions_config import snapshot
        config = snapshot()
        text = render(key=key)
        current_root = _conversation_root(db, stored_key, surface='desktop') if key else None
        disclosure_valid = _read_disclosure(key, owner, reading, config, text)
        actual_transport, actual_session = server._current_session_steer_authority(sid)
        authority_valid = (actual_transport is transport and actual_session is session and not transport.closed
                and transport.auth_identity is identity_instance and transport.auth_identity == identity
                and server._session_home(session) == home
                and str(Path(server._session_cwd(session)).resolve()) == project
                and session.get('agent') is agent and getattr(agent, '_session_db', None) is db
                and session.get('session_key') == stored_key
                and (not key or current_root == key.conversation_root))
        fence.seal()
        # FINAL non-I/O decision; even disappearing registry ownership revokes.
        final_transport, final_session = server._current_session_steer_authority(sid)
        if not (authority_valid and disclosure_valid
                and final_transport is transport and final_session is session and not transport.closed
                and transport.auth_identity is identity_instance and transport.auth_identity == identity
                and (session.get('profile_home'), session.get('cwd'), server._clover_home) == local_paths
                and session.get('agent') is agent and getattr(agent, '_session_db', None) is db
                and session.get('session_key') == stored_key
                and fence.current(private='confidence=' in text)):
            return 'Current authenticated conversation changed; emotional state withheld.'
        return text
    except Exception:
        return 'Current authenticated conversation changed; emotional state withheld.'
    finally:
        if fence is not None:
            fence.close()
        reset_clover_home_override(token)
