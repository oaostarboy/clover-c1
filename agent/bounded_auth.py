"""No-refresh credential acquisition for the bounded one-request lane.

Never call load_pool/select/peek, ordinary auth resolvers, command token sources,
quota discovery or OAuth refresh here. Configured task credentials are owner
sources in their own right; JWT claims only shape an already-authorized wire.
"""
from dataclasses import dataclass, field
import hashlib
import json
import time
from typing import Callable, NoReturn
from urllib.parse import urlparse

from agent.bounded_provider_metadata import metadata, configured_entry, DIRECT_API_BASE_URLS


def _held(reason) -> NoReturn:
    from agent.bounded_completion import CompletionHeld
    raise CompletionHeld(reason)


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).digest()


def _endpoint(value):
    # Deliberately exact apart from the SDK's trailing slash. Host equality is
    # insufficient: tenants on different paths do not own each other's secrets.
    return str(value or '').rstrip('/')


def _config_binding(config):
    return _fingerprint(tuple(config.get(k) for k in
        ('model', 'auxiliary', 'providers', 'custom_providers', 'presence')))


@dataclass(frozen=True)
class Authority:
    """Opaque immutable snapshot; never expose credentials/fingerprints in logs."""
    fingerprint: bytes = field(repr=False)
    readback: Callable = field(repr=False, compare=False)
    valid: Callable = field(repr=False, compare=False)

    def validate(self):
        try:
            if _fingerprint(self.readback()) != self.fingerprint:
                _held('credential_authority_changed')
            self.valid()
        except Exception as exc:
            from agent.bounded_completion import CompletionHeld
            if isinstance(exc, CompletionHeld):
                raise
            _held('credential_authority_changed')


def _configured_key(entry, aux):
    """An explicit but unresolved override is a hold, never ambient fallback."""
    if not isinstance(entry, dict):
        return None, None
    if entry.get('key_cmd'):
        _held('credential_command_override_unsupported')
    if 'api_key' in entry and entry['api_key'] not in (None, ''):
        key = entry['api_key']
        if not isinstance(key, str) or not key.strip():
            _held('credential_override_unsupported')
        return key.strip(), None
    env = entry.get('key_env') or entry.get('api_key_env')
    if env:
        key = aux._scoped_key_env(env)
        if not key:
            _held('credential_override_unavailable')
        return key, lambda: aux._scoped_key_env(env)
    return None, None



def _owned_reader(reader, expected):
    # Baseline the exact credential we will send, not a later replacement that
    # happened to appear while constructing the snapshot.
    expected_fp = _fingerprint(expected)
    def read():
        value = reader()
        if _fingerprint(value) != expected_fp:
            _held('credential_authority_changed')
        return value
    return read


