"""Offline provider configuration metadata. No auth, inference or discovery.

Shared by bounded admission and credential binding; status callers may import
this module without importing auxiliary clients or credential owners.
"""
from dataclasses import dataclass
import os
from clover_cli.providers import (
    get_provider, resolve_user_provider, resolve_custom_provider,
    custom_provider_aliases,
)

# Existing auxiliary direct-API alias, not model-name/provider detection.
DIRECT_API_BASE_URLS = {'openai': 'https://api.openai.com/v1'}


def endpoint_identity(value):
    """Structural owner identity; never rewrite the caller's endpoint token.

    Default ports and the SDK's terminal slash are equivalent. Tenant/base
    paths, non-default ports, and provider/config provenance stay distinct.
    """
    from urllib.parse import urlsplit
    parsed = urlsplit(str(value or ''))
    port = parsed.port
    if port is None:
        port = {'https': 443, 'http': 80}.get(parsed.scheme)
    return (parsed.scheme, parsed.hostname, port, parsed.path.rstrip('/'),
            parsed.username, parsed.password, parsed.query, parsed.fragment)


def configured_entry(config, name):
    raw = name.lower()
    providers = config.get('providers', {})
    if isinstance(providers, dict) and isinstance(providers.get(raw), dict):
        return providers[raw]
    for entry in config.get('custom_providers', []) or []:
        if isinstance(entry, dict) and raw in custom_provider_aliases(
                entry.get('name', ''), entry.get('provider_key', '')):
            return entry
    return None


@dataclass(frozen=True)
class ProviderMetadata:
    provider: str
    endpoint: str
    api_mode: str
    auth_type: str
    key_envs: tuple[str, ...]
    configured: bool = False
    credential_endpoint: str = ''


def configured_metadata(config, name):
    """Supplied-settings projection only: no env, catalog, auth or runtime I/O."""
    entry = configured_entry(config, name)
    if entry is not None:
        owner = (resolve_user_provider(name.lower(), config.get('providers'))
                 or resolve_custom_provider(name, config.get('custom_providers')))
        if owner is not None:
            mode = entry.get('api_mode') or {
                'anthropic': 'anthropic_messages', 'codex_responses': 'codex_responses'
            }.get(owner.transport, 'chat_completions')
            return ProviderMetadata(owner.id, owner.base_url, mode, owner.auth_type,
                                    owner.api_key_env_vars, True)
    return None


def metadata(config, name):
    """Dispatch-only offline registry/endpoint metadata; may read local catalog/env.

    Status uses configured_metadata/select_route, never this enrichment layer.
    """
    configured = configured_metadata(config, name)
    if configured is not None:
        return configured
    from providers import get_provider_profile
    if name in DIRECT_API_BASE_URLS:
        return ProviderMetadata(name, DIRECT_API_BASE_URLS[name], 'chat_completions',
                                'api_key', ('OPENAI_API_KEY',))
    if name == 'custom':
        return ProviderMetadata(name, '', 'chat_completions', 'api_key', ())
    profile = get_provider_profile(name)
    owner = get_provider(name, allow_network=False)
    if owner is None:
        return ProviderMetadata(name, '', 'unsupported', 'unknown', ())
    credential_endpoint = getattr(profile, 'base_url', '') or owner.base_url
    endpoint = (os.getenv(owner.base_url_env_var, '') if owner.base_url_env_var else '')
    endpoint = endpoint or credential_endpoint
    mode = getattr(profile, 'api_mode', None) or {
        'openai_chat': 'chat_completions', 'codex_responses': 'codex_responses',
        'anthropic': 'anthropic_messages'
    }.get(owner.transport, 'chat_completions')
    return ProviderMetadata(owner.id, endpoint, mode, owner.auth_type, owner.api_key_env_vars,
                            credential_endpoint=credential_endpoint)