def acquire(config, route, *, source_config=None):
    """Return (owned credential, exact endpoint, immutable revalidation handle).

    Runtime context is the existing turn-local owner, never compatibility
    globals or another session's last selection. A pool reference is optional;
    absent that, an exact concrete runtime token/endpoint is an owned handle.
    """
    from agent import auxiliary_client as aux
    from clover_cli.auth import read_credential_pool, _codex_access_token_is_expiring
    config_fp = _config_binding(config)
    source_config = config if source_config is None else source_config
    meta = metadata(config, route.provider)
    if _config_binding(source_config) != config_fp:
        _held('configured_route_changed')
    base = route.base_url or meta.endpoint
    if not base:
        _held('configured_endpoint_required')
    parsed = urlparse(base)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        _held('invalid_configured_endpoint')
    task = config.get('auxiliary', {}).get('presence', {})
    main = config.get('model', {})
    main = main if isinstance(main, dict) else {}
    entry = configured_entry(config, route.provider)
    checks = []
    valid_checks = []
    key, source_reader = _configured_key(task, aux)
    explicit = key is not None
    if source_reader:
        checks.append(_owned_reader(source_reader, key))
    main_meta = metadata(config, main.get('provider', ''))
    same_main = (route.provider == main.get('provider')
                 and _endpoint(base) == _endpoint(main.get('base_url') or main_meta.endpoint))
    if key is None and same_main:
        key, source_reader = _configured_key(main, aux)
        if source_reader:
            checks.append(_owned_reader(source_reader, key))
    if key is None and entry is not None and _endpoint(base) == _endpoint(meta.endpoint):
        key, source_reader = _configured_key(entry, aux)
        if source_reader:
            checks.append(_owned_reader(source_reader, key))

    if key is None and not route.configured:
        from clover_cli.models import opencode_zen_free_runtime
        policy = opencode_zen_free_runtime(route.provider, route.model)
        if policy is not None and _endpoint(base) == _endpoint(policy['base_url']):
            key = policy['api_key']

    runtime = aux._RUNTIME_MAIN_CONTEXT.get()
    runtime = runtime if isinstance(runtime, dict) else {}
    same_runtime_provider = runtime.get('provider') in {route.provider, meta.provider}
    if (not same_runtime_provider and runtime.get('provider') == 'custom'
            and runtime.get('requested_provider') == route.provider
            and (route.provider in DIRECT_API_BASE_URLS or meta.configured)):
        same_runtime_provider = True
    runtime_base = runtime.get('base_url') or meta.endpoint
    if key is None and same_runtime_provider and _endpoint(base) == _endpoint(runtime_base):
        pool = runtime.get('credential_pool')
        if pool is not None:
            selected = pool.current()
            if selected is None:
                _held('selected_authority_missing')
            from agent.credential_pool import credential_pool_matches_provider
            if not credential_pool_matches_provider(pool.provider, runtime.get('provider'), base_url=base):
                _held('credential_endpoint_unowned')
            selected_id = selected.id
            selected_key = selected.runtime_api_key
            selected_state = _fingerprint(selected.to_dict())
            def pool_state():
                current_runtime = aux._RUNTIME_MAIN_CONTEXT.get() or {}
                current = pool.current()
                return (current_runtime.get('provider'), current_runtime.get('model'),
                        current_runtime.get('base_url'), current_runtime.get('api_key'),
                        id(current_runtime.get('credential_pool')), current.to_dict() if current else None)
            def pool_valid():
                current = pool.current()
                if current is None or current.provider != pool.provider:
                    _held('credential_authority_changed')
                if (current.id != selected_id or current.runtime_api_key != selected_key
                        or _fingerprint(current.to_dict()) != selected_state):
                    _held('credential_authority_changed')
                if current.last_status not in (None, '', 'ok'):
                    _held('credential_unavailable')
                if pool._entry_needs_refresh(current):  # pure expiry check only
                    _held('credential_expired')
                if current.expires_at_ms is not None and current.expires_at_ms <= time.time() * 1000:
                    _held('credential_expired')
                if _endpoint(current.runtime_base_url or runtime_base) != _endpoint(base):
                    _held('credential_endpoint_unowned')
            pool_valid()
            key = selected_key
            if runtime.get('api_key') and runtime['api_key'] != key:
                _held('credential_authority_changed')
            checks.append(pool_state)
            valid_checks.append(pool_valid)
            # Read only the exact selected record. Changes to other accounts
            # neither select them nor invalidate an unchanged selected owner.
            from clover_constants import get_clover_home
            def disk_state():
                rows = read_credential_pool(pool.provider)
                selected_rows = [row for row in rows if row.get('id') == selected_id]
                return (get_clover_home() / 'auth.json').exists(), selected_rows, bool(rows)
            disk_exists, disk_rows, has_rows = disk_state()
            if (disk_exists or has_rows) and len(disk_rows) != 1:
                _held('credential_authority_revoked')
            if disk_rows:
                row = disk_rows[0]
                if row.get('last_status') not in (None, '', 'ok'):
                    _held('credential_authority_revoked')
                if row.get('access_token') and row['access_token'] != selected_key:
                    _held('credential_authority_changed')
            checks.append(_owned_reader(disk_state, (disk_exists, disk_rows, has_rows)))
        else:
            candidate = runtime.get('api_key')
            if callable(candidate):
                _held('credential_override_unsupported')
            if candidate:
                key = candidate
                runtime_fields = ('provider', 'model', 'base_url', 'api_key')
                expected_runtime = tuple(runtime.get(k) for k in runtime_fields)
                checks.append(_owned_reader(lambda: tuple((aux._RUNTIME_MAIN_CONTEXT.get() or {}).get(k)
                    for k in runtime_fields), expected_runtime))

    owned_default = _endpoint(base) == _endpoint(meta.endpoint)
    if key is None:
        # Unselected persisted pool entries are NOT authority to choose an
        # account. They also cannot silently fall through to an ambient key.
        if read_credential_pool(meta.provider):
            _held('selected_authority_missing')
        from clover_cli.auth import PROVIDER_REGISTRY
        provider_config = PROVIDER_REGISTRY.get(meta.provider)
        declared_envs = tuple(dict.fromkeys((*meta.key_envs,
            *getattr(provider_config, 'api_key_env_vars', ()))))
        envs = declared_envs if owned_default else ()
        # Anonymous custom main route explicitly pairs OPENAI_API_KEY with its
        # configured endpoint, just as the established custom provider contract.
        if route.provider == 'custom' and same_main:
            envs = ('OPENAI_API_KEY',)
        # Preserve provider-owned .env precedence and profile secret scope,
        # without the ordinary resolver's pool enumeration/refresh fallback.
        from clover_cli.config import get_env_value_prefer_dotenv
        from clover_cli.auth import _secret_matches_declared_prefix, has_usable_secret
        reader = aux._scoped_key_env if route.provider in {'openai', 'custom'} else get_env_value_prefer_dotenv
        if meta.provider == 'anthropic' and owned_default:
            envs = ('ANTHROPIC_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', *envs)
        for env in envs:
            candidate = reader(env)
            if candidate and has_usable_secret(candidate) and _secret_matches_declared_prefix(meta.provider, candidate):
                key = candidate
                checks.append(_owned_reader(lambda env=env: reader(env), key))
                break
    if key is None:
        # Existing named custom mappings explicitly permit no-key-required.
        # Anonymous custom local endpoints do too. Never guess a remote keyless
        # route solely from a caller-provided URL or borrow OPENAI_API_KEY.
        local = parsed.hostname in {'localhost', '127.0.0.1', '::1'}
        if ((entry is not None and owned_default) or (local and route.provider == 'custom')):
            key = 'no-key-required'
        elif meta.provider == 'lmstudio' and local:
            from clover_cli.auth import LMSTUDIO_NOAUTH_PLACEHOLDER
            key = LMSTUDIO_NOAUTH_PLACEHOLDER
        elif meta.provider == 'actual':
            from clover_cli.auth import ACTUAL_LOCAL_NOAUTH_PLACEHOLDER, is_actual_local_base_url
            if not is_actual_local_base_url(base):
                _held('credential_endpoint_unowned')
            key = ACTUAL_LOCAL_NOAUTH_PLACEHOLDER
        else:
            _held('credential_endpoint_unowned' if not owned_default or route.provider == 'custom'
                  else 'selected_authority_missing')
    if not isinstance(key, str) or not key:
        _held('credential_override_unsupported')
    if not route.configured:
        from clover_cli.models import opencode_zen_free_runtime
        policy = opencode_zen_free_runtime(route.provider, route.model)
        if (policy is not None and _endpoint(base) == _endpoint(policy['base_url'])
                and key != policy['api_key']):
            _held('credential_override_unsupported')
    if not route.configured and route.provider in {'openai-codex', 'codex'}:
        # Expiry is not authority authentication; provenance above is required.
        def token_valid():
            if _codex_access_token_is_expiring(key, 120):
                _held('credential_expired')
        token_valid()
        valid_checks.append(token_valid)
        if explicit and _endpoint(base) != _endpoint(aux._CODEX_AUX_BASE_URL):
            _held('credential_override_unsupported')
    def readback():
        if _config_binding(config) != config_fp or _config_binding(source_config) != config_fp:
            _held('configured_route_changed')
        return (_config_binding(config), [check() for check in checks],
                route.provider, base, route.model, route.api_mode)
    def valid():
        if _config_binding(config) != config_fp or _config_binding(source_config) != config_fp:
            _held('configured_route_changed')
        for check in valid_checks:
            check()
    authority = Authority(_fingerprint(readback()), readback, valid)
    return key, base, authority


def build_client(config, route, *, async_mode, source_config=None):
    """Construct audited SDK/serializer objects from already-owned material."""
    from agent import auxiliary_client as aux
    from agent.bounded_completion import admitted
    cap = admitted(route)
    key, base, authority = acquire(config, route, source_config=source_config)
    meta = metadata(config, route.provider)
    headers = {}
    if not route.configured and route.provider in {'openai-codex', 'codex'}:
        headers = aux._codex_cloudflare_headers(key, base_url=base)
    else:
        from providers import get_provider_profile
        profile = get_provider_profile(route.provider)
        headers.update(getattr(profile, 'default_headers', {}) or {})
        if aux.base_url_host_matches(base, 'api.kimi.com'):
            headers['User-Agent'] = 'claude-code/0.1.0'
        elif aux.base_url_host_matches(base, 'githubcopilot.com'):
            from clover_cli.copilot_auth import copilot_request_headers
            headers.update(copilot_request_headers(is_agent_turn=True, is_vision=False))
        elif aux.base_url_host_matches(base, 'integrate.api.nvidia.com'):
            headers.update(aux.build_nvidia_nim_headers(base))
        # A main provider's extra headers may themselves contain secrets. Do
        # not import global main headers into a different endpoint/task owner.
        main = config.get('model', {})
        main = main if isinstance(main, dict) else {}
        main_meta = metadata(config, main.get('provider', ''))
        sources = []
        if (main.get('provider') == route.provider and _endpoint(base) ==
                _endpoint(main.get('base_url') or main_meta.endpoint)):
            sources.append(main)
        entry = configured_entry(config, route.provider)
        if entry is not None and _endpoint(base) == _endpoint(meta.endpoint):
            sources.append(entry)
        sources.append(config.get('auxiliary', {}).get('presence', {}))
        for source in sources:
            for name in ('default_headers', 'extra_headers'):
                for header, value in (source.get(name) or {}).items():
                    if header.lower() in {'authorization', 'x-api-key', 'api-key', 'x-goog-api-key', 'chatgpt-account-id'}:
                        _held('credential_header_override_unsupported')
                    headers[header] = value
    from clover_cli.models import OPENCODE_ZEN_FREE_KEYLESS_PLACEHOLDER, opencode_zen_free_headers
    if key == OPENCODE_ZEN_FREE_KEYLESS_PLACEHOLDER:
        headers.update(opencode_zen_free_headers())
    if cap.transport == 'anthropic_messages':
        from agent.anthropic_adapter import build_anthropic_client, _is_oauth_token
        native = build_anthropic_client(key, base)
        client = aux.AnthropicAuxiliaryClient(native, route.model, key, base, _is_oauth_token(key))
    elif cap.transport == 'gemini_native':
        from agent.gemini_native_adapter import GeminiNativeClient
        client = GeminiNativeClient(api_key=key, base_url=base)
    elif async_mode:
        import openai
        client = openai.AsyncOpenAI(api_key=key, base_url=aux._to_openai_base_url(base),
            default_headers=headers, max_retries=0, **aux._openai_http_client_kwargs(base, async_mode=True))
    else:
        client = aux._create_openai_client(api_key=key, base_url=aux._to_openai_base_url(base),
                                          default_headers=headers)
    setattr(client, '_bounded_authority', authority)
    setattr(client, '_bounded_headers', headers)
    setattr(client, '_bounded_keyless', key == OPENCODE_ZEN_FREE_KEYLESS_PLACEHOLDER)
    return client, route.model
